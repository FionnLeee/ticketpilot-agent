import { useQuery } from "@tanstack/react-query";
import { ArrowUpRight, CheckCircle2, Clock3, Database, Layers3, ShieldCheck } from "lucide-react";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Link } from "react-router";

import { ErrorPanel, LoadingPanel, SectionHeading } from "~/components/ui";
import { api, formatNumber } from "~/lib/api";
import { useConsole } from "./shell";

export function meta() {
  return [{ title: "运营总览 · TicketPilot" }];
}

export default function Overview() {
  const { mode } = useConsole();
  const dashboard = useQuery({
    queryKey: ["dashboard", mode],
    queryFn: () => api.dashboard(mode),
  });

  if (dashboard.isLoading) return <LoadingPanel label="正在汇总百万级业务视图" />;
  if (dashboard.error) return <ErrorPanel error={dashboard.error} onRetry={() => dashboard.refetch()} />;

  const data = dashboard.data!;
  const dataset = data.datasets[0];
  const totalRows = dataset?.total_rows ?? 0;
  const resolved = data.refund_funnel.find((item) => item.status === "EXECUTED")?.approval_count ?? 0;
  const pending = data.refund_funnel.find((item) => item.status === "PENDING")?.approval_count ?? 0;

  return (
    <div className="page-stack">
      <section className="hero-grid">
        <div className="hero-copy">
          <span className="eyebrow">AI AFTER-SALES OPERATIONS / 运营态势</span>
          <h1>看见 Agent 的判断，<br /><em>也看见它没有越过的边界。</em></h1>
          <p>工单事实来自 PostgreSQL；模型负责理解，确定性代码负责权限、金额、审批与幂等。</p>
          <div className="hero-actions">
            <Link className="button button-primary" to="/demo">启动面试演示 <ArrowUpRight size={17} /></Link>
            <Link className="button button-quiet" to="/tickets">进入工单队列</Link>
          </div>
        </div>
        <div className="dataset-radar">
          <div className="radar-orbit orbit-one" />
          <div className="radar-orbit orbit-two" />
          <div className="radar-sweep" />
          <div className="radar-core">
            <span>VERIFIED ROWS</span>
            <strong>{totalRows ? `${(totalRows / 1_000_000).toFixed(2)}M` : "—"}</strong>
            <small>{dataset?.dataset_id ?? "等待数据集证据"}</small>
          </div>
          <span className="radar-blip blip-one" />
          <span className="radar-blip blip-two" />
        </div>
      </section>

      <section className="metric-row" aria-label="关键指标">
        <Metric label="已验证关系行" value={formatNumber(totalRows)} note="PostgreSQL 16 实际落库" icon={<Database />} />
        <Metric label="历史工单" value={formatNumber(dataset?.row_counts.tickets ?? 0)} note="12 tenants · 730 days" icon={<Layers3 />} />
        <Metric label="退款已执行" value={formatNumber(resolved)} note="SYNTHETIC_HISTORY" icon={<CheckCircle2 />} />
        <Metric label="等待审批" value={formatNumber(pending)} note="当前漏斗状态" icon={<Clock3 />} warn />
      </section>

      <div className="content-grid content-grid-wide">
        <section className="panel chart-panel">
          <SectionHeading eyebrow="VOLUME RADAR · 最近 30 天" title="历史工单流量" aside={<span className="source-chip">SYNTHETIC_HISTORY</span>} />
          <div className="chart-wrap">
            {data.daily_volume.length ? (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={data.daily_volume} margin={{ top: 12, right: 8, left: -18, bottom: 0 }}>
                  <defs>
                    <linearGradient id="ticketArea" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="#0f8b8d" stopOpacity={0.3} />
                      <stop offset="100%" stopColor="#0f8b8d" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid stroke="#dce5e7" strokeDasharray="3 6" vertical={false} />
                  <XAxis dataKey="day" tickFormatter={(value) => value.slice(5)} tick={{ fill: "#6b778c", fontSize: 11 }} axisLine={false} tickLine={false} />
                  <YAxis tick={{ fill: "#6b778c", fontSize: 11 }} axisLine={false} tickLine={false} />
                  <Tooltip contentStyle={{ border: "1px solid #cbd8da", borderRadius: 4, fontFamily: "IBM Plex Mono", fontSize: 12 }} />
                  <Area type="monotone" dataKey="created_count" stroke="#0f8b8d" strokeWidth={2.5} fill="url(#ticketArea)" name="创建工单" />
                  <Area type="monotone" dataKey="refund_count" stroke="#f2a900" strokeWidth={1.5} fill="transparent" name="退款工单" />
                </AreaChart>
              </ResponsiveContainer>
            ) : <div className="chart-empty"><div><strong>历史库与在线演示库物理分离</strong><span>上方规模来自 {dataset?.evidence_source ?? "versioned evidence"}；挂载历史数据库后，此处读取 30 天聚合视图。</span></div></div>}
          </div>
          <div className="chart-legend"><span><i className="legend-created" />创建工单</span><span><i className="legend-refund" />退款请求</span></div>
        </section>

        <section className="panel posture-panel">
          <SectionHeading eyebrow="GUARDRAIL POSTURE" title="安全边界" />
          <ul className="posture-list">
            <li><ShieldCheck /><div><strong>租户边界在 SQL</strong><span>资源不存在与越权统一返回 404，不泄露对象存在性。</span></div></li>
            <li><ShieldCheck /><div><strong>高风险动作先审批</strong><span>LangGraph interrupt 持久化等待，由审批决定恢复。</span></div></li>
            <li><ShieldCheck /><div><strong>重试不重复退款</strong><span>请求键、动作键和数据库状态共同保护业务副作用。</span></div></li>
          </ul>
          <div className="posture-score">
            <span>DEMO READINESS</span><strong>04/04</strong><small>isolation · evidence · approval · idempotency</small>
          </div>
        </section>
      </div>

      <section className="panel dataset-ledger">
        <SectionHeading eyebrow="DATASET LEDGER" title="数据证据账本" aside={<span className="mono-note">{dataset?.evidence_source ?? "NO EVIDENCE"} · {dataset?.loaded_at?.slice(0, 10) ?? "—"}</span>} />
        <div className="ledger-grid">
          {Object.entries(dataset?.row_counts ?? {}).map(([label, value]) => (
            <div key={label}><span>{label.replaceAll("_", " ")}</span><strong>{formatNumber(value)}</strong></div>
          ))}
        </div>
      </section>
    </div>
  );
}

function Metric({ label, value, note, icon, warn = false }: { label: string; value: string; note: string; icon: React.ReactNode; warn?: boolean }) {
  return (
    <article className={warn ? "metric-card metric-warn" : "metric-card"}>
      <div className="metric-icon">{icon}</div><span>{label}</span><strong>{value}</strong><small>{note}</small>
    </article>
  );
}
