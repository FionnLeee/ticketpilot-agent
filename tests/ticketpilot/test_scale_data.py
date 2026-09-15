from dataclasses import asdict
from pathlib import Path

import pytest

from ticketpilot.policies import LocalPolicyRetriever, load_policy_corpus
from ticketpilot.scale_data import describe_orders, generate_scale_orders
from ticketpilot.schemas import RequestPrincipal


def test_scale_data_reproducible_and_valid():
    orders = generate_scale_orders()
    assert [asdict(o) for o in orders] == [asdict(o) for o in generate_scale_orders()]
    summary = describe_orders(orders)
    assert summary["order_count"] == 10000
    assert summary["tenant_customer_count"] == 1000
    assert len({o.id for o in orders}) == 10000
    assert len({(o.tenant_id, o.order_reference) for o in orders}) == 10000
    assert all(0 <= o.refundable_amount <= o.paid_amount for o in orders)
    assert all(o.tenant_id.startswith("tenant-scale-") for o in orders)
    assert len({o.paid_amount for o in orders}) > 1000
    assert len(summary["payment_distribution"]) == 5
    assert len(summary["fulfillment_distribution"]) == 5


@pytest.mark.asyncio
async def test_expanded_policy_topics_and_tenant_isolation():
    path = Path(__file__).resolve().parents[2] / "data/ticketpilot/scale_policy_manifest.json"
    corpus = load_policy_corpus(path)
    assert len({chunk.chunk_id for chunk in corpus.chunks}) == 24
    retriever = LocalPolicyRetriever(corpus)
    for tenant, expected in [
        ("tenant-scale-01", {"scale-tenant1"}),
        ("tenant-scale-02", {"scale-tenant2"}),
        ("tenant-scale-03", set()),
    ]:
        result = await retriever.search(
            RequestPrincipal(tenant_id=tenant, actor_id="staff", role="STAFF"), "专属客服"
        )
        assert {item.chunk_id for item in result} == expected
