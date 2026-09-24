import asyncio
import json
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from ticketpilot import retrieval_cache
from ticketpilot.observability import RunTelemetry, current_run
from ticketpilot.policies import PolicyChunk, PolicyCorpus
from ticketpilot.retrieval import HybridPolicyRetriever, PolicySearchIndex
from ticketpilot.retrieval_cache import CachedPolicyRetriever, create_cache_client
from ticketpilot.schemas import RequestPrincipal

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def principal(tenant="tenant-a"):
    return RequestPrincipal(tenant_id=tenant, actor_id="customer", role="CUSTOMER")


def retriever(chunks=None):
    chunk = PolicyChunk("source", "public", "退货", frozenset({"*"}), ("退货",), "退货需审批")
    private = replace(
        chunk, chunk_id="private", tenant_ids=frozenset({"tenant-a"}), content="退货专属条款"
    )
    index = PolicySearchIndex(PolicyCorpus("test", "", tuple(chunks or [chunk, private])))
    result = HybridPolicyRetriever(index, "bm25")
    result.search = AsyncMock(wraps=result.search)
    return result


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.writes = []
        self.expires = {}

    async def get(self, key):
        if self.expires.get(key, float("inf")) <= time.monotonic():
            self.values.pop(key, None)
        return self.values.get(key)

    async def set(self, key, value, *, ex=None, px=None, nx=False):
        if nx and await self.get(key) is not None:
            return False
        self.values[key] = value
        self.expires[key] = time.monotonic() + (px / 1000 if px else ex)
        if not nx:
            self.writes.append((key, value, ex))
        return True

    async def eval(self, script, count, *args):
        if script == retrieval_cache.RELEASE_LEASE:
            key, token = args
            if await self.get(key) == token:
                self.values.pop(key, None)
                return 1
            return 0
        key, lease, token, value, ttl = args
        if await self.get(lease) != token:
            return 0
        await self.set(key, value, ex=ttl)
        return 1


@pytest.mark.asyncio
async def test_hit_skips_retrieval_and_records_cache_not_model_usage():
    backend, client = retriever(), MemoryRedis()
    cache = CachedPolicyRetriever(backend, client)
    run = RunTelemetry("test")
    token = current_run.set(run)
    try:
        first = await cache.search(principal(), "退货")
        assert await cache.search(principal(), "退货") == first
    finally:
        current_run.reset(token)
    assert backend.search.await_count == 1
    assert [e["cache_status"] for e in run.events] == ["MISS", "HIT"]
    assert run.summary()["policy_cache_hits"] == 1
    assert run.summary()["model_calls"] == 0
    key, value, ttl = client.writes[0]
    assert "退货" not in key and "tenant-a" not in key
    assert json.loads(value) == [c.chunk_id for c in first]
    assert 540 <= ttl <= 660


@pytest.mark.asyncio
async def test_tenants_and_query_parameters_do_not_share_entries():
    backend, client = retriever(), MemoryRedis()
    cache = CachedPolicyRetriever(backend, client)
    first = await cache.search(principal(), "退货")
    other = await cache.search(principal("tenant-b"), "退货")
    await cache.search(principal(), "退货", limit=1)
    await cache.search(principal(), "退货政策")
    assert "private" in [c.chunk_id for c in first]
    assert [c.chunk_id for c in other] == ["public"]
    assert backend.search.await_count == 4
    assert len(client.values) == 4


@pytest.mark.asyncio
async def test_corpus_content_and_retrieval_configuration_invalidate_cache():
    client, backend = MemoryRedis(), retriever()
    cache = CachedPolicyRetriever(backend, client)
    await cache.search(principal(), "退货")
    changed_chunks = [replace(c, content=c.content + "新版") for c in backend.index.corpus.chunks]
    newer = retriever(changed_chunks)
    result = await CachedPolicyRetriever(newer, client).search(principal(), "退货")
    assert all(c.excerpt.endswith("新版") for c in result)
    assert newer.search.await_count == 1
    backend.min_similarity = 0.75
    await cache.search(principal(), "退货")
    backend.strategy = "keyword"
    await cache.search(principal(), "退货")
    assert len(client.values) == 4


