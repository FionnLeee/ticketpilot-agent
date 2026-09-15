import { useMutation } from "@tanstack/react-query";
import { ArrowRight, CheckCircle2, Copy, Database, Fingerprint, Play, RotateCcw, ShieldCheck, Stamp } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router";

import { api, compactId } from "~/lib/api";

export function meta() { return [{ title: "面试演示 · TicketPilot" }]; }

const steps = [
  { icon: Database, label: "读取事实", text: "订单与政策通过工具读取，不进入模型记忆。" },
  { icon: ShieldCheck, label: "风险路由", text: "查询直接回答，退款动作进入确定性审批边界。" },
  { icon: Stamp, label: "人工审批", text: "interrupt 持久化等待，审批后从 checkpoint 恢复。" },
  { icon: Fingerprint, label: "动作幂等", text: "同一次申请可安全重试，再次申请使用新动作键。" },
];

export default function Demo() {
  const [scenario, setScenario] = useState<"logistics" | "refund">("logistics");
  const [idempotencyKey, setIdempotencyKey] = useState("");
  useEffect(() => setIdempotencyKey(crypto.randomUUID()), []);
  const mutation = useMutation({ mutationFn: () => api.createTicket(scenario === "logistics" ? { subject: "面试演示：物流查询", message: "我的订单 TP-0009 什么时候送达？", order_reference: "TP-0009" } : { subject: "面试演示：退款审批", message: "订单 TP-0013 我要退款 20 元", order_reference: "TP-0013" }, idempotencyKey) });
  const retry = () => mutation.mutate();
  return (
    <div className="page-stack demo-page">
      <section className="demo-hero"><div><span className="eyebrow">INTERVIEW FLIGHT PLAN · 5 MINUTES</span><h1>不要只讲架构，<br /><em>让系统自己证明。</em></h1><p>选择一条航线，现场展示 Agent 如何在数据库边界内完成业务。</p></div><div className="demo-duration"><span>DEMO WINDOW</span><strong>05:00</strong><small>从业务问题到数据库证据</small></div></section>
      <div className="demo-layout">
        <section className="panel demo-console">
          <div className="scenario-switch"><button className={scenario === "logistics" ? "active" : ""} onClick={() => setScenario("logistics")}>只读物流</button><button className={scenario === "refund" ? "active" : ""} onClick={() => setScenario("refund")}>退款审批</button></div>
          <div className="demo-command"><span>REQUEST</span><p>{scenario === "logistics" ? "我的订单 TP-0009 什么时候送达？" : "订单 TP-0013 我要退款 20 元"}</p><small>ORDER · {scenario === "logistics" ? "TP-0009" : "TP-0013"}</small></div>
          <div className="key-strip"><Fingerprint size={16} /><div><span>IDEMPOTENCY-KEY</span><code>{idempotencyKey}</code></div><Copy size={15} /></div>
          {!mutation.data ? <button className="launch-button" onClick={() => mutation.mutate()} disabled={mutation.isPending || !idempotencyKey}><Play />{mutation.isPending ? "Agent 正在执行航线…" : "启动这次演示"}<ArrowRight /></button> : <div className="demo-result"><CheckCircle2 /><div><span>RUN COMPLETED / PAUSED SAFELY</span><h3>{mutation.data.ticket.processing_result?.replaceAll("_", " ") ?? mutation.data.ticket.status}</h3><p>Ticket {compactId(mutation.data.ticket.id)} · Run {compactId(mutation.data.run_id)}</p></div><Link className="button button-primary" to={`/tickets/${mutation.data.ticket.id}`}>打开执行航道</Link></div>}
          {mutation.data ? <button className="retry-proof" onClick={retry} disabled={mutation.isPending}><RotateCcw size={15} />使用同一个 key 重试：返回同一 ticket/run，不重复执行</button> : null}
          {mutation.error ? <p className="inline-error">{mutation.error.message}</p> : null}
        </section>
        <aside className="demo-script"><span className="eyebrow">TALK TRACK</span><h2>四个必须讲清的点</h2>{steps.map(({ icon: Icon, label, text }, index) => <div className="script-step" key={label}><span>{String(index + 1).padStart(2, "0")}</span><Icon /><div><strong>{label}</strong><p>{text}</p></div></div>)}</aside>
      </div>
    </div>
  );
}
