# 百万级合成业务历史与数据工程验证

这套数据增量用于证明 TicketPilot 不只会跑几条演示请求，还能以可复现、可校验的方式承载一段有业务关系的多租户售后历史。它不是生产流量回放，也不是百万次真实模型推理。

## 已实测的数据规模

2026-09-15 在 Windows 11、Python 3.12、PostgreSQL 16 上执行默认 manifest：

| 数据表 | 本数据集行数 |
| --- | ---: |
| `orders` | 1,000,000 |
| `tickets` | 235,924 |
| `ticket_messages` | 478,813 |
| `approvals` | 56,127 |
| `audit_events` | 1,475,896 |
| 合计 | 3,246,760 |

数据覆盖 12 个租户、730 天时间窗。完整导入使用 50 个批次（每批 20,000 订单），耗时 196.360 秒，约 5,093 订单/秒或 16,535 关联行/秒；生成、入库、`ANALYZE`、质量校验、分析视图和索引查询的端到端耗时 209.222 秒。数据库总大小为 1.539 GiB，其中五张业务表及索引合计 1.487 GiB；该数据库还包含此前的小规模演示记录，因此存储数不是本数据集的独占大小。

固定样本的“租户 + 客户 + 最近工单”查询实际计划为 `Limit → Index Scan`，单次 `EXPLAIN ANALYZE` 执行 0.083 ms。它是本机单次缓存状态下的访问路径证据，不是线上 P95 或 SLA。

## 数据为什么不是随机堆行

manifest 固定 seed、快照时间、租户权重、联系率、退款占比、客单分布与承运商组合。生成器加入缓慢增长、星期节奏、618/双十一/双十二峰值，并按订单年龄生成合法支付与履约状态。

每张合成工单由同一个状态构建器生成完整关系：

```text
order
  └─ ticket（状态、处理结果、风险、时间线）
       ├─ customer / agent messages
       ├─ refund approval（可选）
       └─ audit events（run、tool、审批与执行轨迹）
```

覆盖物流查询、政策问答、无政策证据、订单依赖超时、未知订单、多轮补金额、待审批、拒绝、已执行、执行前状态变化阻断、超额与零余额等路径。预生成历史使用 `synthetic-history:` 标识；通过真实 Service/LangGraph 跑出的演示记录保留为 `LIVE_RUN`，两者不会混成“模型实际处理量”。

## 生成、入库与重跑设计

- `data/ticketpilot/history_manifest.json` 是数据契约，记录版本、seed、规模、租户参数、许可和无真实个人数据声明。
- manifest 指纹同时包含政策 manifest 内容哈希。相同配置得到稳定 UUID 和相同数据；配置变化不会静默复用旧注册记录。
- `generate_history()` 流式产出批次，内存随 `batch_size` 有界，不先构造百万订单列表。
- 新数据按 `orders → tickets → approvals → ticket_messages → audit_events` 外键顺序使用 PostgreSQL `COPY` 写入；每批独立短事务，并在合成数据装载事务中关闭同步提交。
- `synthetic_datasets` 保存数据集指纹与行数。完整数据集再次执行时，装载阶段 0.560 秒内识别并跳过，行数不增长；若上次只完成部分批次，则使用临时 staging 表和 `ON CONFLICT DO NOTHING` 续传。
- `--replace` 只清理 manifest 所声明的 12 个 tenant namespace；这是显式重建选项，不会碰原演示租户。

## 质量与分析层

落库后执行 21 条 SQL 质量规则，本次 `21/21` 通过、违规数全部为 0。规则覆盖：

- 金额边界、支付/履约合法组合、退款后余额；
- 工单与订单的租户/客户归属；
- 工单、消息、审批、事件的时间顺序；
- `WAITING_APPROVAL` 与唯一 `PENDING` 审批的双向一致性；
- 单订单最多一次已执行退款；
- 每张工单唯一 `TICKET_CREATED`，每个 run 从 `RUN_STARTED` 开始；
- 合成来源标识完整。

数据库迁移同时提供一个来源投影视图和六个分析视图：租户概览、工单状态、退款审批漏斗、每日工单量、积压账龄、真实运行/合成历史来源统计。本次各视图查询在 18–4,397 ms，最慢的是扫描百万订单和 23 万工单的 `tenant_overview`；若产品需要高频刷新，应再做物化或增量汇总，本阶段不把一次性面试数据概览伪装成实时数仓。

## 复跑命令

以下使用隔离的本地演示数据库；不要把合成历史脚本指向生产库。

```powershell
$env:TICKETPILOT_HISTORY_DSN='postgresql://postgres:scale-local-only@127.0.0.1:55439/ticketpilot_scale'

# 先用独立 namespace 做千行冒烟
uv run python scripts/ticketpilot_history_data.py --orders 1000 --batch-size 200 --dry-run

# 默认加载百万订单，并保存完整机器可读证据
uv run python scripts/ticketpilot_history_data.py `
  --batch-size 20000 `
  --output ../../outputs/2026-09-15/intermediate/history-million-report.json
```

常用分析示例：

```sql
SELECT * FROM ticketpilot.tenant_overview ORDER BY order_count DESC;

SELECT status, sum(approval_count) AS approvals, sum(requested_amount) AS amount
FROM ticketpilot.refund_approval_funnel
WHERE data_origin = 'SYNTHETIC_HISTORY'
GROUP BY status
ORDER BY approvals DESC;

SELECT day, sum(created_count) AS tickets
FROM ticketpilot.daily_ticket_volume
WHERE data_origin = 'SYNTHETIC_HISTORY'
GROUP BY day
ORDER BY day DESC
LIMIT 30;
```

## 面试口径

可以说：完成百万订单、324.7 万关联行的确定性合成业务历史；设计流式生成、批量 `COPY`、数据集指纹、幂等重跑、21 条跨表质量规则和六个分析视图，并在 PostgreSQL 16 实际落库验证。

不能说：处理了百万真实用户、完成了百万次 LLM 调用、达到了生产级 QPS，或数据库大小完全由该数据集独占。数据是第一方合成历史，Agent 并发与副作用安全的证据见 [`SCALE_DEMO.md`](SCALE_DEMO.md)，两类指标应分开讲。