@pytest.mark.asyncio
async def test_policy_boundary_invalidates_without_waiting_for_ttl(monkeypatch):
    now = datetime(2026, 9, 16, tzinfo=UTC)
    old = PolicyChunk(
        "s",
        "old",
        "退货",
        frozenset({"*"}),
        ("退货",),
        "旧规则",
        rule_id="return",
        expires_at=(now + timedelta(seconds=1)).isoformat(),
    )
    new = replace(
        old,
        chunk_id="new",
        content="新规则",
        expires_at=None,
        effective_at=(now + timedelta(seconds=1)).isoformat(),
    )
    future = replace(
        old,
        chunk_id="future",
        rule_id="new-rule",
        expires_at=None,
        effective_at=(now + timedelta(seconds=1)).isoformat(),
    )

    class Clock:
        @staticmethod
        def now(_):
            return now

    monkeypatch.setattr(retrieval_cache, "datetime", Clock)
    backend = retriever([old, new, future])
    cache = CachedPolicyRetriever(backend, MemoryRedis())
    assert [c.chunk_id for c in await cache.search(principal(), "退货")] == ["old"]
    now += timedelta(seconds=2)
    assert {c.chunk_id for c in await cache.search(principal(), "退货")} == {"new", "future"}
    assert backend.search.await_count == 2


@pytest.mark.asyncio
async def test_negative_cache_has_short_ttl():
    backend, client = retriever(), MemoryRedis()
    cache = CachedPolicyRetriever(backend, client)
    assert await cache.search(principal(), "不存在的规定xyz") == []
    assert await cache.search(principal(), "不存在的规定xyz") == []
    assert backend.search.await_count == 1
    assert 27 <= client.writes[0][2] <= 33


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "invalid-json",
        '{"id": "public"}',
        '["private"]',
        '["public", "public"]',
        "[12]",
        '["missing"]',
    ],
)
async def test_corrupt_or_unauthorized_cache_is_recomputed(raw):
    backend, client = retriever(), MemoryRedis()
    cache = CachedPolicyRetriever(backend, client)
    caller = principal("tenant-b")
    key = cache.cache_key(caller, "退货", 5, datetime.now(UTC))
    client.values[key] = raw
    result = await cache.search(caller, "退货")
    assert [c.chunk_id for c in result] == ["public"]
    assert backend.search.await_count == 1


@pytest.mark.asyncio
async def test_same_query_concurrency_coalesces_within_process():
    backend, client = retriever(), MemoryRedis()
    cache = CachedPolicyRetriever(backend, client)
    results = await asyncio.gather(*(cache.search(principal(), "退货") for _ in range(20)))
    assert all(result == results[0] for result in results)
    assert backend.search.await_count == 1


@pytest.mark.asyncio
async def test_redis_failure_cooldown_and_recovery():
    backend, client = retriever(), MemoryRedis()
    original_get = client.get
    client.get = AsyncMock(side_effect=RedisConnectionError("offline"))
    cache = CachedPolicyRetriever(backend, client)
    first = await cache.search(principal(), "退货")
    assert await cache.search(principal(), "退货") == first
    assert client.get.await_count == 1
    assert not client.writes
    client.get = original_get
    cache.unavailable_until = 0
    assert await cache.search(principal(), "退货") == first
    assert await cache.search(principal(), "退货") == first
    assert backend.search.await_count == 3


@pytest.mark.asyncio
async def test_slow_redis_and_write_failures_do_not_fail_retrieval():
    async def slow_get(_):
        await asyncio.sleep(60)

    backend, client = retriever(), MemoryRedis()
    client.get = slow_get
    cache = CachedPolicyRetriever(backend, client, timeout_seconds=0.01)
    async with asyncio.timeout(1):
        assert await cache.search(principal(), "退货")
    client = MemoryRedis()
    client.eval = AsyncMock(side_effect=RedisConnectionError("write failed"))
    assert await CachedPolicyRetriever(backend, client).search(principal(), "退货")


@pytest.mark.asyncio
async def test_retrieval_failure_is_not_cached_and_cancellation_propagates():
    backend, client = retriever(), MemoryRedis()
    backend.search.side_effect = TimeoutError("retrieval_capacity")
    cache = CachedPolicyRetriever(backend, client)
    with pytest.raises(TimeoutError):
        await cache.search(principal(), "退货")
    assert not client.writes
    client.get = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await cache.search(principal(), "退货")
    assert backend.search.await_count == 1


@pytest.fixture
def redis_url():
    name = f"ticketpilot-cache-test-{uuid4().hex[:10]}"
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            name,
            "-p",
            "127.0.0.1::6379",
            "redis:7.4-alpine",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    try:
        port = subprocess.check_output(["docker", "port", name, "6379"], text=True).strip()
        for _ in range(30):
            ready = subprocess.run(
                ["docker", "exec", name, "redis-cli", "ping"], capture_output=True, text=True
            )
            if "PONG" in ready.stdout:
                break
            time.sleep(0.1)
        else:
            pytest.fail("Redis did not become ready")
        yield f"redis://{port}/0"
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=True, capture_output=True)


