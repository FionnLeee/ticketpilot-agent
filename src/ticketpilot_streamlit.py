"""TicketPilot demo workbench rendered inside the Streamlit app.

Only the stable ``/v1`` business API is used; LangGraph checkpoints are never read here.
Identity tokens stay server-side in the Streamlit process — the browser only sees labels.
"""

import json
import os
from datetime import datetime
from typing import Any
from uuid import uuid4

import streamlit as st

from client import TicketPilotClient, TicketPilotClientError

TICKET_STATE_KEY = "ticketpilot_ticket_snapshot"
EVENTS_STATE_KEY = "ticketpilot_run_events"
TICKET_RUNS_KEY = "ticketpilot_ticket_runs"
SESSION_TICKETS_KEY = "ticketpilot_session_tickets"
LAST_REQUEST_KEY = "ticketpilot_last_request"
RETRY_RESULT_KEY = "ticketpilot_retry_result"
NOTICE_KEY = "ticketpilot_notice"
FORM_RESET_KEY = "ticketpilot_reset_form"
FOLLOWUP_RESET_KEY = "ticketpilot_reset_followup"
PREFILL_KEY = "ticketpilot_prefill"
BACKEND_INFO_KEY = "ticketpilot_backend_info"
IDENTITY_PROBES_KEY = "ticketpilot_identity_probes"

CUSTOM_IDENTITY = "自定义令牌…"
SOURCE_ENV = "本地 .env 令牌"
SOURCE_DOCKER = "Docker 演示令牌"

FORM_DEFAULTS = {
    "ticketpilot_subject": "",
    "ticketpilot_message": "",
    "ticketpilot_order_reference": "",
}

ROLE_LABELS = {"CUSTOMER": "客户", "STAFF": "客服", "APPROVER": "审批员", "ADMIN": "管理员"}
STATUS_LABELS = {
    "NEW": "新建",
    "PROCESSING": "处理中",
    "WAITING_INFORMATION": "等待补充信息",
    "WAITING_APPROVAL": "等待人工审批",
    "RESOLVED": "已解决",
    "FAILED": "未成功",
}
STATUS_COLORS = {
    "NEW": "gray",
    "PROCESSING": "blue",
    "WAITING_INFORMATION": "orange",
    "WAITING_APPROVAL": "orange",
    "RESOLVED": "green",
    "FAILED": "red",
}
STATUS_ICONS = {
    "NEW": "🆕",
    "PROCESSING": "⏳",
    "WAITING_INFORMATION": "🟡",
    "WAITING_APPROVAL": "⏸️",
    "RESOLVED": "✅",
    "FAILED": "❌",
}
RESULT_LABELS = {
    "ANSWERED": ("green", "已基于证据回答"),
    "NEEDS_INPUT": ("orange", "需要客户补充信息"),
    "WAITING_APPROVAL": ("orange", "退款动作等待人工审批"),
    "DEPENDENCY_FAILED": ("red", "外部依赖失败，可稍后重试"),
    "INSUFFICIENT_EVIDENCE": ("red", "证据不足，需要人工核查"),
    "PROCESSING_FAILED": ("red", "处理失败"),
}
RESULT_MESSAGES = {
    "ANSWERED": (st.success, "本轮已基于订单事实和政策证据完成回答。"),
    "NEEDS_INPUT": (st.info, "本轮尚未解决：系统没有猜测缺失信息，正在等待客户补充。"),
    "WAITING_APPROVAL": (st.warning, "本轮尚未完成：退款提案已保存，工作流暂停在人工审批节点。"),
    "DEPENDENCY_FAILED": (
        st.error,
        "本轮未解决：外部依赖不可用。这不是“订单不存在”，可以稍后重试。",
    ),
    "INSUFFICIENT_EVIDENCE": (st.warning, "本轮未解决：没有可引用的政策证据，系统拒绝编造结论。"),
    "PROCESSING_FAILED": (st.error, "本轮处理失败，需要重试或人工处理。"),
}
CATEGORY_LABELS = {
    "ORDER_STATUS": "订单/物流查询",
    "REFUND": "退款申请",
    "POLICY": "政策咨询",
    "OTHER": "其他",
}
RISK_LABELS = {"READ_ONLY": "只读", "HIGH_RISK_WRITE": "高风险写操作"}
PAYMENT_LABELS = {
    "PENDING": "待支付",
    "PAID": "已支付",
    "PARTIALLY_REFUNDED": "部分退款",
    "REFUNDED": "已全额退款",
    "CANCELLED": "已取消",
}
FULFILLMENT_LABELS = {
    "PENDING": "待处理",
    "PROCESSING": "处理中",
    "SHIPPED": "已发货",
    "DELIVERED": "已送达",
    "CANCELLED": "已取消",
}
ACTOR_LABELS = {"CUSTOMER": "客户", "AGENT": "Agent", "SYSTEM": "系统", "STAFF": "人工"}
TOOL_LABELS = {
    "query_order": "订单查询",
    "search_policy": "政策检索",
    "execute_refund_mock": "Mock 退款执行",
}
EVENT_LABELS = {
    "TICKET_CREATED": "工单已创建",
    "MESSAGE_ADDED": "客户追加消息",
    "RUN_STARTED": "开始处理",
    "TICKET_TRIAGED": "意图分类完成",
    "ORDER_LINKED": "工单绑定订单",
    "TOOL_SUCCEEDED": "工具调用成功",
    "TOOL_FAILED": "工具调用失败",
    "TICKET_RESOLVED": "工单已解决",
    "TICKET_INFORMATION_REQUESTED": "请求客户补充信息",
    "DEPENDENCY_FAILED": "外部依赖失败",
    "INSUFFICIENT_EVIDENCE": "证据不足",
    "RUN_FAILED": "处理失败",
    "REFUND_APPROVAL_REQUESTED": "退款提案已保存，等待人工审批",
    "REFUND_APPROVAL_REPLAYED": "同一退款动作重放，复用原审批",
    "APPROVAL_DECIDED": "人工审批已决定",
    "APPROVAL_DECISION_REPLAYED": "重复审批请求被安全重放",
    "REFUND_EXECUTED": "Mock 退款已执行",
    "REFUND_REJECTED": "退款已被拒绝",
    "REFUND_EXECUTION_BLOCKED": "退款执行被阻断（执行前复验失败）",
}
OUTCOME_ICONS = {"STARTED": "🔵", "SUCCEEDED": "✅", "BLOCKED": "⏸️", "FAILED": "❌"}
REASONER_LABELS = {
    "llm": "真实模型",
    "deterministic_demo": "确定性演示（不调用模型）",
}

