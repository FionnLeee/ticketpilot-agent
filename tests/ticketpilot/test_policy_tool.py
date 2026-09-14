import asyncio
import json

import pytest
from langgraph.prebuilt import ToolRuntime

from ticketpilot.domain import PrincipalRole
from ticketpilot.policies import (
    DEFAULT_POLICY_MANIFEST_PATH,
    LocalPolicyRetriever,
    PolicyRetriever,
    load_policy_corpus,
)
from ticketpilot.schemas import Citation, RequestPrincipal
from ticketpilot.tools import TicketPilotContext, search_policy, search_policy_func


class UnusedOrderReader:
    async def get_by_reference(self, principal, order_reference):
        raise AssertionError("Order reader should not be called by policy search")


class SlowPolicyRetriever:
    async def search(
        self, principal: RequestPrincipal, query: str, limit: int = 3
    ) -> list[Citation]:
        await asyncio.sleep(0.05)
        return []


def make_runtime(
    principal: RequestPrincipal,
    policy_retriever: PolicyRetriever,
    timeout: float = 1,
) -> ToolRuntime:
    return ToolRuntime(
        state={},
        context=TicketPilotContext(
            principal=principal,
            order_reader=UnusedOrderReader(),
            policy_retriever=policy_retriever,
            policy_timeout_seconds=timeout,
        ),
        config={},
        stream_writer=lambda _: None,
        tool_call_id=None,
        store=None,
    )


@pytest.fixture
def customer() -> RequestPrincipal:
    return RequestPrincipal(
        tenant_id="tenant-demo-01",
        actor_id="customer-demo-01",
        role=PrincipalRole.CUSTOMER,
    )


def test_policy_corpus_manifest_and_hash_are_valid() -> None:
    corpus = load_policy_corpus()

    assert corpus.dataset_id == "ticketpilot-demo-policy-v1"
    assert len(corpus.chunks) == 6
    assert len({chunk.chunk_id for chunk in corpus.chunks}) == 6


def test_policy_corpus_hash_survives_crlf_checkout(tmp_path) -> None:
    manifest_path = DEFAULT_POLICY_MANIFEST_PATH
    corpus_relative = json.loads(manifest_path.read_text(encoding="utf-8"))["corpus_path"]
    source = manifest_path.parent / corpus_relative
    target = tmp_path / corpus_relative
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    (tmp_path / manifest_path.name).write_bytes(manifest_path.read_bytes())

    corpus = load_policy_corpus(tmp_path / manifest_path.name)

    assert len(corpus.chunks) == 6


@pytest.mark.asyncio
async def test_local_policy_retriever_returns_ranked_fixed_citations(customer) -> None:
    retriever = LocalPolicyRetriever()

    evidence = await retriever.search(customer, "退款金额和退款审批")

    assert [item.chunk_id for item in evidence] == [
        "refund-eligibility",
        "refund-approval",
    ]
    assert all(item.excerpt for item in evidence)


@pytest.mark.asyncio
async def test_policy_tool_hides_runtime_and_classifies_empty_and_timeout(customer) -> None:
    retriever = LocalPolicyRetriever()

    no_match = await search_policy_func("完全无关的话题", make_runtime(customer, retriever))
    timed_out = await search_policy_func(
        "退款",
        make_runtime(customer, SlowPolicyRetriever(), timeout=0.001),
    )

    assert search_policy.name == "search_policy"
    assert set(search_policy.tool_call_schema.model_json_schema()["properties"]) == {"query"}
    assert no_match["error_code"] == "NO_RELEVANT_POLICY"
    assert timed_out["error_code"] == "DEPENDENCY_TIMEOUT"
