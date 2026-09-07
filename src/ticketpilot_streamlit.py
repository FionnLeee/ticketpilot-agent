from typing import Any

import streamlit as st

from client import TicketPilotClient, TicketPilotClientError

TICKET_STATE_KEY = "ticketpilot_ticket_snapshot"
EVENTS_STATE_KEY = "ticketpilot_run_events"
RUN_IDS_STATE_KEY = "ticketpilot_run_ids"


def _error_text(error: TicketPilotClientError) -> str:
    return f"{error.code}: {error}" if error.code else str(error)


def _remember_run_result(result: dict[str, Any]) -> None:
    ticket = result.get("ticket")
    if isinstance(ticket, dict):
        st.session_state[TICKET_STATE_KEY] = ticket
    run_id = result.get("run_id")
    if isinstance(run_id, str):
        run_ids = list(st.session_state.get(RUN_IDS_STATE_KEY, []))
        if run_id not in run_ids:
            run_ids.append(run_id)
        st.session_state[RUN_IDS_STATE_KEY] = run_ids


def _remember_ticket(ticket: dict[str, Any]) -> None:
    st.session_state[TICKET_STATE_KEY] = ticket
    run_ids = list(st.session_state.get(RUN_IDS_STATE_KEY, []))
    for message in ticket.get("messages", []):
        run_id = message.get("run_id") if isinstance(message, dict) else None
        if isinstance(run_id, str) and run_id not in run_ids:
            run_ids.append(run_id)
    st.session_state[RUN_IDS_STATE_KEY] = run_ids


def _load_ticket(client: TicketPilotClient, token: str, ticket_id: str) -> None:
    ticket = client.get_ticket(token, ticket_id)
    _remember_ticket(ticket)


def _load_events(client: TicketPilotClient, token: str, run_id: str) -> None:
    st.session_state[EVENTS_STATE_KEY] = client.get_run_events(token, run_id)


def _render_ticket(ticket: dict[str, Any]) -> None:
    st.markdown("#### 当前工单")
    status, category, risk = st.columns(3)
    status.metric("状态", ticket.get("status", "-"))
    category.metric("分类", ticket.get("category") or "-")
    risk.metric("风险", ticket.get("risk_level") or "-")
    st.caption(
        f"Ticket `{ticket.get('id', '-')}` · Thread `{ticket.get('thread_id', '-')}`"
    )
    st.write(ticket.get("subject", ""))

    order = ticket.get("order")
    if isinstance(order, dict):
        st.markdown("**订单快照（已脱敏）**")
        st.json(order, expanded=False)

    messages = ticket.get("messages")
    if isinstance(messages, list) and messages:
        with st.expander(f"消息记录（{len(messages)}）", expanded=False):
            for message in messages:
                if not isinstance(message, dict):
                    continue
                st.markdown(f"**{message.get('role', 'UNKNOWN')}** · {message.get('content', '')}")


def _render_events(events_response: dict[str, Any]) -> None:
    run_id = events_response.get("run_id", "-")
    events = events_response.get("events", [])
    st.markdown(f"#### 审计时间线 · Run `{run_id}`")
    if not events:
        st.info("该运行暂时没有审计事件。")
        return

    icons = {
        "STARTED": "🔵",
        "SUCCEEDED": "✅",
        "BLOCKED": "⏸️",
        "FAILED": "❌",
    }
    for event in events:
        if not isinstance(event, dict):
            continue
        outcome = event.get("outcome", "UNKNOWN")
        with st.container(border=True):
            st.markdown(
                f"{icons.get(outcome, '•')} **{event.get('event_type', 'UNKNOWN')}** "
                f"`{outcome}`"
            )
            context = [event.get("occurred_at")]
            if event.get("node_name"):
                context.append(f"node={event['node_name']}")
            if event.get("tool_name"):
                context.append(f"tool={event['tool_name']}")
            if event.get("actor_id"):
                context.append(f"actor={event['actor_id']}")
            st.caption(" · ".join(str(item) for item in context if item))
            details = event.get("details")
            if isinstance(details, dict) and details:
                st.json(details, expanded=False)


def _render_approval(
    client: TicketPilotClient,
    ticket: dict[str, Any],
) -> None:
    approval = ticket.get("pending_approval")
    if not isinstance(approval, dict):
        return

    st.warning(
        f"等待审批：`{approval.get('action_type', 'UNKNOWN')}` · "
        f"Approval `{approval.get('id', '-')}`"
    )
    st.json(approval.get("action_payload", {}), expanded=False)
    approver_token = st.text_input(
        "审批令牌",
        type="password",
        key="ticketpilot_approver_token",
        help="必须属于同一 tenant 的 APPROVER 或 ADMIN。",
    )
    reason = st.text_input(
        "审批理由",
        value="本地演示人工复核",
        key="ticketpilot_approval_reason",
    )
    approve, reject = st.columns(2)
    decision = None
    if approve.button("批准并恢复", key="ticketpilot_approve", type="primary"):
        decision = "APPROVE"
    if reject.button("拒绝并恢复", key="ticketpilot_reject"):
        decision = "REJECT"
    if decision is None:
        return
    if not approver_token or not reason:
        st.error("审批令牌和审批理由不能为空。")
        return

    try:
        with st.spinner("提交审批并恢复原 LangGraph 运行..."):
            result = client.decide_approval(
                approver_token,
                str(approval["id"]),
                decision=decision,
                reason=reason,
            )
            _remember_run_result(result)
            ticket_id = str(result["ticket"]["id"])
            _load_ticket(client, approver_token, ticket_id)
            _load_events(client, approver_token, str(result["run_id"]))
        st.session_state["ticketpilot_notice"] = "审批已提交，工作流已恢复。"
        st.rerun()
    except (KeyError, TicketPilotClientError) as error:
        message = _error_text(error) if isinstance(error, TicketPilotClientError) else str(error)
        st.error(message)