DEFAULT_IDENTITIES = [
    {
        "label": "客户 · customer-demo-01",
        "token": "demo-customer-token",
        "role": "CUSTOMER",
        "tenant_id": "tenant-demo-01",
        "actor_id": "customer-demo-01",
    },
    {
        "label": "审批员 · approver-demo-01",
        "token": "demo-approver-token",
        "role": "APPROVER",
        "tenant_id": "tenant-demo-01",
        "actor_id": "approver-demo-01",
    },
    {
        "label": "其他租户审批员 · tenant-demo-02",
        "token": "other-tenant-token",
        "role": "APPROVER",
        "tenant_id": "tenant-demo-02",
        "actor_id": "approver-demo-02",
    },
]

SCENARIOS = [
    {
        "key": "logistics",
        "title": "📦 查询物流",
        "subject": "查询订单物流",
        "message": "请查询订单 TP-0009 的物流状态和预计送达时间。",
        "order_reference": "TP-0009",
        "expect": "只读查询：订单 Tool → 脱敏运单 → 带政策引用回答",
    },
    {
        "key": "policy",
        "title": "📖 咨询退款政策",
        "subject": "咨询退款政策",
        "message": "你们的退款政策是什么？退款金额有没有上限？",
        "order_reference": "",
        "expect": "政策咨询：只检索政策并引用，不创建退款",
    },
    {
        "key": "denied",
        "title": "🚫 提到退款但不退款",
        "subject": "只查物流，不退款",
        "message": "我不需要退款，只想查一下订单 TP-0013 的物流到哪了。",
        "order_reference": "TP-0013",
        "expect": "否定退款：识别为物流查询，不生成退款提案",
    },
    {
        "key": "vague_refund",
        "title": "🌗 模糊退款（不说金额）",
        "subject": "申请部分退款",
        "message": "订单 TP-0013 我想退一部分钱。",
        "order_reference": "TP-0013",
        "expect": "缺金额：进入等待补充，不默认全额退款",
    },
    {
        "key": "refund_100",
        "title": "💴 明确退款 100 元",
        "subject": "申请退款 100 元",
        "message": "订单 TP-0013 申请退款 100 元。",
        "order_reference": "TP-0013",
        "expect": "明确金额：生成退款提案，暂停等待人工审批",
    },
]

FOLLOWUP_CHIPS = {
    "WAITING_INFORMATION": [
        ("补充金额：100 元", "100 元"),
        ("改为全额退款", "我要申请全额退款。"),
        ("取消退款", "算了，不退了。"),
    ],
    "RESOLVED": [
        ("再次申请相同金额", "订单 {order} 再申请退款 100 元。"),
        ("确认当前物流", "请再确认一下订单 {order} 现在的物流状态。"),
    ],
    "FAILED": [("重试同一问题", "请再查一次订单 {order} 的状态。")],
}

WORKBENCH_CSS = """
<style>
[data-testid="stMainBlockContainer"], .block-container {
    max-width: 1320px;
    padding-top: 1.5rem;
}
[data-testid="stSidebar"] .stButton button { text-align: left; justify-content: flex-start; }
</style>
"""


def _error_text(error: TicketPilotClientError) -> str:
    hints = {
        "UNAUTHORIZED": "当前后端不接受这个身份令牌。请确认页面连接的后端与令牌来源一致（本地 .env 或 Docker 演示）。",
        "FORBIDDEN": "当前身份没有权限执行这个操作，请在左侧切换身份。",
        "RESOURCE_NOT_FOUND": "在当前身份的可见范围内找不到该资源。跨租户、跨客户访问会统一返回 404，不泄露资源是否存在。",
        "DEPENDENCY_UNAVAILABLE": "后端未启用 TicketPilot 或数据库不可用。",
    }
    hint = hints.get(error.code or "")
    prefix = f"{error.code}: {error}" if error.code else str(error)
    return f"{prefix}\n\n{hint}" if hint else prefix


def _show_error(error: Exception) -> None:
    if isinstance(error, TicketPilotClientError):
        st.error(_error_text(error))
    else:
        st.error(str(error))


def _short(value: Any, length: int = 8) -> str:
    text = str(value or "")
    return text[:length] if text else "-"


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_time(value: Any) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        return str(value or "")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%H:%M:%S")


def _format_amount(value: Any, currency: Any = None) -> str:
    if value is None or value == "":
        return "-"
    try:
        text = f"{float(value):,.2f}"
    except (TypeError, ValueError):
        text = str(value)
    return f"{text} {currency}" if currency else text


def _badge(text: str, color: str) -> str:
    return f":{color}-badge[{text}]"


# --- identities -----------------------------------------------------------------


