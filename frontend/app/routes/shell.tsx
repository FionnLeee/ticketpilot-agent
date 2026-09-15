import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Database,
  Inbox,
  LayoutDashboard,
  Menu,
  PlayCircle,
  Radar,
  ShieldCheck,
  Stamp,
  X,
} from "lucide-react";
import { useState } from "react";
import { NavLink, Outlet, useOutletContext } from "react-router";

import { api, type IdentityMode } from "~/lib/api";

type ConsoleContextValue = {
  mode: IdentityMode;
  setMode: (mode: IdentityMode) => void;
};

export function useConsole() {
  return useOutletContext<ConsoleContextValue>();
}

const nav = [
  { to: "/", label: "运营总览", icon: LayoutDashboard, end: true },
  { to: "/tickets", label: "工单工作台", icon: Inbox },
  { to: "/approvals", label: "审批中心", icon: Stamp },
  { to: "/demo", label: "面试演示", icon: PlayCircle },
];

export default function Shell() {
  const [mode, setMode] = useState<IdentityMode>("customer");
  const [menuOpen, setMenuOpen] = useState(false);
  const identity = useQuery({
    queryKey: ["identity", mode],
    queryFn: () => api.identity(mode),
  });

  return (
    <div className="app-shell">
        <aside className={menuOpen ? "sidebar sidebar-open" : "sidebar"}>
          <div className="brand-lockup">
            <div className="brand-mark"><Radar size={24} /></div>
            <div><strong>TicketPilot</strong><span>AI OPS CONTROL</span></div>
          </div>
          <button className="mobile-close" onClick={() => setMenuOpen(false)} aria-label="关闭菜单"><X /></button>
          <nav className="primary-nav" aria-label="主导航">
            {nav.map(({ to, label, icon: Icon, end }) => (
              <NavLink key={to} to={to} end={end} onClick={() => setMenuOpen(false)}>
                <Icon size={18} /><span>{label}</span>
              </NavLink>
            ))}
          </nav>
          <div className="system-card">
            <div className="system-card-title"><Activity size={15} /> SYSTEM POSTURE</div>
            <div className="system-line"><span>业务事实</span><b><i /> PostgreSQL</b></div>
            <div className="system-line"><span>工作流</span><b><i /> LangGraph</b></div>
            <div className="system-line"><span>隔离边界</span><b><ShieldCheck size={13} /> Tenant SQL</b></div>
          </div>
          <div className="sidebar-foot">
            <Database size={15} />
            <span>展示数据与在线运行分源统计</span>
          </div>
        </aside>

        <main className="main-frame">
          <header className="topbar">
            <button className="mobile-menu" onClick={() => setMenuOpen(true)} aria-label="打开菜单"><Menu /></button>
            <div className="flight-strip">
              <span className="live-dot" />
              <b>CONTROL ONLINE</b>
              <span>{identity.data?.tenant_id ?? "正在验证租户"}</span>
            </div>
            <div className="identity-switch" aria-label="演示身份">
              <button className={mode === "customer" ? "active" : ""} onClick={() => setMode("customer")}>客户</button>
              <button className={mode === "approver" ? "active" : ""} onClick={() => setMode("approver")}>审批人</button>
            </div>
            <div className="actor-chip">
              <span>{identity.data?.role ?? "VERIFYING"}</span>
              <strong>{identity.data?.actor_id ?? "identity probe"}</strong>
            </div>
          </header>
          <div className="page-stage">
            <Outlet context={{ mode, setMode }} />
          </div>
        </main>
      </div>
  );
}
