import asyncio
import random
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.runnables.config import merge_configs


class ModelCallFailed(Exception):
    pass


class ExecutionLimitExceeded(Exception):
    pass


class InvalidCitation(Exception):
    pass


@dataclass(frozen=True)
class ExecutionLimits:
    deadline_seconds: float = 120
    call_timeout_seconds: float = 45
    max_model_calls: int = 6
    max_tokens: int = 16000
    max_output_tokens: int = 1200
    max_retries: int = 1


@dataclass
class RunTelemetry:
    trace_id: str
    limits: ExecutionLimits = field(default_factory=ExecutionLimits)
    started: float = field(default_factory=perf_counter)
    calls: int = 0
    charged_tokens: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)

    def reserve(self, input_estimate: int) -> int:
        if self.calls >= self.limits.max_model_calls:
            raise ExecutionLimitExceeded("model_call_limit")
        reserved = input_estimate + self.limits.max_output_tokens
        if self.charged_tokens + reserved > self.limits.max_tokens:
            raise ExecutionLimitExceeded("token_budget")
        if perf_counter() - self.started >= self.limits.deadline_seconds:
            raise ExecutionLimitExceeded("run_deadline")
        self.calls += 1
        self.charged_tokens += reserved
        return reserved

    def summary(self) -> dict[str, Any]:
        usage = [e for e in self.events if e.get("kind") == "model"]
        known = all(e.get("input_tokens") is not None for e in usage)
        return {
            "trace_id": self.trace_id,
            "duration_ms": round((perf_counter() - self.started) * 1000),
            "model_calls": self.calls,
            "policy_cache_hits": sum(
                e.get("cache_status") == "HIT" for e in self.events if e.get("kind") == "cache"
            ),
            "policy_cache_requests": sum(e.get("kind") == "cache" for e in self.events),
            "budget_tokens_charged": self.charged_tokens,
            "usage_complete": known,
            "input_tokens": sum(e["input_tokens"] for e in usage) if known else None,
            "output_tokens": sum(e["output_tokens"] for e in usage) if known else None,
            "cost_estimate": None,
            "cost_note": "No configured provider price table",
            "limits": vars(self.limits),
            "prompt_version": "classification-v2/grounded-v3",
        }


current_run: ContextVar[RunTelemetry | None] = ContextVar("ticketpilot_run", default=None)


class UsageCollector(AsyncCallbackHandler):
    def __init__(self) -> None:
        self.usage: dict[str, int] | None = None

    async def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        for group in response.generations:
            for generation in group:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None)
                if usage and "input_tokens" in usage and "output_tokens" in usage:
                    self.usage = {
                        "input_tokens": int(usage["input_tokens"]),
                        "output_tokens": int(usage["output_tokens"]),
                    }


async def invoke_model(runnable: Any, messages: list[Any], config: Any, stage: str) -> Any:
    run = current_run.get() or RunTelemetry("standalone")
    # UTF-8 byte count is a conservative reservation, not reported model usage.
    estimate = sum(len(str(message.content).encode("utf-8")) for message in messages) + 128
    for attempt in range(run.limits.max_retries + 1):
        reserved = run.reserve(estimate)
        collector = UsageCollector()
        started = perf_counter()
        event: dict[str, Any] = {
            "kind": "model",
            "stage": stage,
            "attempt": attempt + 1,
            "input_tokens": None,
            "output_tokens": None,
            "model_route": str(config.get("configurable", {}).get("model", "default")),
        }
        try:
            remaining = run.limits.deadline_seconds - (perf_counter() - run.started)
            async with asyncio.timeout(min(remaining, run.limits.call_timeout_seconds)):
                result = await runnable.ainvoke(
                    messages, merge_configs(config, {"callbacks": [collector]})
                )
            event["outcome"] = "SUCCEEDED"
            return result
        except Exception as exc:
            event["outcome"] = "FAILED"
            event["error_type"] = type(exc).__name__
            status = getattr(exc, "status_code", None)
            transient = (
                isinstance(exc, (TimeoutError, ConnectionError))
                or status
                in {
                    429,
                    500,
                    502,
                    503,
                    504,
                }
                or type(exc).__name__ in {"APITimeoutError", "APIConnectionError"}
            )
            if not transient or attempt >= run.limits.max_retries:
                raise ModelCallFailed(type(exc).__name__) from exc
            await asyncio.sleep(0.2 * 2**attempt + random.uniform(0, 0.1))
        finally:
            if collector.usage:
                event.update(collector.usage)
                run.charged_tokens += sum(collector.usage.values()) - reserved
            event["duration_ms"] = round((perf_counter() - started) * 1000)
            run.events.append(event)
    raise ModelCallFailed("retry_exhausted")