def _env_identities() -> list[dict[str, Any]]:
    raw = os.getenv("TICKETPILOT_AUTH_TOKENS")
    if not raw:
        return []
    try:
        mapping = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(mapping, dict):
        return []
    identities: list[dict[str, Any]] = []
    tenants = {str(entry.get("tenant_id")) for entry in mapping.values() if isinstance(entry, dict)}
    for token, entry in mapping.items():
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role", ""))
        actor = str(entry.get("actor_id", ""))
        tenant = str(entry.get("tenant_id", ""))
        label = f"{ROLE_LABELS.get(role, role)} · {actor}"
        if len(tenants) > 1:
            label += f" · {tenant}"
        identities.append(
            {
                "label": label,
                "token": str(token),
                "role": role,
                "tenant_id": tenant,
                "actor_id": actor,
            }
        )
    return identities


def _select_identity() -> dict[str, Any] | None:
    env_identities = _env_identities()
    if env_identities:
        source = st.radio(
            "令牌来源",
            [SOURCE_ENV, SOURCE_DOCKER],
            horizontal=True,
            key="ticketpilot_identity_source",
            help="本地 `run_service.py` 读取 .env 里的令牌；`docker compose` 演示后端使用 demo-*-token。两套令牌互不通用。",
        )
        identities = env_identities if source == SOURCE_ENV else DEFAULT_IDENTITIES
    else:
        identities = DEFAULT_IDENTITIES
    labels = [item["label"] for item in identities] + [CUSTOM_IDENTITY]
    chosen = st.selectbox("演示身份", labels, key="ticketpilot_identity")
    if chosen == CUSTOM_IDENTITY:
        token = st.text_input(
            "自定义令牌",
            type="password",
            key="ticketpilot_custom_token",
            help="直接填写后端 TICKETPILOT_AUTH_TOKENS 中的某一个 token。",
        )
        if not token:
            return None
        return {"label": CUSTOM_IDENTITY, "token": token, "role": None}
    for item in identities:
        if item["label"] == chosen:
            return item
    return None


def _probe_identity(client: TicketPilotClient, token: str) -> Any:
    probes = st.session_state.setdefault(IDENTITY_PROBES_KEY, {})
    if token not in probes:
        probes[token] = client.probe_identity(token)
    return probes[token]


def _render_identity_status(client: TicketPilotClient, identity: dict[str, Any] | None) -> None:
    if identity is None:
        st.info("请选择或填写一个身份令牌。")
        return
    role = identity.get("role")
    if role:
        st.caption(
            f"角色 **{ROLE_LABELS.get(role, role)}** · 租户 `{identity.get('tenant_id', '-')}` · "
            f"操作者 `{identity.get('actor_id', '-')}`"
        )
    probe = _probe_identity(client, identity["token"])
    if probe == "ok":
        st.caption("✅ 后端已接受该身份")
    elif probe == "unauthorized":
        st.error(
            "❌ 后端不接受这个令牌（401）。本地 `.env` 令牌只对本地后端有效，"
            "`demo-*-token` 只对 Docker 演示后端有效；请切换令牌来源，并确认 8080 只运行了一个后端。"
        )
    elif probe == "unreachable":
        st.error("⚠️ 无法连接后端，请确认 8080 端口的服务已启动。")
    elif probe == "forbidden":
        st.warning("⚠️ 令牌有效，但该角色不能读取工单。")
    if st.button("重新校验身份", key="ticketpilot_reprobe"):
        st.session_state.pop(IDENTITY_PROBES_KEY, None)
        st.rerun()


# --- session state helpers ------------------------------------------------------


def _ticket_runs(ticket_id: str) -> list[str]:
    runs = st.session_state.setdefault(TICKET_RUNS_KEY, {})
    return runs.setdefault(ticket_id, [])


def _remember_run(ticket_id: str, run_id: Any) -> None:
    if isinstance(run_id, str) and run_id not in _ticket_runs(ticket_id):
        _ticket_runs(ticket_id).append(run_id)


def _remember_ticket(ticket: dict[str, Any]) -> None:
    st.session_state[TICKET_STATE_KEY] = ticket
    ticket_id = str(ticket.get("id", ""))
    for message in ticket.get("messages", []) or []:
        if isinstance(message, dict):
            _remember_run(ticket_id, message.get("run_id"))
    tickets = st.session_state.setdefault(SESSION_TICKETS_KEY, {})
    tickets[ticket_id] = {
        "subject": ticket.get("subject", ""),
        "status": ticket.get("status", ""),
        "processing_result": ticket.get("processing_result"),
        "order_reference": ticket.get("order_reference"),
    }


def _remember_run_result(result: dict[str, Any]) -> str:
    ticket = result.get("ticket")
    ticket_id = str(ticket.get("id", "")) if isinstance(ticket, dict) else ""
    _remember_run(ticket_id, result.get("run_id"))
    return ticket_id


def _sync_ticket(client: TicketPilotClient, token: str, ticket_id: str) -> None:
    ticket = client.get_ticket(token, ticket_id)
    _remember_ticket(ticket)
    events = st.session_state.setdefault(EVENTS_STATE_KEY, {})
    for run_id in list(_ticket_runs(ticket_id)):
        try:
            response = client.get_run_events(token, run_id)
        except TicketPilotClientError:
            continue
        run_events = response.get("events", []) if isinstance(response, dict) else []
        events[run_id] = run_events if isinstance(run_events, list) else []


def _merged_events(ticket_id: str) -> list[dict[str, Any]]:
    events = st.session_state.get(EVENTS_STATE_KEY, {})
    merged: list[tuple[Any, int, dict[str, Any]]] = []
    order = 0
    for run_id in _ticket_runs(ticket_id):
        for event in events.get(run_id, []):
            if isinstance(event, dict):
                parsed = _parse_time(event.get("occurred_at"))
                merged.append((parsed.timestamp() if parsed else 0.0, order, event))
                order += 1
    merged.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in merged]