def _render_message_form(
    client: TicketPilotClient,
    token: str,
    ticket: dict[str, Any],
) -> None:
    if ticket.get("status") not in {"RESOLVED", "FAILED"}:
        return
    with st.expander("向当前工单追加消息"):
        message = st.text_area("追加问题", key="ticketpilot_followup_message")
        idempotency_key = st.text_input(
            "Idempotency-Key",
            key="ticketpilot_message_idempotency_key",
            help="同一个键和同一正文可安全重试；换正文必须换键。",
        )
        if not st.button("追加并运行", key="ticketpilot_add_message"):
            return
        if not token or not message or not idempotency_key:
            st.error("操作令牌、追加问题和 Idempotency-Key 均不能为空。")
            return
        try:
            with st.spinner("追加消息并沿用原 thread 启动新 run..."):
                result = client.add_message(
                    token,
                    str(ticket["id"]),
                    message=message,
                    idempotency_key=idempotency_key,
                )
                _remember_run_result(result)
                _load_ticket(client, token, str(ticket["id"]))
                _load_events(client, token, str(result["run_id"]))
            st.session_state["ticketpilot_notice"] = "消息已追加，新运行已完成。"
            st.rerun()
        except (KeyError, TicketPilotClientError) as error:
            text = _error_text(error) if isinstance(error, TicketPilotClientError) else str(error)
            st.error(text)


def render_ticketpilot_console(base_url: str) -> None:
    st.subheader("🎫 TicketPilot 业务控制台")
    st.caption("业务 API 展示层：工单事实和审计来自 PostgreSQL，不读取 LangGraph checkpoint。")
    notice = st.session_state.pop("ticketpilot_notice", None)
    if notice:
        st.success(notice)
    client = TicketPilotClient(base_url)
    viewer_token = st.text_input(
        "操作/查询令牌",
        type="password",
        key="ticketpilot_viewer_token",
        help="创建工单可使用 CUSTOMER；查看可使用同 tenant 的 CUSTOMER、STAFF、APPROVER 或 ADMIN。",
    )

    with st.expander("创建演示工单", expanded=True):
        subject = st.text_input("主题", value="查询订单物流", key="ticketpilot_subject")
        message = st.text_area(
            "问题",
            value="我的订单什么时候送达？",
            key="ticketpilot_message",
        )
        order_reference = st.text_input(
            "订单号（可选）",
            placeholder="O-DEMO-0001",
            key="ticketpilot_order_reference",
        )
        if st.button("创建并运行", key="ticketpilot_create", type="primary"):
            if not viewer_token:
                st.error("请先填写操作令牌。")
            elif not subject or not message:
                st.error("主题和问题不能为空。")
            else:
                try:
                    with st.spinner("创建工单并执行 TicketPilot graph..."):
                        result = client.create_ticket(
                            viewer_token,
                            subject=subject,
                            message=message,
                            order_reference=order_reference or None,
                        )
                        _remember_run_result(result)
                        _load_ticket(client, viewer_token, str(result["ticket"]["id"]))
                        _load_events(client, viewer_token, str(result["run_id"]))
                    st.success("工单已创建。")
                except (KeyError, TicketPilotClientError) as error:
                    message = (
                        _error_text(error)
                        if isinstance(error, TicketPilotClientError)
                        else str(error)
                    )
                    st.error(message)

    st.markdown("#### 查询已有工单")
    ticket_id = st.text_input("Ticket ID", key="ticketpilot_lookup_ticket_id")
    if st.button("刷新工单", key="ticketpilot_refresh_ticket"):
        if not viewer_token or not ticket_id:
            st.error("查询令牌和 Ticket ID 不能为空。")
        else:
            try:
                _load_ticket(client, viewer_token, ticket_id)
            except TicketPilotClientError as error:
                st.error(_error_text(error))

    ticket = st.session_state.get(TICKET_STATE_KEY)
    if isinstance(ticket, dict):
        _render_ticket(ticket)
        _render_approval(client, ticket)
        _render_message_form(client, viewer_token, ticket)

    run_ids = list(st.session_state.get(RUN_IDS_STATE_KEY, []))
    st.markdown("#### 查询运行事件")
    selected_run = st.selectbox(
        "已知 Run ID",
        options=run_ids,
        index=len(run_ids) - 1 if run_ids else None,
        placeholder="创建或加载工单后自动出现",
        key="ticketpilot_selected_run",
    )
    manual_run = st.text_input("或手动输入 Run ID", key="ticketpilot_manual_run_id")
    if st.button("加载审计事件", key="ticketpilot_load_events"):
        run_id = manual_run or selected_run
        if not viewer_token or not run_id:
            st.error("查询令牌和 Run ID 不能为空。")
        else:
            try:
                _load_events(client, viewer_token, str(run_id))
            except TicketPilotClientError as error:
                st.error(_error_text(error))

    events = st.session_state.get(EVENTS_STATE_KEY)
    if isinstance(events, dict):
        _render_events(events)
