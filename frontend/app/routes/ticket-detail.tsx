import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Bot, Box, CheckCircle2, Clock3, Database, Send, ShieldCheck, UserRound } from "lucide-react";
import { type FormEvent, useMemo, useState } from "react";
import { Link, useParams } from "react-router";

import { ErrorPanel, LoadingPanel, StatusPill } from "~/components/ui";
import { api, compactId, formatTime, type RunEvent } from "~/lib/api";
import { useConsole } from "./shell";

export function meta() { return [{ title: "工单详情 · TicketPilot" }]; }

const runwayStages = [
  { key: "request", label: "Request", match: (event: RunEvent) => event.event_type.includes("TICKET") || event.event_type.includes("MESSAGE") },
  { key: "reason", label: "Reason", match: (event: RunEvent) => event.node_name?.includes("classif") || event.event_type.includes("CLASSIF") },
  { key: "tool", label: "Tool", match: (event: RunEvent) => Boolean(event.tool_name) },
  { key: "action", label: "Action", match: (event: RunEvent) => event.event_type.includes("REFUND_REQUEST") },
  { key: "approval", label: "Approval", match: (event: RunEvent) => event.event_type.includes("APPROV") },
  { key: "effect", label: "Effect", match: (event: RunEvent) => event.event_type.includes("EXECUTED") || event.event_type.includes("RESOLVED") },
];

export default function TicketDetailPage() {
  const { ticketId = "" } = useParams();
  const { mode } = useConsole();
  const detail = useQuery({ queryKey: ["ticket", mode, ticketId], queryFn: () => api.ticket(mode, ticketId), enabled: Boolean(ticketId) });
  const runId = useMemo(() => [...(detail.data?.messages ?? [])].reverse().find((item) => item.run_id)?.run_id, [detail.data]);
  const events = useQuery({ queryKey: ["events", mode, runId], queryFn: () => api.runEvents(mode, runId!), enabled: Boolean(runId) });

  if (detail.isLoading) return <LoadingPanel label="正在拼接工单、订单与审批事实" />;
  if (detail.error) return <ErrorPanel error={detail.error} onRetry={() => detail.refetch()} />;
  const ticket = detail.data!;

  return (
    <div className="page-stack">
      <Link className="back-link" to="/tickets"><ArrowLeft size={16} />返回工单队列</Link>
      <section className="ticket-hero">
        <div><span className="eyebrow">TICKET {compactId(ticket.id)}</span><h1>{ticket.subject}</h1><div className="ticket-meta"><StatusPill value={ticket.processing_result ?? ticket.status} /><span>{ticket.order_reference ?? "未关联订单"}</span><span>更新于 {formatTime(ticket.updated_at)}</span></div></div>
        <div className="risk-stamp"><ShieldCheck /><span>RISK POSTURE</span><strong>{ticket.risk_level?.replaceAll("_", " ") ?? "PENDING"}</strong></div>
      </section>

      <section className="runway panel">
        <div className="runway-head"><div><span className="eyebrow">EXECUTION RUNWAY</span><h2>执行航道</h2></div><span className="mono-note">RUN {runId ? compactId(runId) : "not assigned"}</span></div>
        <div className="runway-track">
          {runwayStages.map((stage, index) => {
            const event = events.data?.events.find(stage.match);
            const current = !event && runwayStages.slice(0, index).every((item) => events.data?.events.some(item.match));
            return <div className={event ? "runway-stage stage-done" : current ? "runway-stage stage-current" : "runway-stage"} key={stage.key}><span>{event ? <CheckCircle2 /> : index + 1}</span><strong>{stage.label}</strong><small>{event?.event_type.replaceAll("_", " ") ?? (current ? "awaiting signal" : "not reached")}</small></div>;
          })}
        </div>
      </section>

      <RuntimeDetails events={events.data?.events ?? []} />

      <div className="workspace-grid">
        <section className="panel conversation-panel">
          <div className="panel-title"><div><span className="eyebrow">CONVERSATION</span><h2>客户与 Agent</h2></div><span>{ticket.messages.length} messages</span></div>
          <div className="messages">
            {ticket.messages.map((message) => <article className={`message message-${message.role.toLowerCase()}`} key={message.id}><div className="message-avatar">{message.role === "AGENT" ? <Bot /> : <UserRound />}</div><div><header><strong>{message.role === "AGENT" ? "TicketPilot Agent" : "客户"}</strong><time>{formatTime(message.created_at)}</time></header><p>{message.content}</p>{message.citations.length ? <div className="citations">{message.citations.map((citation) => <details key={citation.chunk_id ?? citation.source_id}><summary><Database size={13} />{citation.title} · {citation.policy_version ?? "v1"}</summary><p>{citation.excerpt}</p><small>{citation.chunk_id ?? citation.source_id}{citation.effective_at ? ` · 生效 ${formatTime(citation.effective_at)}` : ""}</small></details>)}</div> : null}</div></article>)}
          </div>
          {mode === "customer" ? <MessageComposer ticketId={ticket.id} /> : null}
        </section>

        <aside className="context-stack">
          <section className="panel context-card"><span className="eyebrow">ORDER CONTEXT</span><h3><Box size={18} />{ticket.order?.order_reference ?? "未关联订单"}</h3>{ticket.order ? <dl><div><dt>履约</dt><dd>{ticket.order.fulfillment_status}</dd></div><div><dt>支付</dt><dd>{ticket.order.payment_status}</dd></div><div><dt>实付</dt><dd>¥{ticket.order.paid_amount}</dd></div><div><dt>可退款</dt><dd>¥{ticket.order.refundable_amount}</dd></div><div><dt>物流</dt><dd>{ticket.order.carrier ?? "—"}</dd></div><div><dt>运单</dt><dd>{ticket.order.tracking_number_masked ?? "—"}</dd></div></dl> : <p className="muted-copy">Agent 将在补齐订单号后读取订单事实。</p>}</section>
          <section className={ticket.pending_approval ? "panel approval-signal" : "panel quiet-signal"}><span className="eyebrow">HUMAN GATE</span>{ticket.pending_approval ? <><Clock3 /><h3>等待人工审批</h3><strong>¥{String(ticket.pending_approval.action_payload.amount ?? "—")}</strong><Link className="button button-warning" to="/approvals">前往审批中心</Link></> : <><CheckCircle2 /><h3>当前无需人工介入</h3><p>只读查询和低风险回复可由 Agent 完成。</p></>}</section>
        </aside>
      </div>
    </div>
  );
}