def _current_ticket() -> dict[str, Any] | None:
    ticket = st.session_state.get(TICKET_STATE_KEY)
    return ticket if isinstance(ticket, dict) else None


def _open_ticket(client: TicketPilotClient, token: str, ticket_id: str) -> None:
    try:
        with st.spinner("加载工单与审计时间线…"):
            _sync_ticket(client, token, ticket_id)
        st.session_state.pop(RETRY_RESULT_KEY, None)
        st.rerun()
    except TicketPilotClientError as error:
        _show_error(error)


# --- sidebar --------------------------------------------------------------------


def _render_backend_info(client: TicketPilotClient, base_url: str) -> None:
    info = st.session_state.get(BACKEND_INFO_KEY)
    if info is None:
        try:
            info = client.get_info()
        except TicketPilotClientError:
            info = {}
        st.session_state[BACKEND_INFO_KEY] = info
    model = info.get("default_model", "未知") if isinstance(info, dict) else "未知"
    mode = info.get("ticketpilot_reasoner_mode") if isinstance(info, dict) else None
    mode_label = REASONER_LABELS.get(str(mode), str(mode) if mode else "未知")
    st.caption(f"后端 `{base_url}`")
    st.caption(f"模型 `{model}` · 推理器 {mode_label}")


def _render_sidebar(client: TicketPilotClient, base_url: str) -> dict[str, Any] | None:
    with st.sidebar:
        st.markdown("### 🎫 TicketPilot 工作台")
        st.caption("演示环境 · 合成数据 · Mock 退款 · 身份切换仅用于本地演示，不是生产登录。")

        st.markdown("**演示身份**")
        identity = _select_identity()
        _render_identity_status(client, identity)

        st.markdown("**演示场景**")
        st.caption("点击后自动填好新工单表单，再按“创建并运行”。")
        for scenario in SCENARIOS:
            if st.button(
                scenario["title"],
                key=f"ticketpilot_scenario_{scenario['key']}",
                help=scenario["expect"],
                width="stretch",
            ):
                st.session_state[PREFILL_KEY] = {
                    "ticketpilot_subject": scenario["subject"],
                    "ticketpilot_message": scenario["message"],
                    "ticketpilot_order_reference": scenario["order_reference"],
                }
                st.session_state.pop(TICKET_STATE_KEY, None)
                st.session_state.pop(RETRY_RESULT_KEY, None)
                st.rerun()
        if st.button("＋ 空白新工单", key="ticketpilot_new_blank", width="stretch"):
            st.session_state[FORM_RESET_KEY] = True
            st.session_state.pop(TICKET_STATE_KEY, None)
            st.session_state.pop(RETRY_RESULT_KEY, None)
            st.rerun()

        st.markdown("**本次会话的工单**")
        tickets = st.session_state.get(SESSION_TICKETS_KEY, {})
        current = _current_ticket()
        current_id = str(current.get("id")) if current else None
        if not tickets:
            st.caption("还没有工单。选择一个场景开始。")
        for ticket_id, summary in reversed(list(tickets.items())):
            status = summary.get("status", "")
            label = f"{STATUS_ICONS.get(status, '•')} {summary.get('subject') or ticket_id[:8]}"
            if st.button(
                label,
                key=f"ticketpilot_open_{ticket_id}",
                type="primary" if ticket_id == current_id else "secondary",
                help=f"{STATUS_LABELS.get(status, status)} · Ticket {ticket_id[:8]}",
                width="stretch",
            ):
                if identity is None:
                    st.error("请先选择身份。")
                else:
                    _open_ticket(client, identity["token"], ticket_id)
        with st.expander("按 Ticket ID 打开"):
            ticket_id = st.text_input("Ticket ID", key="ticketpilot_lookup_ticket_id")
            if st.button("打开工单", key="ticketpilot_refresh_ticket"):
                if identity is None or not ticket_id:
                    st.error("身份和 Ticket ID 不能为空。")
                else:
                    _open_ticket(client, identity["token"], ticket_id.strip())

        with st.expander("这个页面在演示什么"):
            st.markdown(
                "- **入口隔离**：只暴露 `/v1` 业务 API，其他租户看到的是 404\n"
                "- **意图边界**：提到退款 ≠ 申请退款；缺金额 ≠ 全额退款\n"
                "- **运行归属**：一张工单同一时刻只有一个 active run\n"
                "- **两层幂等**：同一请求重试复用原 run；同金额新消息是新动作\n"
                "- **结果语义**：区分已回答、等待补充、等待审批、依赖失败、证据不足"
            )
        _render_backend_info(client, base_url)
    return identity


# --- main area: ticket header, chat, context ------------------------------------


def _render_header(ticket: dict[str, Any]) -> None:
    status = str(ticket.get("status", ""))
    result = ticket.get("processing_result")
    badges = [_badge(STATUS_LABELS.get(status, status or "-"), STATUS_COLORS.get(status, "gray"))]
    if result in RESULT_LABELS:
        color, label = RESULT_LABELS[result]
        badges.append(_badge(label, color))
    category = ticket.get("category")
    if category:
        badges.append(_badge(CATEGORY_LABELS.get(category, category), "blue"))
    risk = ticket.get("risk_level")
    if risk:
        badges.append(
            _badge(RISK_LABELS.get(risk, risk), "red" if risk == "HIGH_RISK_WRITE" else "gray")
        )
    st.markdown(f"### {ticket.get('subject', '工单')}")
    st.markdown(" ".join(badges))
    if result in RESULT_MESSAGES:
        renderer, message = RESULT_MESSAGES[result]
        renderer(message)
    elif status == "PROCESSING":
        st.info("工单正在处理中。")


