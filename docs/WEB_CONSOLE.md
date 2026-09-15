# TicketPilot React 运营控制台

正式展示入口位于 `frontend/`，使用 React 19、TypeScript、Vite、React Router 7 SPA、TanStack Query、Recharts 和手写 TicketPilot 组件。Streamlit 不删除，保留为内部调试台。

## 产品定位

界面不是通用管理后台，而是“多租户 AI 售后运营控制塔”。它服务两个对象：业务角色在一个上下文内处理工单；面试官在五分钟内看清 Agent 的执行链和安全边界。

视觉系统来自航空运行语义：夜航墨黑、雷达青、许可琥珀和告警珊瑚红。唯一的高强调组件是 **Execution Runway（执行航道）**，将 `Request → Reason → Tool → Action → Approval → Effect` 映射到真实审计事件。桌面端使用左侧导航、中央工作区与右侧上下文；移动端折叠导航并重排卡片。支持键盘焦点和 `prefers-reduced-motion`。

## 页面

| 路由 | 用途 |
| --- | --- |
| `/` | 百万级数据证据、租户态势、安全边界和历史/在线库分离说明 |
| `/tickets` | tenant/customer 过滤后的工单队列，客户可创建新工单 |
| `/tickets/:id` | 对话、订单事实、审批状态和 Execution Runway |
| `/approvals` | 审批人角色的租户内退款动作队列与决定入口 |
| `/demo` | 可复跑的物流/退款面试航线，显示 Idempotency-Key |

前端只调用 FastAPI `/v1`，不读取 LangGraph checkpoint，也不直连 PostgreSQL。Vite 开发服务器和 Nginx 容器都把 `/api` 反向代理到 `agent_service:8080`，避免浏览器跨域配置。

## 启动

全容器启动：

```powershell
Copy-Item .env.example .env
docker compose -f compose.yaml -f docker/compose.ticketpilot-demo.yaml up -d --build
```

打开 `http://localhost:3000`。默认本地演示 token 写在 compose override 中，只对应合成数据；不得作为生产鉴权方案。

单独开发前端：

```powershell
cd frontend
npm ci
npm run dev
```

生产构建和类型检查：

```powershell
npm run typecheck
npm run build
```

## 数据口径

在线 demo PostgreSQL 与百万级历史 PostgreSQL 可以物理分离。控制台优先读取 `synthetic_datasets` 注册表；当前服务未挂载历史库时，规模卡片读取仓库内版本化 `history_benchmark.json` 并显示 `VERSIONED_BENCHMARK`，不会把静态证据伪装成在线查询。日序列只在实际连接历史库时显示。

## 当前边界

- 工单/审批列表使用有上限的最近记录读取，尚未实现 cursor pagination。
- 当前通过 polling/手动刷新读状态，没有新增 WebSocket 或 SSE。
- 演示 token 位于浏览器请求中，只适合本地合成演示。真实部署应接 OIDC/短期 session，并由 FastAPI 或网关设置 HttpOnly cookie。
- Recharts 只在运营总览路由加载，仍需后续做更细的 bundle 拆分。
