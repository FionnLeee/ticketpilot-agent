import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Clock3, ExternalLink, ShieldAlert, X } from "lucide-react";
import { Link } from "react-router";

import { EmptyPanel, ErrorPanel, LoadingPanel, SectionHeading, StatusPill } from "~/components/ui";
import { api, compactId, formatTime, type Approval } from "~/lib/api";
import { useConsole } from "./shell";

export function meta() { return [{ title: "审批中心 · TicketPilot" }]; }

export default function Approvals() {
  const { mode, setMode } = useConsole();
  const approvals = useQuery({ queryKey: ["approvals"], queryFn: api.approvals, enabled: mode === "approver" });

  if (mode !== "approver") {
    return <div className="page-stack"><section className="page-title-row"><div><span className="eyebrow">HUMAN APPROVAL GATE</span><h1>审批中心</h1><p>高风险动作与客户身份分离，由独立角色决定。</p></div></section><div className="role-gate"><ShieldAlert /><span>ROLE REQUIRED</span><h2>切换到审批人视角</h2><p>客户 token 无权列出或决定审批，这是系统刻意保留的职责分离。</p><button className="button button-warning" onClick={() => setMode("approver")}>验证审批人身份</button></div></div>;
  }
  return (
    <div className="page-stack">
      <section className="page-title-row"><div><span className="eyebrow">HUMAN APPROVAL GATE</span><h1>审批中心</h1><p>只展示当前租户；待处理项优先排列。</p></div><div className="approval-counter"><Clock3 /><span>待处理</span><strong>{approvals.data?.items.filter((item) => item.status === "PENDING").length ?? 0}</strong></div></section>
      <section className="panel approval-queue">
        <SectionHeading eyebrow="DECISION QUEUE" title="退款动作" aside={<span className="source-chip">TENANT SCOPED</span>} />
        {approvals.isLoading ? <LoadingPanel label="正在读取审批队列" /> : null}
        {approvals.error ? <ErrorPanel error={approvals.error} onRetry={() => approvals.refetch()} /> : null}
        {!approvals.isLoading && approvals.data?.items.length === 0 ? <EmptyPanel>没有待审批动作。先在面试演示中创建一笔退款请求。</EmptyPanel> : null}
        <div className="approval-cards">{approvals.data?.items.map((approval) => <ApprovalCard approval={approval} key={approval.id} />)}</div>
      </section>
    </div>
  );
}

function ApprovalCard({ approval }: { approval: Approval }) {
  const queryClient = useQueryClient();
  const decide = useMutation({ mutationFn: (decision: "APPROVE" | "REJECT") => api.decideApproval(approval.id, decision, decision === "APPROVE" ? "证据与金额已核验" : "证据不足，拒绝执行"), onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: ["approvals"] }); await queryClient.invalidateQueries({ queryKey: ["tickets"] }); } });
  const amount = String(approval.action_payload.amount ?? "—");
  return <article className={approval.status === "PENDING" ? "approval-card approval-pending" : "approval-card"}><div className="approval-card-head"><div><span className="mono-note">APP {compactId(approval.id)}</span><h3>{approval.subject ?? "退款申请"}</h3></div><StatusPill value={approval.status} /></div><div className="approval-amount"><span>REQUESTED EFFECT</span><strong>¥{amount}</strong><small>{approval.order_reference ?? "NO ORDER LINK"}</small></div><div className="approval-facts"><span>请求时间 <b>{formatTime(approval.requested_at)}</b></span><span>动作类型 <b>{approval.action_type}</b></span><span>幂等语义 <b>ONE EFFECT</b></span></div><div className="approval-card-actions"><Link className="text-link" to={`/tickets/${approval.ticket_id}`}>查看证据 <ExternalLink size={14} /></Link>{approval.status === "PENDING" ? <><button className="button button-danger" onClick={() => decide.mutate("REJECT")} disabled={decide.isPending}><X size={16} />拒绝</button><button className="button button-primary" onClick={() => decide.mutate("APPROVE")} disabled={decide.isPending}><Check size={16} />批准并恢复</button></> : null}</div>{decide.error ? <p className="inline-error">{decide.error.message}</p> : null}</article>;
}