def _render_citations(citations: Any) -> None:
    if not isinstance(citations, list) or not citations:
        return
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        title = citation.get("title") or citation.get("source_id") or "政策依据"
        with st.expander(f"📎 政策依据：{title}"):
            if citation.get("excerpt"):
                st.write(citation["excerpt"])
            st.caption(
                f"来源 `{citation.get('source_id', '-')}` · 片段 `{citation.get('chunk_id', '-')}`"
            )


def _render_conversation(ticket: dict[str, Any]) -> None:
    messages = ticket.get("messages")
    if not isinstance(messages, list) or not messages:
        st.caption("暂无消息。")
        return
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", ""))
        is_customer = role == "CUSTOMER"
        with st.chat_message("user" if is_customer else "assistant"):
            st.markdown(message.get("content", ""))
            meta = [_format_time(message.get("created_at"))]
            if message.get("run_id"):
                meta.append(f"run `{_short(message['run_id'])}`")
            st.caption(" · ".join(item for item in meta if item))
            if not is_customer:
                _render_citations(message.get("citations"))


def _followup_chips(ticket: dict[str, Any]) -> None:
    chips = FOLLOWUP_CHIPS.get(str(ticket.get("status", "")), [])
    if not chips:
        return
    order = ticket.get("order_reference") or "TP-0013"
    columns = st.columns(len(chips))
    for column, (title, template) in zip(columns, chips, strict=True):
        if column.button(title, key=f"ticketpilot_chip_{title}", width="stretch"):
            st.session_state["ticketpilot_followup_message"] = template.format(order=order)


def _render_retry_result() -> None:
    result = st.session_state.get(RETRY_RESULT_KEY)
    if not isinstance(result, dict):
        return
    kind = "创建工单" if result.get("kind") == "create" else "追加消息"
    if result.get("same_run"):
        st.success(
            f"🔁 幂等验证通过：用同一个 Idempotency-Key 和同一正文重放“{kind}”请求，"
            f"服务端返回了同一个 run `{_short(result.get('run_id'))}`，"
            "没有再次执行工作流，也没有产生新的消息、审批或退款。"
        )
    else:
        st.warning(
            f"重放“{kind}”请求得到了不同的 run：`{_short(result.get('previous_run_id'))}` → "
            f"`{_short(result.get('run_id'))}`。请检查请求内容是否与上一次完全一致。"
        )


def _replay_last_request(client: TicketPilotClient, token: str) -> None:
    last = st.session_state.get(LAST_REQUEST_KEY)
    if not isinstance(last, dict):
        st.error("本次会话还没有可重放的请求。")
        return
    try:
        with st.spinner("按原 Idempotency-Key 重放上一次请求…"):
            if last["kind"] == "create":
                result = client.create_ticket(
                    token,
                    subject=last["payload"]["subject"],
                    message=last["payload"]["message"],
                    idempotency_key=last["idempotency_key"],
                    order_reference=last["payload"].get("order_reference"),
                )
            else:
                result = client.add_message(
                    token,
                    last["ticket_id"],
                    message=last["payload"]["message"],
                    idempotency_key=last["idempotency_key"],
                )
            ticket_id = _remember_run_result(result)
            _sync_ticket(client, token, ticket_id or last["ticket_id"])
        st.session_state[RETRY_RESULT_KEY] = {
            "kind": last["kind"],
            "same_run": result.get("run_id") == last.get("run_id"),
            "run_id": result.get("run_id"),
            "previous_run_id": last.get("run_id"),
        }
        st.rerun()
    except (KeyError, TicketPilotClientError) as error:
        _show_error(error)


def _render_followup(client: TicketPilotClient, token: str, ticket: dict[str, Any]) -> None:
    status = str(ticket.get("status", ""))
    ticket_id = str(ticket.get("id", ""))
    st.markdown("**继续对话**")
    if status == "WAITING_APPROVAL":
        st.info("工单正在等待人工审批，审批完成并恢复执行后才能继续追加消息。")
    elif status == "PROCESSING":
        st.info("工单正在处理中，请稍后刷新。")
    else:
        _followup_chips(ticket)
        st.session_state.setdefault("ticketpilot_followup_message", "")
        message = st.text_area(
            "追加消息",
            key="ticketpilot_followup_message",
            placeholder="例如：100 元 / 算了，不退了 / 再申请退款 100 元",
            label_visibility="collapsed",
        )
        st.caption("每次发送自动生成新的 Idempotency-Key；同一工单同一时刻只允许一个 active run。")
        send, retry = st.columns(2)
        if send.button(
            "发送并运行", key="ticketpilot_add_message", type="primary", width="stretch"
        ):
            if not message:
                st.error("追加消息不能为空。")
            else:
                key = f"ui-msg-{uuid4()}"
                try:
                    with st.spinner("追加消息并沿用原 thread 启动新 run…"):
                        result = client.add_message(
                            token, ticket_id, message=message, idempotency_key=key
                        )
                        _remember_run_result(result)
                        _sync_ticket(client, token, ticket_id)
                    st.session_state[LAST_REQUEST_KEY] = {
                        "kind": "message",
                        "ticket_id": ticket_id,
                        "payload": {"message": message},
                        "idempotency_key": key,
                        "run_id": result.get("run_id"),
                    }
                    st.session_state.pop(RETRY_RESULT_KEY, None)
                    st.session_state[FOLLOWUP_RESET_KEY] = True
                    st.session_state[NOTICE_KEY] = "消息已追加，新 run 已完成。"
                    st.rerun()
                except (KeyError, TicketPilotClientError) as error:
                    _show_error(error)
        if retry.button(
            "🔁 重试上一次请求",
            key="ticketpilot_retry_last",
            help="模拟客户端在响应丢失后，用同一个 Idempotency-Key 和同一正文重发上一次请求。",
            width="stretch",
        ):
            _replay_last_request(client, token)
    _render_retry_result()


