# 数据规模与并发验证

本页记录可复跑的真实测量，服务于项目展示与面试追问。它不把合成数据包装成真实业务数据，也不把本机短测包装成生产 SLA。

## 三层数据与负载证据

| 层次 | 实际规模 | 验证目标 |
| --- | --- | --- |
| PostgreSQL 历史仓 | 1,000,000 订单、235,924 工单、478,813 消息、56,127 审批、1,475,896 审计，共 3,246,760 行；12 租户、730 天 | COPY 装载、跨表质量、分析视图、索引查询、幂等重跑 |
| 可执行工作流集 | 10,000 订单、10 租户、1,000 客户、24 政策片段；12 场景 × 10 租户 = 120 条链路 | `TicketService → LangGraph → PostgreSQL` 状态机与并发不变量 |
| HTTP 读取短测 | 5 档并发 × 每档 1,000 请求 = 5,000 请求 | `Nginx → Uvicorn/FastAPI → Bearer → PostgreSQL` 真实 HTTP 路径 |

全部为固定 seed 的第一方合成数据，不含真实个人信息。百万历史以 `SYNTHETIC_HISTORY` 标记，与 Service/LangGraph 产生的 `LIVE_RUN` 分开统计。10,000 订单只有 10 类状态模板，120 条工作流使用确定性 reasoner，均不计入真实模型准确率。百万历史的生成、装载和 21 条质量规则见 [`HISTORY_DATASET.md`](HISTORY_DATASET.md)。

## 实验 A：完整工作流并发

2026-09-15 本机环境：Windows 11、Python 3.12.4、PostgreSQL 16；业务连接池与 checkpoint 连接池上限各 20。分类器人为等待 20ms 形成重叠，不调用真实 LLM、HTTP 或支付系统。每档 200 次独立请求。

| 并发 | 成功 | 尝试/秒 | P50 ms | P95 ms | P99 ms |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 200/200 | 8.22 | 127.237 | 143.872 | 152.386 |
| 10 | 200/200 | 11.23 | 880.295 | 959.678 | 986.158 |
| 30 | 200/200 | 10.97 | 2,675.312 | 3,184.304 | 3,266.487 |
| 100 | 200/200 | 10.69 | 9,263.094 | 9,514.809 | 9,574.989 |

120/120 场景通过。100 个并发相同创建请求得到 1 个 ticket、1 个 run，数据库按完整请求键复核也只有 1 行；100 个并发相同审批只有 1 次成功执行、99 次状态冲突，余额只减少 0.01，`REFUND_EXECUTED` 只有 1 条。

这组曲线的价值不是“并发越高越强”，而是暴露容量边界：并发从 10 提升到 100 后，吞吐没有继续增长，P95 从约 0.96 秒升到 9.51 秒。连接池等待、状态写入和 checkpoint 是后续应分解测量的候选瓶颈；仅凭本机结果不能给它们排定责任。

复跑命令：

```powershell
$env:TICKETPILOT_SCALE_DSN='postgresql://postgres:scale-local-only@127.0.0.1:55439/ticketpilot_scale'
uv run python scripts/ticketpilot_scale_demo.py `
  --requests 200 `
  --concurrency-levels 1,10,30,100 `
  --idempotency-concurrency 100 `
  --pool-size 20 `
  --output ../../outputs/2026-09-15/intermediate/scale-run-new.json
```

脚本会应用迁移、幂等插入订单、运行场景和并发实验，并保存逐次状态、耗时、工单编号及聚合结果。输出路径必须不存在。重复运行不会重复插入订单，但会新增带唯一实验 key 的工单与审计；每轮审批验证会扣一次 0.01 元 Mock 金额。

## 实验 B：真实 HTTP 读取短测

2026-09-15 经 Docker Nginx 入口请求一个租户内已存在工单，覆盖 HTTP、反向代理、Uvicorn/FastAPI、Bearer 鉴权和 PostgreSQL repository；不包含 LLM 与写动作。

| 并发 | 200 响应 | req/s | P50 ms | P95 ms | P99 ms |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 1,000/1,000 | 95.50 | 10.250 | 11.748 | 13.857 |
| 20 | 1,000/1,000 | 199.41 | 98.157 | 112.368 | 136.564 |
| 50 | 1,000/1,000 | 191.49 | 257.398 | 303.802 | 333.226 |
| 100 | 1,000/1,000 | 189.62 | 517.278 | 588.995 | 659.444 |
| 200 | 1,000/1,000 | 172.17 | 1,081.749 | 1,524.338 | 1,547.976 |

本机短测峰值约 199 req/s（并发 20），继续增加并发反而提高排队和尾延迟。这个结果只能描述该机器、该配置、该只读端点的一次短时观测，不能称为 Agent QPS、持续容量或生产 SLA。

两组实测的版本化聚合证据保存在 [`data/ticketpilot/benchmarks/concurrency_20260915.json`](../data/ticketpilot/benchmarks/concurrency_20260915.json)；逐请求原始报告体积更大，保存在执行当日的工作区 `outputs/2026-09-15/intermediate/`。

```powershell
$env:TICKETPILOT_BENCHMARK_TOKEN='demo-customer-token'
uv run python scripts/ticketpilot_http_benchmark.py `
  --base-url http://localhost:3000/api `
  --requests 1000 `
  --levels 1,20,50,100,200 `
  --output ../../outputs/2026-09-15/intermediate/http-read-new.json
```

## 并发正确性设计

- 创建工单：`(tenant_id, actor_id, Idempotency-Key)` 部分唯一索引先阻止重复插入，再用 request hash 区分“同动作重试”和“同 key 不同载荷”。同载荷可重放，冲突载荷返回 409；再次申请相同金额只需新 key，即形成新动作。
- 同工单串行：短事务锁定 ticket；`active_run_id` 记录所有权，条件更新 `run_started` 完成 compare-and-set，避免两个 worker 同时执行一条业务链路。跨工单仍可并行，模型调用不占用数据库长事务。
- 退款动作：审批用不可变 `action_id` 唯一约束；决策和执行阶段锁定 approval/ticket/order 行，并检查状态迁移，Mock 支付使用 `refund:{action_id}` 幂等键。因此重试不会二次扣减，但新 action 可以再次退款。
- 多租户：业务表、索引和查询均携带 `tenant_id`；客户读取还要求 `customer_id`，避免为了规模测试而绕过隔离边界。

## 生产化边界

若继续扩展，顺序应是：先用 k6/Locust 做持续 HTTP 混合读写负载并拆分数据库/checkpoint/模型耗时；再增加服务端并发额度、有限队列、超时与 429/503、租户配额及 OpenTelemetry。只有出现跨进程异步任务需求时才引入队列；接真实支付时再补供应商幂等、Transactional Outbox、回调去重和对账。当前阶段不为“技术栈数量”虚构 Redis、Kafka、分库分表或互联网级流量。
