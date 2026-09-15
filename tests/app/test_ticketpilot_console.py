import json
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

DEMO_TOKENS = {
    "customer-token": {
        "tenant_id": "tenant-a",
        "actor_id": "customer-a",
        "role": "CUSTOMER",
    },
    "approver-token": {
        "tenant_id": "tenant-a",
        "actor_id": "approver-a",
        "role": "APPROVER",
    },
}

TICKET_ID = "11bb0b9a-b217-48fc-b540-9605fa56ab6d"
RUN_ID = "c2739e9f-f24f-4ce0-a2af-18a8efc1de27"
APPROVAL_ID = "6a9d1d1b-76c4-499d-8b58-4ce3da7e8f25"


@pytest.fixture
def demo_tokens(monkeypatch):
    monkeypatch.setenv("TICKETPILOT_AUTH_TOKENS", json.dumps(DEMO_TOKENS))


def _open_console() -> AppTest:
    at = AppTest.from_file("../../src/streamlit_app.py").run(timeout=10)
    at.sidebar.toggle(key="show_ticketpilot_console").set_value(True).run(timeout=10)
    return at


def test_console_is_opt_in_and_lists_env_identities(mock_agent_client, demo_tokens):
    at = AppTest.from_file("../../src/streamlit_app.py").run(timeout=10)

    assert not any("TicketPilot 售后工单工作台" in item.value for item in at.subheader)

    at.sidebar.toggle(key="show_ticketpilot_console").set_value(True).run(timeout=10)

    assert any("TicketPilot 售后工单工作台" in item.value for item in at.subheader)
    identity = at.selectbox(key="ticketpilot_identity")
    assert identity.options == ["客户 · customer-a", "审批员 · approver-a", "自定义令牌…"]
    assert identity.value == "客户 · customer-a"
    assert not at.exception


def test_console_falls_back_to_docker_demo_identities(mock_agent_client, monkeypatch):
    # An empty value keeps load_dotenv() from pulling the developer's real .env tokens in.
    monkeypatch.setenv("TICKETPILOT_AUTH_TOKENS", "")
    at = _open_console()

    identity = at.selectbox(key="ticketpilot_identity")
    assert identity.options[:2] == ["客户 · customer-demo-01", "审批员 · approver-demo-01"]
    assert not any(item.key == "ticketpilot_identity_source" for item in at.radio)
    assert not at.exception


def test_console_defaults_to_the_token_source_the_backend_accepts(mock_agent_client, demo_tokens):
    with patch("ticketpilot_streamlit.TicketPilotClient") as client_class:
        ticket_client = client_class.return_value
        ticket_client.probe_identity.side_effect = lambda token: (
            "ok" if token == "demo-customer-token" else "unauthorized"
        )
        at = _open_console()

    assert at.radio(key="ticketpilot_identity_source").value == "Docker 演示令牌"
    assert at.selectbox(key="ticketpilot_identity").value == "客户 · customer-demo-01"
    assert any("后端已接受该身份" in item.value for item in at.caption)
    assert not at.exception


def test_scenario_prefills_new_ticket_form(mock_agent_client, demo_tokens):
    at = _open_console()

    at.button(key="ticketpilot_scenario_vague_refund").click().run(timeout=10)

    assert at.text_input(key="ticketpilot_subject").value == "申请部分退款"
    assert at.text_area(key="ticketpilot_message").value == "订单 TP-0013 我想申请部分退款。"
    assert at.text_input(key="ticketpilot_order_reference").value == "TP-0013"
    assert not at.exception