@pytest.mark.asyncio
async def test_admission_limit_rejects_excess_without_leaking_slots():
    backend, client = retriever(), MemoryRedis()
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(*args, **kwargs):
        entered.set()
        await release.wait()
        return []

    backend.search = AsyncMock(side_effect=slow)
    cache = CachedPolicyRetriever(backend, client, max_inflight=2)
    first = asyncio.create_task(cache.search(principal(), "退货"))
    await entered.wait()
    second = asyncio.create_task(cache.search(principal(), "退货"))
    await asyncio.sleep(0)
    try:
        with pytest.raises(TimeoutError, match="policy_cache_capacity"):
            await cache.search(principal(), "退货")
        assert cache.inflight == 2
    finally:
        release.set()
        await asyncio.gather(first, second)
    assert cache.inflight == 0
    assert backend.search.await_count == 1


@pytest.mark.asyncio
async def test_queue_wait_is_bounded_and_cancelled_owner_releases_lease():
    backend, client = retriever(), MemoryRedis()
    entered = asyncio.Event()

    async def slow(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    backend.search = AsyncMock(side_effect=slow)
    cache = CachedPolicyRetriever(backend, client, wait_seconds=0.01)
    first = asyncio.create_task(cache.search(principal(), "退货"))
    await entered.wait()
    try:
        async with asyncio.timeout(1):
            with pytest.raises(TimeoutError):
                await cache.search(principal(), "退货")
        assert cache.inflight == 1
    finally:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    key = cache.cache_key(principal(), "退货", 5, datetime.now(UTC))
    assert await client.get(key + ":lease") is None
    assert cache.inflight == 0
    backend.search.side_effect = None
    backend.search.return_value = []
    assert await cache.search(principal(), "退货") == []


@pytest.mark.asyncio
async def test_busy_remote_owner_times_out_without_duplicate_computation():
    backend, client = retriever(), MemoryRedis()
    cache = CachedPolicyRetriever(backend, client, wait_seconds=0.02)
    key = cache.cache_key(principal(), "退货", 5, datetime.now(UTC))
    await client.set(key + ":lease", "other-owner", nx=True, px=10000)
    run = RunTelemetry("busy")
    token = current_run.set(run)
    try:
        async with asyncio.timeout(1):
            with pytest.raises(TimeoutError, match="policy_cache_fill_busy"):
                await cache.search(principal(), "退货")
    finally:
        current_run.reset(token)
    assert backend.search.await_count == 0
    assert run.events[-1]["cache_status"] == "FILL_BUSY"
    assert await client.get(key + ":lease") == "other-owner"


@pytest.mark.asyncio
async def test_lease_acquisition_failure_falls_back_without_publishing():
    backend, client = retriever(), MemoryRedis()
    client.set = AsyncMock(side_effect=RedisConnectionError("acquire offline"))
    cache = CachedPolicyRetriever(backend, client)
    assert await cache.search(principal(), "退货")
    assert backend.search.await_count == 1
    assert not client.writes


@pytest.mark.asyncio
async def test_ttl_jitter_spreads_expiry_and_negative_cache_expires_earlier(monkeypatch):
    client = MemoryRedis()
    cache = CachedPolicyRetriever(retriever(), client)
    jitter = iter([0.9, 1.1, 1.0])
    monkeypatch.setattr(retrieval_cache.random, "uniform", lambda *_: next(jitter))
    await cache.search(principal(), "退货 A")
    await cache.search(principal(), "退货 B")
    await cache.search(principal(), "不存在xyz")
    assert [ttl for _, _, ttl in client.writes] == [540, 660, 30]


@pytest.mark.docker
@pytest.mark.asyncio
async def test_real_expired_owner_cannot_publish_or_delete_new_lease(redis_url):
    async with create_cache_client(redis_url, 0.5) as client:
        cache = CachedPolicyRetriever(retriever(), client, lease_seconds=0.02)
        key = cache.cache_key(principal(), "退货", 5, datetime.now(UTC))
        assert await cache._acquire(key, "old")
        await asyncio.sleep(0.05)
        cache.lease_seconds = 10
        assert await cache._acquire(key, "new")
        assert await cache._write(key, [], "old") == "LEASE_LOST"
        await cache._release(key, "old")
        assert await client.get(key + ":lease") == "new"
        assert await client.get(key) is None
        assert await cache._write(key, [], "new") == "STORED"
        await cache._release(key, "new")
        assert await client.get(key + ":lease") is None


@pytest.mark.docker
@pytest.mark.asyncio
async def test_four_processes_merge_80_cold_requests_into_one_fill(redis_url):
    program = r"""
import asyncio, json, sys
from ticketpilot.policies import PolicyChunk, PolicyCorpus
from ticketpilot.retrieval import HybridPolicyRetriever, PolicySearchIndex
from ticketpilot.retrieval_cache import CachedPolicyRetriever, create_cache_client
from ticketpilot.schemas import RequestPrincipal
async def main():
    async with create_cache_client(sys.argv[1], 0.5) as redis:
        chunk = PolicyChunk('s', 'public', 'return', frozenset({'*'}), ('return',), 'return policy')
        backend = HybridPolicyRetriever(PolicySearchIndex(PolicyCorpus('test', '', (chunk,))), 'bm25')
        original = backend.search
        async def slow(*args, **kwargs):
            await redis.incr('fills')
            await asyncio.sleep(0.2)
            return await original(*args, **kwargs)
        backend.search = slow
        cache = CachedPolicyRetriever(backend, redis, timeout_seconds=0.5)
        caller = RequestPrincipal(tenant_id='t', actor_id='a', role='CUSTOMER')
        await redis.incr('ready')
        async with asyncio.timeout(30):
            while not await redis.get('start'):
                await asyncio.sleep(0.01)
        results = await asyncio.gather(*(cache.search(caller, 'return') for _ in range(20)))
        assert all([c.chunk_id for c in r] == ['public'] for r in results)
        print(json.dumps({'requests': len(results)}))
asyncio.run(main())
"""

    def worker():
        return subprocess.run(
            [sys.executable, "-c", program, redis_url],
            capture_output=True,
            text=True,
            timeout=60,
        )

    # Children need the same src import root that pytest config supplies to this process.
    env_path = str(Path(__file__).resolve().parents[2] / "src")
    program = f"import sys; sys.path.insert(0, {env_path!r})\n" + program
    tasks = [asyncio.create_task(asyncio.to_thread(worker)) for _ in range(4)]
    try:
        async with create_cache_client(redis_url, 0.5) as client:
            async with asyncio.timeout(30):
                while int(await client.get("ready") or 0) < 4:
                    await asyncio.sleep(0.05)
            await client.set("start", "1", ex=60)
            results = await asyncio.gather(*tasks)
            for result in results:
                assert result.returncode == 0, result.stderr
                assert json.loads(result.stdout)["requests"] == 20
            assert int(await client.get("fills")) == 1
    finally:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.docker
@pytest.mark.asyncio
async def test_real_redis_shared_hits_expiry_and_eviction(redis_url):
    first, second = retriever(), retriever()
    async with create_cache_client(redis_url, 0.5) as client:
        cache_a = CachedPolicyRetriever(first, client)
        cache_b = CachedPolicyRetriever(second, client)
        expected = await cache_a.search(principal(), "退货")
        assert await cache_b.search(principal(), "退货") == expected
        assert second.search.await_count == 0
        key = cache_a.cache_key(principal(), "退货", 5, datetime.now(UTC))
        assert 0 < await client.ttl(key) <= 660
        await client.pexpire(key, 20)
        await asyncio.sleep(0.05)
        assert await cache_b.search(principal(), "退货") == expected
        assert second.search.await_count == 1
        await client.delete(key)
        assert await cache_a.search(principal(), "退货") == expected
        assert first.search.await_count == 2


@pytest.mark.docker
@pytest.mark.asyncio
async def test_cache_events_persist_through_ticket_workflow(redis_url):
    from langgraph.checkpoint.memory import MemorySaver
    from test_workflow_graph import PolicyReasoner, cleanup_tenant, make_service

    from ticketpilot.db import apply_migrations, get_ticketpilot_pool
    from ticketpilot.domain import ProcessingResult
    from ticketpilot.graph import build_ticketpilot_graph
    from ticketpilot.schemas import CreateTicketRequest

    caller = principal(f"cache-workflow-{uuid4()}")
    async with get_ticketpilot_pool() as pool, create_cache_client(redis_url, 0.5) as client:
        await apply_migrations(pool)
        service = make_service(pool, build_ticketpilot_graph(MemorySaver()))
        service.reasoner = PolicyReasoner()
        service.reasoner.answer = AsyncMock(return_value="退货需审批。")
        service.policy_retriever = CachedPolicyRetriever(retriever(), client)
        try:
            for expected_status in ("MISS", "HIT"):
                result = await service.create_ticket(
                    caller,
                    CreateTicketRequest(subject="缓存验收", message="退货政策是什么"),
                    str(uuid4()),
                )
                assert result.ticket.processing_result == ProcessingResult.ANSWERED
                events = (await service.get_run_events(caller, result.run_id)).events
                cached = [e for e in events if e.event_type == "POLICY_CACHE"]
                assert len(cached) == 1
                assert cached[0].details["cache_status"] == expected_status
                assert not any(e.event_type == "MODEL_CALL" for e in events)
                summary = next(e.details for e in events if e.event_type == "RUN_TELEMETRY")
                assert summary["policy_cache_requests"] == 1
                assert summary["policy_cache_hits"] == int(expected_status == "HIT")
        finally:
            await cleanup_tenant(pool, caller.tenant_id)
