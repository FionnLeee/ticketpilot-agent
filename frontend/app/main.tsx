import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, StrictMode, Suspense, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes, useLocation } from "react-router";

import Shell from "~/routes/shell";
import "./styles.css";

const Approvals = lazy(() => import("~/routes/approvals"));
const Demo = lazy(() => import("~/routes/demo"));
const Overview = lazy(() => import("~/routes/overview"));
const TicketDetail = lazy(() => import("~/routes/ticket-detail"));
const Tickets = lazy(() => import("~/routes/tickets"));

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 15_000, retry: 1, refetchOnWindowFocus: false },
  },
});

const titles: Record<string, string> = {
  "/": "运营总览 · TicketPilot",
  "/tickets": "工单工作台 · TicketPilot",
  "/approvals": "审批中心 · TicketPilot",
  "/demo": "面试演示 · TicketPilot",
};

function DocumentTitle() {
  const { pathname } = useLocation();
  useEffect(() => {
    document.title = titles[pathname] ?? (pathname.startsWith("/tickets/") ? "工单详情 · TicketPilot" : "TicketPilot");
  }, [pathname]);
  return null;
}

function App() {
  return (
    <BrowserRouter>
      <DocumentTitle />
      <Suspense fallback={<div className="boot-screen"><div className="boot-mark" /><strong>TicketPilot</strong><span>正在装载业务视图</span></div>}>
        <Routes>
          <Route element={<Shell />}>
            <Route index element={<Overview />} />
            <Route path="tickets" element={<Tickets />} />
            <Route path="tickets/:ticketId" element={<TicketDetail />} />
            <Route path="approvals" element={<Approvals />} />
            <Route path="demo" element={<Demo />} />
          </Route>
        </Routes>
      </Suspense>
    </BrowserRouter>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