def test_create_ticket_generates_key_and_renders_workbench(mock_agent_client, demo_tokens):
    run_result = {
        "ticket": {
            "id": TICKET_ID,
            "thread_id": "thread-a",
            "status": "RESOLVED",
            "subject": "查询物流",
        },
        "run_id": RUN_ID,
    }
    ticket_detail = {
        **run_result["ticket"],
        "processing_result": "ANSWERED",
        "category": "ORDER_STATUS",
        "risk_level": "READ_ONLY",
        "order_reference": "TP-0009",
        "order": {
            "order_reference": "TP-0009",
            "payment_status": "PAID",
            "fulfillment_status": "SHIPPED",
            "paid_amount": "9999.00",
            "refundable_amount": "9999.00",
            "currency": "CNY",
            "carrier": "JD Logistics",
            "tracking_number_masked": "TR******0009",
        },
        "messages": [
            {
                "id": "m-1",
                "run_id": RUN_ID,
                "role": "CUSTOMER",
                "content": "订单什么时候送到？",
                "citations": [],
                "created_at": "2026-09-06T12:00:00Z",
            },
            {
                "id": "m-2",
                "run_id": RUN_ID,
                "role": "AGENT",
                "content": "订单 TP-0009 已发货。",
                "citations": [
                    {
                        "source_id": "tp-demo-after-sales-v1",
                        "title": "订单物流与预计送达",
                        "chunk_id": "order-tracking",
                        "excerpt": "预计送达时间是估计值。",
                    }
                ],
                "created_at": "2026-09-06T12:00:01Z",
            },
        ],
        "pending_approval": None,
    }
    event_response = {
        "run_id": RUN_ID,
        "events": [
            {
                "event_type": "TICKET_TRIAGED",
                "outcome": "SUCCEEDED",
                "run_id": RUN_ID,
                "actor_type": "AGENT",
                "node_name": "classify_request",
                "occurred_at": "2026-09-06T12:00:00Z",
                "details": {
                    "category": "ORDER_STATUS",
                    "priority": "NORMAL",
                    "risk_level": "READ_ONLY",
                },
            },
            {
                "event_type": "TOOL_SUCCEEDED",
                "outcome": "SUCCEEDED",
                "run_id": RUN_ID,
                "actor_type": "AGENT",
                "tool_name": "query_order",
                "node_name": "query_order",
                "occurred_at": "2026-09-06T12:00:00.5Z",
                "details": {"found": True, "duration_ms": 8},
            },
        ],
    }

    with patch("ticketpilot_streamlit.TicketPilotClient") as client_class:
        ticket_client = client_class.return_value
        ticket_client.create_ticket.return_value = run_result
        ticket_client.get_ticket.return_value = ticket_detail
        ticket_client.get_run_events.return_value = event_response

        at = _open_console()
        at.text_input(key="ticketpilot_subject").set_value("查询物流")
        at.text_area(key="ticketpilot_message").set_value("订单什么时候送到？")
        at.button(key="ticketpilot_create").click().run(timeout=10)

    ticket_client.create_ticket.assert_called_once()
    call = ticket_client.create_ticket.call_args
    assert call.args == ("customer-token",)
    assert call.kwargs["subject"] == "查询物流"
    assert call.kwargs["message"] == "订单什么时候送到？"
    assert call.kwargs["order_reference"] is None
    assert call.kwargs["idempotency_key"].startswith("ui-create-")
    ticket_client.get_ticket.assert_called_with("customer-token", TICKET_ID)
    ticket_client.get_run_events.assert_called_with("customer-token", RUN_ID)
    assert at.text_area(key="ticketpilot_message").value == ""
    assert any("处理进度" in item.value for item in at.markdown)
    assert any("找到订单" in item.value for item in at.markdown)
    assert any("审计时间线" in item.value for item in at.markdown)
    assert any("TOOL_SUCCEEDED" in item.value for item in at.markdown)
    assert any("政策依据：订单物流与预计送达" in item.label for item in at.expander)
    bubbles = [item.markdown[0].value for item in at.chat_message]
    assert "订单什么时候送到？" in bubbles
    assert "订单 TP-0009 已发货。" in bubbles
    assert not at.exception


