# TicketPilot 演示手册

工作台是 Streamlit 页面 `src/ticketpilot_streamlit.py`，只调用 `/v1` 业务 API；工单事实与审计全部来自 PostgreSQL 业务表，不读取 LangGraph checkpoint。所有订单、政策、身份和退款都是合成数据，退款是 Mock 副作用。

## 1. 一键启动（推荐：面试演示 / 第一次跑）

需要 Docker Desktop。全容器模式使用确定性 reasoner，不调用模型、不需要 API Key，每步秒级返回：

```powershell
cd ticketpilot
if (-not (Test-Path .env)) { Copy-Item .env.example .env }   # 全容器演示不需要填任何 Key
docker compose -f compose.yaml -f docker/compose.ticketpilot-demo.yaml up -d --build
docker compose -f compose.yaml -f docker/compose.ticketpilot-demo.yaml ps
```

`agent_service` 和 `streamlit_app` 都显示 `healthy` 后，打开 `http://localhost:8501`。全容器模式下页面直接列出三个 `demo-*` 演示身份；如果本机 `.env` 里也配置了 `TICKETPILOT_AUTH_TOKENS`，页面会多出「令牌来源」开关，并自动选中当前后端接受的那一套。

演示结束后停止但保留数据卷：

```powershell
docker compose -f compose.yaml -f docker/compose.ticketpilot-demo.yaml stop
```

不要随手执行 `docker compose down -v`，`-v` 会删除 `postgres_data` 数据卷。

## 2. 本地开发方式（真实模型）

窗口 1：PostgreSQL 用容器，FastAPI 用本机进程；`.env` 里的 `DEFAULT_MODEL`、`TICKETPILOT_AUTH_TOKENS` 生效，`TICKETPILOT_REASONER_MODE` 默认为 `llm`：

```powershell
cd ticketpilot
docker compose up -d postgres
uv run python src/run_service.py
```

窗口 2：

```powershell
cd ticketpilot
uv run streamlit run src/streamlit_app.py
```

真实模型一轮通常 15–35 秒（取决于供应商）。页面左下角会显示当前后端的模型与推理器模式，方便在录屏里说明「这一轮调用的是真实模型」。

> 两套后端不要同时占用 8080。本机 `run_service.py` 只认 `.env` 里的令牌，Docker 演示后端只认 `demo-*-token`；同时运行时 `localhost` 可能命中任意一个，页面会报 `UNAUTHORIZED`。工作台的「令牌来源」和「重新校验身份」就是为了快速定位这个问题。

## 3. 演示身份

| 令牌来源 | 身份 | 用途 |
| --- | --- | --- |
| Docker 演示令牌 | `客户 · customer-demo-01` | 创建工单、追加消息、申请退款 |
| Docker 演示令牌 | `审批员 · approver-demo-01` | 批准 / 拒绝退款 |
| Docker 演示令牌 | `其他租户审批员 · tenant-demo-02` | 演示跨租户访问统一 404 |
| 本地 .env 令牌 | 由 `TICKETPILOT_AUTH_TOKENS` 决定 | 本机后端 |

令牌只存在于 Streamlit 进程里，浏览器只看到身份标签。这是本地演示用的合成身份，不是生产登录方案。

演示客户 `customer-demo-01` 可访问的订单：`TP-0009`（9,999 元已发货，用于查物流）、`TP-0013`（416 元已发货，用于退款）、`TP-0005`、`TP-0025`、`TP-0029`、`TP-0033` 等。

## 4. 五分钟演示脚本

