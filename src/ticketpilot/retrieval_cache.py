import asyncio
import hashlib
import json
import logging
import random
from datetime import UTC, datetime
from time import monotonic, perf_counter
from typing import Any
from uuid import uuid4

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

from ticketpilot.observability import current_run
from ticketpilot.policies import applicable_chunks, policy_citation
from ticketpilot.retrieval import HybridPolicyRetriever
from ticketpilot.schemas import Citation, RequestPrincipal

logger = logging.getLogger(__name__)

RELEASE_LEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
PUBLISH_IF_OWNER = """
if redis.call('GET', KEYS[2]) == ARGV[1] then
    redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
    return 1
end
return 0
"""


def create_cache_client(url: str, timeout: float) -> Redis:
    return Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
        max_connections=20,
        retry=Retry(NoBackoff(), 0),
    )


class CachedPolicyRetriever:
    def __init__(
        self,
        retriever: HybridPolicyRetriever,
        client: Redis,
        *,
        ttl_seconds: int = 600,
        negative_ttl_seconds: int = 30,
        timeout_seconds: float = 0.15,
        wait_seconds: float = 1.5,
        max_inflight: int = 64,
        lease_seconds: float = 10,
    ) -> None:
        self.retriever = retriever
        self.client = client
        self.ttl_seconds = ttl_seconds
        self.negative_ttl_seconds = negative_ttl_seconds
        self.timeout_seconds = timeout_seconds
        self.wait_seconds = wait_seconds
        self.max_inflight = max_inflight
        self.lease_seconds = lease_seconds
        self.inflight = 0
        self.unavailable_until = 0.0
        self.locks = [asyncio.Lock() for _ in range(64)]

    def metadata(self) -> dict[str, Any]:
        return {**self.retriever.metadata(), "retrieval_cache": "redis"}

    def cache_key(
        self, principal: RequestPrincipal, query: str, limit: int, as_of: datetime
    ) -> str:
        payload = {
            **self.retriever.metadata(),
            "tenant_id": principal.tenant_id,
            "query": query,
            "limit": limit,
            "min_similarity": self.retriever.min_similarity,
            "applicable_chunks": [
                c.chunk_id for c in applicable_chunks(self.retriever.index.corpus, principal, as_of)
            ],
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        return f"ticketpilot:policy-search:v1:{digest}"

    def _unavailable(self) -> None:
        if monotonic() >= self.unavailable_until:
            logger.warning("Policy cache unavailable; using retrieval with a 5s cache cooldown")
        self.unavailable_until = monotonic() + 5

    async def _read(
        self, key: str, principal: RequestPrincipal, limit: int, as_of: datetime
    ) -> tuple[list[Citation] | None, str]:
        if monotonic() < self.unavailable_until:
            return None, "BYPASS"
        try:
            async with asyncio.timeout(self.timeout_seconds):
                raw = await self.client.get(key)
        except (RedisError, OSError, TimeoutError, UnicodeError):
            self._unavailable()
            return None, "UNAVAILABLE"
        if raw is None:
            return None, "MISS"
        allowed = {
            c.chunk_id: c for c in applicable_chunks(self.retriever.index.corpus, principal, as_of)
        }
        try:
            ids = json.loads(raw)
            if (
                not isinstance(ids, list)
                or len(ids) > limit
                or any(not isinstance(i, str) or i not in allowed for i in ids)
                or len(set(ids)) != len(ids)
            ):
                return None, "CORRUPT"
        except (ValueError, TypeError):
            return None, "CORRUPT"
        # Redis stores identifiers only; authoritative excerpts and scope come from the corpus.
        return [policy_citation(allowed[i]) for i in ids], "HIT"

    async def _write(self, key: str, result: list[Citation], token: str) -> str:
        if monotonic() < self.unavailable_until:
            return "BYPASS"
        base_ttl = self.ttl_seconds if result else self.negative_ttl_seconds
        ttl = max(1, int(base_ttl * random.uniform(0.9, 1.1)))
        try:
            async with asyncio.timeout(self.timeout_seconds):
                stored = await self.client.eval(
                    PUBLISH_IF_OWNER,
                    2,
                    key,
                    key + ":lease",
                    token,
                    json.dumps([c.chunk_id for c in result]),
                    ttl,
                )
        except (RedisError, OSError, TimeoutError):
            self._unavailable()
            return "UNAVAILABLE"
        return "STORED" if stored else "LEASE_LOST"

    async def _acquire(self, key: str, token: str) -> bool | None:
        if monotonic() < self.unavailable_until:
            return None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return bool(
                    await self.client.set(
                        key + ":lease", token, nx=True, px=max(1, int(self.lease_seconds * 1000))
                    )
                )
        except (RedisError, OSError, TimeoutError):
            self._unavailable()
            return None

    async def _release(self, key: str, token: str) -> None:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                await self.client.eval(RELEASE_LEASE, 1, key + ":lease", token)
        except (RedisError, OSError, TimeoutError):
            self._unavailable()

    async def _resolve(
        self,
        principal: RequestPrincipal,
        query: str,
        limit: int,
        deadline: float,
        event: dict[str, Any],
    ) -> list[Citation]:
        token = uuid4().hex
        while True:
            # Every wait can cross a policy's effective/expiry boundary.
            as_of = datetime.now(UTC)
            key = self.cache_key(principal, query, limit, as_of)
            result, status = await self._read(key, principal, limit, as_of)
            event["cache_status"] = status
            if result is not None:
                return result
            acquired = await self._acquire(key, token)
            if acquired is None:
                event["cache_write_status"] = "BYPASS"
                event["coordination"] = "UNAVAILABLE"
                return await self.retriever.search(principal, query, limit, as_of=as_of)
            if acquired:
                event["coordination"] = "OWNER"
                try:
                    # Another owner may have filled the cache between our GET and SET NX.
                    result, checked = await self._read(key, principal, limit, as_of)
                    if result is not None:
                        event["cache_status"] = checked
                        return result
                    result = await self.retriever.search(principal, query, limit, as_of=as_of)
                    event["cache_write_status"] = await self._write(key, result, token)
                    return result
                finally:
                    await self._release(key, token)
            event["coordination"] = "WAIT"
            remaining = deadline - monotonic()
            if remaining <= 0:
                event["cache_status"] = "FILL_BUSY"
                raise TimeoutError("policy_cache_fill_busy")
            await asyncio.sleep(min(remaining, random.uniform(0.025, 0.075)))

    async def search(
        self, principal: RequestPrincipal, query: str, limit: int = 5
    ) -> list[Citation]:
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        started = perf_counter()
        event: dict[str, Any] = {"kind": "cache", "stage": "policy_search", "outcome": "FAILED"}
        admitted = False
        try:
            if self.inflight >= self.max_inflight:
                event["cache_status"] = "OVERLOADED"
                raise TimeoutError("policy_cache_capacity")
            self.inflight += 1
            admitted = True
            deadline = monotonic() + self.wait_seconds
            key = self.cache_key(principal, query, limit, datetime.now(UTC))
            lock = self.locks[int(key[-8:], 16) % len(self.locks)]
            try:
                async with asyncio.timeout(self.wait_seconds):
                    await lock.acquire()
            except TimeoutError:
                event["cache_status"] = "QUEUE_TIMEOUT"
                raise
            try:
                result = await self._resolve(principal, query, limit, deadline, event)
                event["outcome"] = "SUCCEEDED"
                event["result_count"] = len(result)
                return result
            finally:
                lock.release()
        finally:
            if admitted:
                self.inflight -= 1
            event["duration_ms"] = round((perf_counter() - started) * 1000, 2)
            if run := current_run.get():
                run.events.append(event)