def test_retry_replays_last_request_with_same_key(mock_agent_client, demo_tokens):
    run_result = {
        "ticket": {"id": TICKET_ID, "thread_id": "thread-a", "status": "RESOLVED", "subject": "查"},
        "run_id": RUN_ID,
    }
    ticket_detail = {
        **run_result["ticket"],
        "processing_result": "ANSWERED",
        "messages": [],
        "pending_approval": None,
    }

    with patch("ticketpilot_streamlit.TicketPilotClient") as client_class:
        ticket_client = client_class.return_value
        ticket_client.create_ticket.return_value = run_result
        ticket_client.get_ticket.return_value = ticket_detail
        ticket_client.get_run_events.return_value = {"run_id": RUN_ID, "events": []}

        at = _open_console()
        at.text_input(key="ticketpilot_subject").set_value("查")
        at.text_area(key="ticketpilot_message").set_value("订单 TP-0009 在哪？")
        at.button(key="ticketpilot_create").click().run(timeout=10)
        at.button(key="ticketpilot_retry_last").click().run(timeout=10)

    first, second = ticket_client.create_ticket.call_args_list
    assert first == second
    assert first.kwargs["idempotency_key"].startswith("ui-create-")
    assert any("幂等验证通过" in item.value for item in at.success)
    assert not at.exception


def test_approval_is_gated_by_role_and_resumes_workflow(mock_agent_client, demo_tokens):
    pending_ticket = {
        "id": TICKET_ID,
        "thread_id": "thread-a",
        "status": "WAITING_APPROVAL",
        "processing_result": "WAITING_APPROVAL",
        "subject": "申请退款",
        "category": "REFUND",
        "risk_level": "HIGH_RISK_WRITE",
        "order_reference": "TP-0013",
        "order": {
            "order_reference": "TP-0013",
            "payment_status": "PAID",
            "fulfillment_status": "SHIPPED",
            "paid_amount": "416.00",
            "refundable_amount": "416.00",
            "currency": "CNY",
        },
        "messages": [],
        "pending_approval": {
            "id": APPROVAL_ID,
            "action_type": "REFUND",
            "status": "PENDING",
            "requested_at": "2026-09-06T12:00:00Z",
            "action_payload": {
                "amount": "100.00",
                "currency": "CNY",
                "order_reference": "TP-0013",
                "action_id": "9f1c2d3e-0000-0000-0000-000000000000",
            },
        },
    }
    resolved_ticket = {
        **pending_ticket,
        "status": "RESOLVED",
        "processing_result": "ANSWERED",
        "pending_approval": None,
    }
    approval_result = {
        "ticket": {
            "id": TICKET_ID,
            "thread_id": "thread-a",
            "status": "RESOLVED",
            "subject": "申请退款",
        },
        "run_id": RUN_ID,
    }
    event_response = {
        "run_id": RUN_ID,
        "events": [
            {
                "event_type": "REFUND_EXECUTED",
                "outcome": "SUCCEEDED",
                "run_id": RUN_ID,
                "actor_type": "SYSTEM",
                "tool_name": "execute_refund_mock",
                "occurred_at": "2026-09-06T12:00:00Z",
                "details": {"amount": "100.00", "remaining_refundable": "316.00"},
            }
        ],
    }

    with patch("ticketpilot_streamlit.TicketPilotClient") as client_class:
        ticket_client = client_class.return_value
        ticket_client.get_ticket.side_effect = [pending_ticket, resolved_ticket]
        ticket_client.decide_approval.return_value = approval_result
        ticket_client.get_run_events.return_value = event_response

        at = _open_console()
        at.text_input(key="ticketpilot_lookup_ticket_id").set_value(TICKET_ID)
        at.button(key="ticketpilot_refresh_ticket").click().run(timeout=10)

        assert any("待审批的退款动作" in item.value for item in at.markdown)
        assert any("不能审批" in item.value for item in at.info)
        assert not any(item.key == "ticketpilot_approve" for item in at.button)
        assert not any(item.key == "ticketpilot_add_message" for item in at.button)

        at.selectbox(key="ticketpilot_identity").select("审批员 · approver-a").run(timeout=10)
        at.text_input(key="ticketpilot_approval_reason").set_value("人工复核通过")
        at.button(key="ticketpilot_approve").click().run(timeout=10)

    ticket_client.decide_approval.assert_called_with(
        "approver-token",
        APPROVAL_ID,
        decision="APPROVE",
        reason="人工复核通过",
    )
    ticket_client.get_ticket.assert_called_with("approver-token", TICKET_ID)
    ticket_client.get_run_events.assert_called_with("approver-token", RUN_ID)
    assert any("REFUND_EXECUTED" in item.value for item in at.markdown)
    assert any("剩余可退 316.00" in item.value for item in at.markdown)
    progress = next(item.value for item in at.markdown if "**人工审批**" in item.value)
    assert "已批准并执行 Mock 退款 100.00" in progress
    assert "等待审批" not in progress
    assert not at.exception