| 步骤 | 页面操作 | 要讲的点 | 截图 |
| --- | --- | --- | --- |
| 0 | 左侧选「客户」，看到「✅ 后端已接受该身份」 | 令牌 → 可信 `tenant / actor / role`；页面不持有业务逻辑 | |
| 1 | 「📦 查询物流」→ 创建并运行 | 订单 Tool、脱敏运单、政策引用（展开 📎） | `01-logistics-answered` |
| 2 | 「🔁 重试上一次请求」 | 同一个 `Idempotency-Key` + 同一正文 → 复用同一个 run，绿色横幅 | `02-retry-idempotent` |
| 3 | 「🚫 提到退款但不退款」→ 创建 | 提到退款 ≠ 申请退款：识别为物流查询，没有生成审批 | |
| 4 | 「🌗 模糊退款（不说金额）」→ 创建 | 进入「等待补充信息」，不默认全额退款 | `03-needs-input` |
| 5 | 点 chip「补充金额：100 元」→ 发送 | 生成退款动作，`interrupt()` 暂停；审批卡提示「客户不能审批」 | `04-waiting-approval` |
| 6 | 切换「审批员」→ 批准并恢复执行 | 从 checkpoint 恢复，执行前复验订单，余额 416 → 316，审计 `APPROVAL_DECIDED → REFUND_EXECUTED` | `05-approver-view`、`06-refund-executed` |
| 7 | 切回「客户」→ chip「再次申请相同金额」→ 发送 → 审批员批准 | 新 message → 新 `action_id` → 新审批；同金额仍是新动作，316 → 216 | `07-second-refund-*` |
| 8 | 切「其他租户审批员」→ 「按 Ticket ID 打开」当前工单 | 统一 404，不泄露资源是否存在 | `08-cross-tenant-404` |
| 9 | 展开「开发者详情」 | `Idempotency-Key` / `run_id` / `action_id` 三层身份分别回答「同一次请求？同一次执行？同一个业务动作？」 | |

截图在 [`media/ticketpilot/`](../media/ticketpilot/)，由第 5 节的浏览器脚本自动生成。

<img src="../media/ticketpilot/07-second-refund-overview.png" width="900" alt="同金额第二次申请形成新动作并执行后的工作台">

## 5. 自动化验收

API 级黄金链路（不开浏览器，对全容器模式运行）：

```powershell
uv run python scripts/ticketpilot_demo.py
```

预期最后输出 `{"status": "passed"}`。脚本验证：普通咨询 `RESOLVED/ANSWERED` 且有 Tool 与解决事件；创建请求同 key 重放返回原 ticket/run；追加消息沿用 `thread_id` 但新建 `run_id`；同 key 换正文返回 `409 STATE_CONFLICT`；退款先 `WAITING_APPROVAL`，批准后执行并 `RESOLVED`；其他 tenant 查询该 run 返回 404；重复批准只产生 `APPROVAL_DECISION_REPLAYED`。

浏览器级黄金链路（Playwright 驱动工作台，同时刷新 `media/ticketpilot/` 截图）：

```powershell
uv run --with playwright python scripts/ticketpilot_ui_e2e.py
```

脚本按第 4 节走完整条链路并断言业务结果：模糊退款不生成审批、同 key 重放复用 run、批准后可退余额变化、第二次同金额申请的 `action_id` 不同且再次扣减、跨租户 404。找不到 Playwright 自带 Chromium 时会使用本机 Chrome / Edge。

可选参数：`--video 目录` 录制整段 WebM 作为备用演示（首次需要 `uv run --with playwright python -m playwright install ffmpeg`），`--pace 3` 让每一步在屏幕上停留 3 秒便于观看，`--token-source docker|env` 强制选择令牌来源。对本机真实模型后端运行时，若 `.env` 里没有第二个租户的身份，跨租户一步会自动跳过。

每跑一次会从 `TP-0013` 扣掉 200 元。可退余额不足 200 时先重置合成数据：

```powershell
docker compose -f compose.yaml -f docker/compose.ticketpilot-demo.yaml run --rm ticketpilot_seed
```

## 6. 常见问题

- 页面报 `UNAUTHORIZED`：令牌与后端不匹配，看第 2 节的提示；点「重新校验身份」或切换「令牌来源」。
- `postgres` 不是 `healthy`：查看 `docker compose logs postgres`；不要同时启动多个 PostgreSQL 占用 5432。
- FastAPI 报 30 秒连接超时：确认 `.env` 中 `DATABASE_TYPE=postgres`、本机运行时 `POSTGRES_HOST=localhost`，容器内由 overlay 覆盖为 `postgres`。
- 页面还是旧版本：Streamlit 进程需要重启（`Ctrl+C` 后重新 `streamlit run`），浏览器按 `Ctrl+F5`。
- 消息接口是同步 JSON；TicketPilot 专用 SSE 尚未实现，页面不借用上游通用聊天流式接口。