def _render_progress(ticket: dict[str, Any], events: list[dict[str, Any]]) -> None:
    # Show the latest customer turn: an approval-resume run has no TICKET_TRIAGED event,
    # so everything after the last triage belongs to the same turn.
    triage_runs = [event for event in events if event.get("event_type") == "TICKET_TRIAGED"]
    scope = events
    if triage_runs:
        last_run = triage_runs[-1].get("run_id")
        started = next(
            (index for index, event in enumerate(events) if event.get("run_id") == last_run), 0
        )
        scope = events[started:]
    by_key: dict[str, dict[str, Any]] = {}
    for event in scope:
        key = str(event.get("event_type", ""))
        if key in {"TOOL_SUCCEEDED", "TOOL_FAILED"} and event.get("tool_name"):
            key = f"{key}:{event['tool_name']}"
        by_key[key] = event

    lines: list[str] = []
    triage = by_key.get("TICKET_TRIAGED")
    if triage:
        details = triage.get("details") or {}
        lines.append(
            f"✅ **理解意图** · {CATEGORY_LABELS.get(details.get('category'), details.get('category', ''))}"
            f" · {RISK_LABELS.get(details.get('risk_level'), details.get('risk_level', ''))}"
        )
    else:
        lines.append("⬜ **理解意图**")

    order_ok = by_key.get("TOOL_SUCCEEDED:query_order")
    order_failed = by_key.get("TOOL_FAILED:query_order")
    if order_ok:
        found = (order_ok.get("details") or {}).get("found")
        lines.append(
            f"✅ **查询订单** · {'已找到订单' if found else '未找到该订单'}"
            f"{' · ' + ticket['order_reference'] if ticket.get('order_reference') else ''}"
        )
    elif order_failed:
        lines.append("❌ **查询订单** · 订单服务超时，本轮未确认订单状态")
    else:
        lines.append("⚪ **查询订单** · 本轮不需要，或缺少订单号")

    policy_ok = by_key.get("TOOL_SUCCEEDED:search_policy")
    policy_failed = by_key.get("TOOL_FAILED:search_policy")
    if policy_ok:
        count = (policy_ok.get("details") or {}).get("evidence_count", 0)
        lines.append(f"✅ **检索政策** · {count} 条可引用依据")
    elif policy_failed:
        lines.append("❌ **检索政策** · 政策检索服务超时")
    else:
        lines.append("⚪ **检索政策** · 本轮未执行")

    status = str(ticket.get("status", ""))
    if by_key.get("REFUND_EXECUTED"):
        details = by_key["REFUND_EXECUTED"].get("details") or {}
        lines.append(
            f"✅ **人工审批** · 已批准并执行 Mock 退款 {_format_amount(details.get('amount'))}"
            f"，剩余可退 {_format_amount(details.get('remaining_refundable'))}"
        )
    elif by_key.get("REFUND_REJECTED"):
        lines.append("🚫 **人工审批** · 已拒绝，未执行退款")
    elif by_key.get("REFUND_EXECUTION_BLOCKED"):
        lines.append("❌ **人工审批** · 已批准，但执行前复验失败，退款被阻断")
    elif status == "WAITING_APPROVAL" or by_key.get("REFUND_APPROVAL_REQUESTED"):
        payload = (ticket.get("pending_approval") or {}).get("action_payload") or {}
        lines.append(
            f"⏸️ **人工审批** · 等待审批 {_format_amount(payload.get('amount'), payload.get('currency'))}"
        )
    else:
        lines.append("⚪ **人工审批** · 只读请求不需要审批")

    result = ticket.get("processing_result")
    if result in RESULT_LABELS:
        icon = STATUS_ICONS.get(status, "•")
        lines.append(f"{icon} **结果** · {RESULT_LABELS[result][1]}")
    else:
        lines.append(f"⬜ **结果** · {STATUS_LABELS.get(status, status or '-')}")

    st.markdown("**处理进度**")
    st.markdown("\n".join(f"{line}  " for line in lines))


def _render_order(ticket: dict[str, Any]) -> None:
    order = ticket.get("order")
    st.markdown("**订单（已脱敏）**")
    if not isinstance(order, dict):
        st.caption("本工单尚未绑定订单。")
        return
    left, right = st.columns(2)
    left.metric("已支付", _format_amount(order.get("paid_amount"), order.get("currency")))
    right.metric("当前可退", _format_amount(order.get("refundable_amount"), order.get("currency")))
    payment = str(order.get("payment_status", ""))
    fulfillment = str(order.get("fulfillment_status", ""))
    st.caption(
        f"`{order.get('order_reference', '-')}` · {PAYMENT_LABELS.get(payment, payment)} · "
        f"{FULFILLMENT_LABELS.get(fulfillment, fulfillment)}"
        + (f" · {order['carrier']}" if order.get("carrier") else "")
        + (
            f" · 运单 `{order['tracking_number_masked']}`"
            if order.get("tracking_number_masked")
            else ""
        )
    )
    if order.get("estimated_delivery_at"):
        st.caption(f"预计送达 {str(order['estimated_delivery_at'])[:10]}（估计值，不是承诺）")


