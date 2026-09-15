import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
from langchain_core.messages import HumanMessage

from ticketpilot.observability import (
    ExecutionLimitExceeded,
    ExecutionLimits,
    InvalidCitation,
    ModelCallFailed,
    RunTelemetry,
    current_run,
    invoke_model,
)
from ticketpilot.policies import PolicyChunk, PolicyCorpus, applicable_chunks, load_policy_corpus
from ticketpilot.retrieval import PolicySearchIndex
from ticketpilot.schemas import Citation, RequestPrincipal, TicketClassification


def principal(tenant="tenant-demo-01"):
    return RequestPrincipal(tenant_id=tenant, actor_id="customer", role="CUSTOMER")


def test_versions_and_tenant_override_are_filtered_before_ranking():
    corpus = load_policy_corpus(
        Path(__file__).resolve().parents[2] / "data/ticketpilot/policy_v2_manifest.json"
    )
    regular = {
        c.chunk_id
        for c in applicable_chunks(corpus, principal(), datetime(2026, 9, 15, tzinfo=UTC))
    }
    vip = {
        c.chunk_id
        for c in applicable_chunks(
            corpus, principal("tenant-vip"), datetime(2026, 9, 15, tzinfo=UTC)
        )
    }
    assert len(corpus.chunks) == 163
    assert "return-01" in regular and "return-01-vip" not in regular
    assert "return-01-vip" in vip and "return-01" not in vip
    assert not {"return-01-old", "return-01-future"} & (regular | vip)
    old = applicable_chunks(corpus, principal(), datetime(2026, 8, 1, tzinfo=UTC))
    assert [c.chunk_id for c in old] == ["return-01-old"]


def test_dense_reranker_never_receives_unauthorized_candidates():
    allowed = PolicyChunk("source", "allowed", "退款", frozenset({"*"}), ("退款",), "退款需要审批")
    secret = replace(allowed, chunk_id="secret", tenant_ids=frozenset({"other"}), content="SECRET")
    embedding = Mock()
    embedding.embed.side_effect = [np.array([[1.0, 0.0], [1.0, 0.0]]), np.array([[1.0, 0.0]])]
    reranker = Mock()
    reranker.score.return_value = [1.0]
    index = PolicySearchIndex(PolicyCorpus("test", "", (allowed, secret)), embedding, reranker)
    index.warm()
    result = index.search(principal(), "退款", strategy="rerank")
    assert [c.chunk_id for c in result] == ["allowed"]
    assert "SECRET" not in str(reranker.score.call_args)


@pytest.mark.asyncio
async def test_transient_retry_is_bounded_and_unknown_usage_is_not_zero():
    run = RunTelemetry("trace", ExecutionLimits(max_retries=1))
    token = current_run.set(run)
    try:
        runnable = Mock(ainvoke=AsyncMock(side_effect=[ConnectionError(), "ok"]))
        assert await invoke_model(runnable, [HumanMessage("hello")], {}, "classify") == "ok"
        assert run.calls == 2
        assert [e["outcome"] for e in run.events] == ["FAILED", "SUCCEEDED"]
        assert run.summary()["input_tokens"] is None
        assert run.summary()["cost_estimate"] is None
    finally:
        current_run.reset(token)


@pytest.mark.asyncio
async def test_timeout_retry_and_token_limit_prevent_extra_calls():
    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    runnable = Mock(ainvoke=AsyncMock(side_effect=slow))
    run = RunTelemetry("trace", ExecutionLimits(call_timeout_seconds=0.001, max_retries=0))
    token = current_run.set(run)
    try:
        with pytest.raises(ModelCallFailed):
            await invoke_model(runnable, [HumanMessage("hello")], {}, "answer")
        assert runnable.ainvoke.await_count == 1
        run.limits = ExecutionLimits(max_tokens=1)
        with pytest.raises(ExecutionLimitExceeded):
            await invoke_model(runnable, [HumanMessage("hello")], {}, "answer")
        assert runnable.ainvoke.await_count == 1
    finally:
        current_run.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,ids", [("答案", ["invented"]), ("答案", []), ("答案[forged]", ["valid"])]
)
async def test_fabricated_and_missing_citations_fail_closed(monkeypatch, text, ids):
    from ticketpilot import reasoning

    model = Mock()
    model.with_structured_output.return_value.ainvoke = AsyncMock(
        return_value={"text": text, "citation_ids": ids}
    )
    monkeypatch.setattr(reasoning, "get_model", lambda _: model)
    with pytest.raises(InvalidCitation):
        await reasoning.LangChainTicketReasoner().answer(
            "退款政策",
            TicketClassification(category="POLICY"),
            None,
            [Citation(source_id="source", chunk_id="valid", title="规则", excerpt="需审批")],
            {},
        )
