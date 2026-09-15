import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Filter, Plus, Search } from "lucide-react";
import { type FormEvent, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router";

import { EmptyPanel, ErrorPanel, LoadingPanel, SectionHeading, StatusPill } from "~/components/ui";
import { api, compactId, formatTime } from "~/lib/api";
import { useConsole } from "./shell";

export function meta() { return [{ title: "工单工作台 · TicketPilot" }]; }

export default function Tickets() {
  const { mode } = useConsole();
  const [query, setQuery] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const tickets = useQuery({ queryKey: ["tickets", mode], queryFn: () => api.tickets(mode) });
  const filtered = useMemo(() => {
    const needle = query.toLowerCase();
    return (tickets.data?.items ?? []).filter((ticket) =>
      [ticket.subject, ticket.order_reference, ticket.id, ticket.status].some((value) => value?.toLowerCase().includes(needle)),
    );
  }, [query, tickets.data]);

  return (
    <div className="page-stack">
      <section className="page-title-row">
        <div><span className="eyebrow">TICKET OPERATIONS</span><h1>工单工作台</h1><p>业务事实、Agent 结论和审批状态保持在同一个上下文。</p></div>
        {mode === "customer" ? <button className="button button-primary" onClick={() => setShowCreate((value) => !value)}><Plus size={17} />创建工单</button> : null}
      </section>

      {showCreate ? <CreateTicketCard onClose={() => setShowCreate(false)} /> : null}

      <section className="panel table-panel">
        <SectionHeading eyebrow="LIVE QUEUE" title={mode === "customer" ? "我的工单" : "租户工单"} aside={<span className="queue-count">{filtered.length} visible</span>} />
        <div className="table-tools">
          <label className="search-field"><Search size={17} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索主题、订单号或 Ticket ID" /></label>
          <button className="icon-button" title="筛选器将在下一阶段接入服务端游标"><Filter size={17} /></button>
        </div>
        {tickets.isLoading ? <LoadingPanel label="正在读取租户工单" /> : null}
        {tickets.error ? <ErrorPanel error={tickets.error} onRetry={() => tickets.refetch()} /> : null}
        {!tickets.isLoading && !tickets.error && filtered.length === 0 ? <EmptyPanel>没有匹配的工单。创建一次物流查询或切换演示身份。</EmptyPanel> : null}
        {filtered.length ? (
          <div className="ticket-table">
            <div className="table-row table-header"><span>工单</span><span>订单</span><span>结果</span><span>风险</span><span>更新时间</span><span /></div>
            {filtered.map((ticket) => (
              <Link className="table-row" to={`/tickets/${ticket.id}`} key={ticket.id}>
                <span className="ticket-subject"><strong>{ticket.subject}</strong><small>{compactId(ticket.id)}</small></span>
                <span className="mono-cell">{ticket.order_reference ?? "UNLINKED"}</span>
                <span><StatusPill value={ticket.processing_result ?? ticket.status} /></span>
                <span className="risk-cell">{ticket.risk_level?.replaceAll("_", " ") ?? "—"}</span>
                <span>{formatTime(ticket.updated_at)}</span>
                <span><ArrowRight size={17} /></span>
              </Link>
            ))}
          </div>
        ) : null}
      </section>
    </div>
  );
}

function CreateTicketCard({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [subject, setSubject] = useState("查询订单物流");
  const [message, setMessage] = useState("我的订单什么时候送达？");
  const [order, setOrder] = useState("TP-0009");
  const mutation = useMutation({
    mutationFn: () => api.createTicket({ subject, message, order_reference: order || undefined }, crypto.randomUUID()),
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: ["tickets"] });
      navigate(`/tickets/${result.ticket.id}`);
    },
  });
  const submit = (event: FormEvent) => { event.preventDefault(); mutation.mutate(); };
  return (
    <form className="panel create-card" onSubmit={submit}>
      <SectionHeading eyebrow="NEW REQUEST" title="创建并运行 Agent 工单" aside={<button type="button" className="text-button" onClick={onClose}>收起</button>} />
      <div className="form-grid">
        <label><span>主题</span><input value={subject} onChange={(e) => setSubject(e.target.value)} required /></label>
        <label><span>订单号</span><input value={order} onChange={(e) => setOrder(e.target.value)} /></label>
        <label className="form-wide"><span>客户问题</span><textarea value={message} onChange={(e) => setMessage(e.target.value)} required /></label>
      </div>
      <div className="form-actions"><span className="form-error">{mutation.error?.message}</span><button className="button button-primary" disabled={mutation.isPending}>{mutation.isPending ? "Agent 运行中…" : "创建并运行"}<ArrowRight size={17} /></button></div>
    </form>
  );
}