def _render_approval(
    client: TicketPilotClient,
    identity: dict[str, Any],
    ticket: dict[str, Any],
    events: list[dict[str, Any]],
) -> None:
    approval = ticket.get("pending_approval")
    if not isinstance(approval, dict):
        executed = [
            event
            for event in events
            if event.get("event_type")
            in {"REFUND_EXECUTED", "REFUND_REJECTED", "REFUND_EXECUTION_BLOCKED"}
        ]
        if executed:
            last = executed[-1]
            details = last.get("details") or {}
            st.markdown("**最近一次退款动作**")
            if last.get("event_type") == "REFUND_EXECUTED":
                st.success(
                    f"Mock 退款已执行 {_format_amount(details.get('amount'))}，"
                    f"剩余可退 {_format_amount(details.get('remaining_refundable'))}。"
                    "同一 action 不会被第二次执行。"
                )
            elif last.get("event_type") == "REFUND_REJECTED":
                st.info("退款已被人工拒绝，订单余额未变化。")
            else:
                st.error("审批通过，但执行前复验发现订单事实已变化，退款被阻断。")
        return

    payload = approval.get("action_payload") or {}
    st.markdown("**待审批的退款动作**")
    with st.container(border=True):
        amount_col, balance_col = st.columns(2)
        amount_col.metric(
            "申请金额", _format_amount(payload.get("amount"), payload.get("currency"))
        )
        order = ticket.get("order") or {}
        balance_col.metric(
            "当前可退", _format_amount(order.get("refundable_amount"), order.get("currency"))
        )
        st.caption(
            f"订单 `{payload.get('order_reference', '-')}` · action `{_short(payload.get('action_id'))}` · "
            f"approval `{_short(approval.get('id'))}` · 申请于 {_format_time(approval.get('requested_at'))}"
        )
        role = identity.get("role")
        if role in {"CUSTOMER", "STAFF"}:
            st.info(
                f"当前身份是{ROLE_LABELS.get(role, role)}，不能审批。"
                "请在左侧切换到“审批员”身份后批准或拒绝。"
            )
            return
        reason = st.text_input(
            "审批理由",
            value="人工复核：金额未超过可退余额，订单状态允许退款",
            key="ticketpilot_approval_reason",
        )
        approve, reject = st.columns(2)
        decision = None
        if approve.button(
            "批准并恢复执行", key="ticketpilot_approve", type="primary", width="stretch"
        ):
            decision = "APPROVE"
        if reject.button("拒绝", key="ticketpilot_reject", width="stretch"):
            decision = "REJECT"
        if decision is None:
            return
        if not reason:
            st.error("审批理由不能为空。")
            return
        try:
            with st.spinner("写入审批决定并从 LangGraph 断点恢复…"):
                result = client.decide_approval(
                    identity["token"], str(approval["id"]), decision=decision, reason=reason
                )
                ticket_id = _remember_run_result(result) or str(ticket.get("id", ""))
                _sync_ticket(client, identity["token"], ticket_id)
            st.session_state[NOTICE_KEY] = (
                "审批已提交，工作流已从断点恢复并执行。"
                if decision == "APPROVE"
                else "审批已拒绝，工作流已恢复并关闭退款动作。"
            )
            st.rerun()
        except (KeyError, TicketPilotClientError) as error:
            _show_error(error)


def _event_summary(event: dict[str, Any]) -> str:
    details = event.get("details") or {}
    event_type = str(event.get("event_type", ""))
    tool = TOOL_LABELS.get(str(event.get("tool_name", "")), event.get("tool_name"))
    if event_type == "TICKET_TRIAGED":
        return (
            f"{CATEGORY_LABELS.get(details.get('category'), details.get('category', ''))} · "
            f"{RISK_LABELS.get(details.get('risk_level'), details.get('risk_level', ''))}"
        )
    if event_type in {"TOOL_SUCCEEDED", "TOOL_FAILED"}:
        parts = [str(tool)] if tool else []
        if "found" in details:
            parts.append("找到订单" if details.get("found") else "未找到订单")
        if "evidence_count" in details:
            parts.append(f"{details['evidence_count']} 条依据")
        if details.get("error_code"):
            parts.append(str(details["error_code"]))
        if details.get("duration_ms") is not None:
            parts.append(f"{details['duration_ms']} ms")
        return " · ".join(parts)
    if event_type in {"REFUND_APPROVAL_REQUESTED", "REFUND_APPROVAL_REPLAYED"}:
        return (
            f"金额 {_format_amount(details.get('amount'), details.get('currency'))} · "
            f"action `{_short(details.get('action_id'))}`"
        )
    if event_type == "REFUND_EXECUTED":
        return (
            f"退款 {_format_amount(details.get('amount'))} · "
            f"剩余可退 {_format_amount(details.get('remaining_refundable'))}"
        )
    if event_type == "APPROVAL_DECIDED":
        return f"{details.get('decision', '')} · {details.get('reason', '')}"
    if event_type in {"DEPENDENCY_FAILED", "INSUFFICIENT_EVIDENCE"}:
        return str(details.get("error_code", ""))
    if event_type == "TICKET_CREATED":
        return "已指定订单" if details.get("order_linked") else "未指定订单"
    if event_type == "MESSAGE_ADDED":
        return ""
    return " · ".join(f"{key}={value}" for key, value in list(details.items())[:3])


def _render_timeline(events: list[dict[str, Any]]) -> None:
    st.markdown("**审计时间线**")
    if not events:
        st.caption("暂无审计事件。")
        return
    with st.container(border=True):
        for event in events:
            event_type = str(event.get("event_type", "UNKNOWN"))
            outcome = str(event.get("outcome", ""))
            icon = OUTCOME_ICONS.get(outcome, "•")
            label = EVENT_LABELS.get(event_type, event_type)
            summary = _event_summary(event)
            actor = ACTOR_LABELS.get(str(event.get("actor_type", "")), event.get("actor_type", ""))
            meta = [_format_time(event.get("occurred_at")), str(actor)]
            if event.get("actor_id"):
                meta.append(str(event["actor_id"]))
            if event.get("run_id"):
                meta.append(f"run {_short(event['run_id'])}")
            st.markdown(f"{icon} **{label}** `{event_type}`" + (f" · {summary}" if summary else ""))
            st.caption(" · ".join(item for item in meta if item))
    with st.expander("查看原始审计 JSON"):
        st.json(events, expanded=False)