def test_follow_up_uses_chip_and_generated_key(mock_agent_client, demo_tokens):
    resolved_ticket = {
        "id": TICKET_ID,
        "thread_id": "thread-a",
        "status": "RESOLVED",
        "processing_result": "ANSWERED",
        "subject": "申请退款",
        "category": "REFUND",
        "risk_level": "HIGH_RISK_WRITE",
        "order_reference": "TP-0013",
        "messages": [],
        "pending_approval": None,
    }
    follow_up_result = {
        "ticket": {
            "id": TICKET_ID,
            "thread_id": "thread-a",
            "status": "RESOLVED",
            "subject": "查询物流",
        },
        "run_id": RUN_ID,
    }

    with patch("ticketpilot_streamlit.TicketPilotClient") as client_class:
        ticket_client = client_class.return_value
        ticket_client.get_ticket.return_value = resolved_ticket
        ticket_client.add_message.return_value = follow_up_result
        ticket_client.get_run_events.return_value = {"run_id": RUN_ID, "events": []}

        at = _open_console()
        at.text_input(key="ticketpilot_lookup_ticket_id").set_value(TICKET_ID)
        at.button(key="ticketpilot_refresh_ticket").click().run(timeout=10)
        at.button(key="ticketpilot_chip_再次申请相同金额").click().run(timeout=10)
        assert (
            at.text_area(key="ticketpilot_followup_message").value
            == "订单 TP-0013 再申请退款 100 元。"
        )
        at.text_area(key="ticketpilot_followup_message").set_value("再查一次")
        at.button(key="ticketpilot_add_message").click().run(timeout=10)

    ticket_client.add_message.assert_called_once()
    call = ticket_client.add_message.call_args
    assert call.args == ("customer-token", TICKET_ID)
    assert call.kwargs["message"] == "再查一次"
    assert call.kwargs["idempotency_key"].startswith("ui-msg-")
    ticket_client.get_run_events.assert_called_with("customer-token", RUN_ID)
    assert at.text_area(key="ticketpilot_followup_message").value == ""
    assert not at.exception


def test_cross_tenant_lookup_explains_hidden_resource(mock_agent_client, demo_tokens):
    from client import TicketPilotClientError

    with patch("ticketpilot_streamlit.TicketPilotClient") as client_class:
        ticket_client = client_class.return_value
        ticket_client.get_ticket.side_effect = TicketPilotClientError(
            "Ticket was not found", code="RESOURCE_NOT_FOUND", status_code=404
        )

        at = _open_console()
        at.text_input(key="ticketpilot_lookup_ticket_id").set_value(TICKET_ID)
        at.button(key="ticketpilot_refresh_ticket").click().run(timeout=10)

    assert any("统一返回 404" in item.value for item in at.error)
    assert not at.exception