function RuntimeDetails({ events }: { events: RunEvent[] }) {
  const summary = events.find((event) => event.event_type === "RUN_TELEMETRY")?.details;
  const retrieval = events.find((event) => event.tool_name === "search_policy")?.details;
  if (!summary) return null;
  return <section className="panel context-card runtime-card">
    <span className="eyebrow">RUN METRICS</span><h3>执行记录</h3>
    <dl><div><dt>本轮耗时</dt><dd>{Number(summary.duration_ms) / 1000} s</dd></div>
      <div><dt>模型调用（含重试）</dt><dd>{String(summary.model_calls)}</dd></div>
      <div><dt>输入 / 输出 token</dt><dd>{summary.usage_complete ? `${summary.input_tokens} / ${summary.output_tokens}` : "供应商未完整返回"}</dd></div>
      <div><dt>预估费用</dt><dd>未配置价格</dd></div>
      <div><dt>政策检索</dt><dd>{String(retrieval?.retrieval_strategy ?? "legacy")}</dd></div>
    </dl>
    <details><summary>查看各步骤耗时与结果</summary><dl>{events.filter((event) => event.event_type === "MODEL_CALL" || event.tool_name).map((event) => <div key={event.id}><dt>{event.tool_name ?? event.node_name} {event.details.attempt ? `#${event.details.attempt}` : ""}</dt><dd>{String(event.details.duration_ms ?? "—")} ms · {event.outcome}</dd></div>)}</dl></details>
  </section>;
}

function MessageComposer({ ticketId }: { ticketId: string }) {
  const [message, setMessage] = useState("");
  const queryClient = useQueryClient();
  const mutation = useMutation({ mutationFn: () => api.addMessage(ticketId, message, crypto.randomUUID()), onSuccess: async () => { setMessage(""); await queryClient.invalidateQueries({ queryKey: ["ticket"] }); } });
  const submit = (event: FormEvent) => { event.preventDefault(); if (message.trim()) mutation.mutate(); };
  return <form className="message-composer" onSubmit={submit}><textarea value={message} onChange={(event) => setMessage(event.target.value)} placeholder="继续补充订单号、退款金额或追问…" /><button aria-label="发送消息" disabled={mutation.isPending}><Send size={18} /></button></form>;
}