def _render_developer_details(ticket: dict[str, Any]) -> None:
    ticket_id = str(ticket.get("id", ""))
    with st.expander("开发者详情（ID 与幂等键）"):
        approval = ticket.get("pending_approval") or {}
        payload = approval.get("action_payload") or {}
        last = st.session_state.get(LAST_REQUEST_KEY) or {}
        st.markdown(
            f"- Ticket `{ticket_id}`\n"
            f"- Thread `{ticket.get('thread_id', '-')}`\n"
            f"- 订单 `{ticket.get('order_reference') or '-'}`\n"
            f"- Runs `{'`, `'.join(_ticket_runs(ticket_id)) or '-'}`\n"
            f"- Approval `{approval.get('id', '-')}` · Action `{payload.get('action_id', '-')}`\n"
            f"- 上一次请求的 Idempotency-Key `{last.get('idempotency_key', '-')}`"
        )
        st.caption(
            "Idempotency-Key 回答“是不是同一次 HTTP 请求”；run_id 回答“是不是同一次执行”；"
            "action_id 回答“是不是同一次业务动作”。"
        )


def _render_create_form(
    client: TicketPilotClient, identity: dict[str, Any] | None, expanded: bool
) -> None:
    with st.expander("＋ 新建工单", expanded=expanded):
        for key, default in FORM_DEFAULTS.items():
            st.session_state.setdefault(key, default)
        subject = st.text_input("主题", key="ticketpilot_subject", placeholder="例如：查询订单物流")
        message = st.text_area(
            "客户问题",
            key="ticketpilot_message",
            placeholder="例如：请查询订单 TP-0009 的物流状态和预计送达时间。",
        )
        order_reference = st.text_input(
            "订单号（可选）",
            key="ticketpilot_order_reference",
            placeholder="TP-0009",
            help="演示客户 customer-demo-01 可访问 TP-0001、TP-0005、TP-0009、TP-0013、TP-0017 等订单。",
        )
        st.caption("创建请求的 Idempotency-Key 自动生成；创建后可用“重试上一次请求”验证幂等。")
        if st.button("创建并运行", key="ticketpilot_create", type="primary"):
            if identity is None:
                st.error("请先在左侧选择身份。")
            elif not subject or not message:
                st.error("主题和客户问题不能为空。")
            else:
                key = f"ui-create-{uuid4()}"
                try:
                    with st.spinner("创建工单并执行 TicketPilot graph…"):
                        result = client.create_ticket(
                            identity["token"],
                            subject=subject,
                            message=message,
                            idempotency_key=key,
                            order_reference=order_reference or None,
                        )
                        ticket_id = _remember_run_result(result)
                        _sync_ticket(client, identity["token"], ticket_id)
                    st.session_state[LAST_REQUEST_KEY] = {
                        "kind": "create",
                        "ticket_id": ticket_id,
                        "payload": {
                            "subject": subject,
                            "message": message,
                            "order_reference": order_reference or None,
                        },
                        "idempotency_key": key,
                        "run_id": result.get("run_id"),
                    }
                    st.session_state.pop(RETRY_RESULT_KEY, None)
                    st.session_state[FORM_RESET_KEY] = True
                    st.session_state[NOTICE_KEY] = "工单已创建并完成本轮处理。"
                    st.rerun()
                except (KeyError, TicketPilotClientError) as error:
                    _show_error(error)


def _apply_pending_state() -> None:
    if st.session_state.pop(FORM_RESET_KEY, False):
        st.session_state.update(FORM_DEFAULTS)
    if st.session_state.pop(FOLLOWUP_RESET_KEY, False):
        st.session_state["ticketpilot_followup_message"] = ""
    prefill = st.session_state.pop(PREFILL_KEY, None)
    if isinstance(prefill, dict):
        st.session_state.update(prefill)


def render_ticketpilot_console(base_url: str) -> None:
    _apply_pending_state()
    st.html(WORKBENCH_CSS)
    client = TicketPilotClient(base_url)
    identity = _render_sidebar(client, base_url)

    st.subheader("🎫 TicketPilot 售后工单工作台")
    st.caption(
        "模型负责理解语言；确定性代码、数据库和人工审批控制权限、状态与副作用。"
        "工单事实与审计来自 PostgreSQL 业务表，页面只调用 `/v1` 业务 API。"
    )
    notice = st.session_state.pop(NOTICE_KEY, None)
    if notice:
        st.success(notice)

    ticket = _current_ticket()
    _render_create_form(client, identity, expanded=ticket is None)
    if ticket is None:
        st.info(
            "左侧选择一个演示场景，或直接填写新工单。创建后这里会显示对话、处理进度、审批与审计。"
        )
        return
    if identity is None:
        st.warning("请先在左侧选择身份，再继续操作当前工单。")
        return

    ticket_id = str(ticket.get("id", ""))
    events = _merged_events(ticket_id)
    _render_header(ticket)
    chat_col, context_col = st.columns([7, 5], gap="large")
    with chat_col:
        st.markdown("**对话**")
        _render_conversation(ticket)
        st.divider()
        _render_followup(client, identity["token"], ticket)
    with context_col:
        with st.container(border=True):
            _render_progress(ticket, events)
        with st.container(border=True):
            _render_order(ticket)
        _render_approval(client, identity, ticket, events)
        _render_timeline(events)
        _render_developer_details(ticket)
