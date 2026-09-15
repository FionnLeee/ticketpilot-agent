# TicketPilot 贡献边界

## 1. 文档目的

TicketPilot 基于开源项目
[`JoshuaC215/agent-service-toolkit`](https://github.com/JoshuaC215/agent-service-toolkit)
进行二次开发。本文件用于持续区分：

1. 上游项目已经提供的能力；
2. 当前仓库中已经完成的个人修改；
3. TicketPilot 计划新增的业务能力；
4. 本阶段明确不做的内容。

README、演示、简历和面试表述都应遵守这条边界，不能把上游能力描述为个人从零实现。

## 2. 当前基线

当前仓库已经从 GitHub fork network 中脱离，作为独立仓库维护，但仍保留上游 MIT
许可证、原版权声明和来源说明。

提交历史按阶段划分：

| Commit | 归属 | 含义 |
| --- | --- | --- |
| `e20d288` | 上游基线导入 | 导入 `agent-service-toolkit` 的服务、Agent、客户端、持久化、UI 和测试基础 |
| `53c6a72` | 个人修改 | 稳定 Windows 环境下的 Streamlit AppTest |
| `b6a0f4b` | 个人修改 | 防止 Uvicorn 覆盖 Windows Selector 事件循环，恢复异步 psycopg 兼容性 |
| `31f6ec0` | 个人修改 | 简体中文 README |
| `89b7a22` | 个人开发 | TicketPilot 业务 MVP：五张业务表���五个 API、订单/政策 Tool、审批与恢复、审计、E2E |
| `3107ba0` | 个人开发 | M1 加固 P0-A～P0-D：入口隔离、退款语义与澄清、消息与运行归属、请求与动作幂等 |
| `52576ef` | 个人开发 | P1-A：工单状态与处理结果分离，六种结果语义与数据库约束 |
| `2a22495` / `09bd710` | 个人开发 | M2：真实模型分类评测基线、兼容服务 schema 修复、提示词对照实验 |
| `bc9de75` 及之后 | 个人开发 | 演示工作台、浏览器黄金链路脚本与截图、文档收口 |

## 3. 能力归属总表

| 能力层 | 上游已经提供 | 当前个人已完成 | TicketPilot 计划新增 |
| --- | --- | --- | --- |
| Agent runtime | LangGraph Agent 注册与加载，兼容 `CompiledStateGraph` 和 `Pregel` | 已完成代码阅读与运行验证 | 新增工单专用 graph，不重写通用 runtime |
| Tool calling | `model -> tools -> model` 循环、`ToolNode`、步数限制和示例工具 | 已完成控制流学习 | 新增订单查询、工单更新、退款申请和知识检索工具 |
| HITL | `interrupt()`、checkpoint 暂停和 `Command(resume=...)` 恢复示例 | 已完成暂停恢复链路学习 | 把 HITL 落到退款或补偿审批，并验证审批前不执行高风险操作 |
| RAG | 基于 Chroma 的基础检索工具和 RAG Agent 示例 | 已完成检索调用链学习 | 建立售后知识库、引用证据、检索评测和 Bad Case 分类 |
| Multi-Agent | Supervisor、子 Agent 和 handoff 示例 | 已完成上游机制学习 | MVP 不以多 Agent 为主线，只有出现明确收益时再评估 |
| HTTP API | FastAPI `invoke`、`stream`、`history`、`threads`、`feedback`、`info` 等接口 | 已新增 tenant token 身份依赖、工单创建/查询、追加消息、审批决定和 run events API | TicketPilot 专用 streaming 留到 Week 3 |
| Streaming | Token 与消息级 SSE streaming | 已完成请求链学习与运行验证 | 复用 streaming，为工单事件定义稳定的业务事件格式 |
| Schema | Pydantic 请求、响应和消息模型 | 已新增严格 Pydantic 领域契约、六种状态、处理结果与状态迁移规则 | 后续仅随已冻结业务接口增量扩展 |
| Checkpoint | SQLite/PostgreSQL/MongoDB checkpointer 接入 | 已验证 PostgreSQL 重启持久化 | 复用 checkpoint 保存 graph 执行进度，不用它代替业务表 |
| Store | 跨 thread 的长期记忆接口 | 已完成 Store 与 Checkpointer 区分学习 | MVP 仅在确有跨会话记忆需求时使用，不存正式审批事实 |
| PostgreSQL | 异步连接池、`AsyncPostgresSaver`、`AsyncPostgresStore` 和 Compose 服务 | 已新增独立业务连接池、版本化迁移、五张业务表、约束、repository 和真实 PostgreSQL 测试 | 后续补 seed、订单 Tool、审批事务和更多故障注入 |
| 身份与安全 | 可选 Bearer Token 校验与基础 safeguard | 已识别客户端可伪造 `user_id`、history 归属校验不足 | 从可信认证上下文取得 tenant/user，加入资源归属检查、RBAC 和高风险审批 |
| Streamlit | 通用聊天 UI、Agent/模型选择、历史会话和语音入口 | 已稳定 Windows UI 测试，并新增默认关闭的工单状态、审批和审计控制台 | 保持本地演示定位，不扩成生产客服门户 |
| Observability | LangSmith/Langfuse 可选集成和 feedback 接口 | 已新增 tenant 范围 run event API、Tool 结果/近似耗时和审批重放事件 | 后续补模型耗时、成本、分页、聚合指标和告警 |
| Docker | PostgreSQL、FastAPI、Streamlit 的 Compose 编排和健康检查 | 已完成 Docker 数据迁移与本地 baseline 验证 | 复用当前编排，只添加 TicketPilot 必需的初始化或迁移步骤 |
| Tests | Agent、Schema、Service、Client、UI 和 Docker E2E 测试基础 | 修复 1 个 Windows 测试问题；baseline 为 `190 passed, 4 skipped`。现已新增 API 契约、隔离、仓储、并发归属、退款语义、graph、评测器、工作台与历史数据测试；2026-09-15 默认全量 `295 passed, 39 skipped`，PostgreSQL 专项 `112 passed` | 独立封存评测集与端到端评测 |

## 4. 上游代码证据

- Agent 注册中心：[`src/agents/agents.py`](../src/agents/agents.py)
- Tool-calling Agent：[`src/agents/research_assistant.py`](../src/agents/research_assistant.py)
- HITL 示例：[`src/agents/interrupt_agent.py`](../src/agents/interrupt_agent.py)
- RAG 示例：[`src/agents/rag_assistant.py`](../src/agents/rag_assistant.py)
- Supervisor 示例：[`src/agents/langgraph_supervisor_agent.py`](../src/agents/langgraph_supervisor_agent.py)
- FastAPI 服务：[`src/service/service.py`](../src/service/service.py)
- Thread 列表：[`src/service/threads.py`](../src/service/threads.py)
- PostgreSQL 持久化：[`src/memory/postgres.py`](../src/memory/postgres.py)
- 通用客户端：[`src/client/client.py`](../src/client/client.py)
- Streamlit UI：[`src/streamlit_app.py`](../src/streamlit_app.py)
- Docker Compose：[`compose.yaml`](../compose.yaml)
- 上游测试：[`tests/`](../tests/)

## 5. 当前已经完成的个人贡献

### 5.1 Windows Streamlit 测试稳定性

提交：`53c6a72`

- 在测试隔离环境时保留 Windows 解析用户目录所需的系统变量；
- 只提高一个已确认抖动的 Streamlit AppTest timeout；
- 没有删除、跳过测试或扩大生产代码改动；
- 全量 baseline 达到 `190 passed, 4 skipped`。

### 5.2 Windows PostgreSQL 事件循环兼容

提交：`b6a0f4b`

- 发现新版 Uvicorn 可能重新建立 Windows Proactor event loop；
- 异步 psycopg 不支持该 loop，导致 PostgreSQL 连接失败；
- Windows 下通过 `loop="none"` 保留项目预先设置的 Selector policy；
- 使用真实模型请求、PostgreSQL checkpoint 和服务重启恢复验证修复。

这两项属于真实个人修改，但仍是“上游适配与基线稳定化”，不是 TicketPilot 的核心业务贡献。

## 6. TicketPilot MVP 的个人贡献范围

MVP 只打通两条可测试的完整链路。

### 6.1 普通订单咨询

```text
创建工单
-> 分类
-> 查询订单
-> 检索售后知识
-> 生成带证据的处理建议
-> 更新并关闭工单
```

### 6.2 高风险退款

```text
创建工单
-> 分类并查询订单
-> 生成退款方案
-> 写入待审批记录
-> interrupt 暂停
-> 人工批准或拒绝
-> 从原 checkpoint 恢复
-> 验证审批和幂等性
-> 执行或取消退款
-> 写入审计事件
```

MVP 需要新增：

- `ticket`、`order`、`approval`、`audit_event` 领域模型；
- 创建/查询工单、追加消息、审批、查询执行事件接口；
- 订单查询、知识检索、工单更新、退款申请等结构化工具；
- 工单分类、风险路由、HITL 和完成/失败处理 graph；
- tenant/user 可信上下文和资源归属校验；
- 退款幂等性与审计证据；
- API、工具、graph 和恢复测试。

## 7. 明确不做

当前 MVP 不做：

- Kubernetes、服务网格和自动扩缩容；
- Redis、Kafka 或独立任务队列；
- 生产支付网关、真实退款或真实客户数据；
- 复杂微服务拆分；
- 自训练或微调基础模型；
- 大规模多 Agent 自由协作；
- 语音客服、AG-UI 和 GitHub MCP 的深度改造；
- 同时部署 Chroma、Milvus 等多套向量数据库；
- 为展示复杂度而增加大量工单状态或工具。

以上内容只有在 MVP 两条主链路、权限边界和测试全部通过后，才重新评估。

## 8. 对外表述规则

推荐表述：

> 我基于开源 `agent-service-toolkit` 的 FastAPI、LangGraph、SSE 和持久化基础，
> 二次开发 TicketPilot 企业售后工单 Agent；个人贡献集中在工单领域模型、业务工具、
> 风险路由、人工审批、tenant 权限、幂等审计和可靠性评测。

禁止表述：

> 我从零实现了完整的 LangGraph Agent 服务、RAG、HITL、SSE 和 PostgreSQL 持久化。

在功能尚未实现或指标尚未实测前，不得使用“已经实现”“提升百分之多少”或“达到生产级”等表述。

## 9. Day 8–10 已落地的个人实现

- 五张 TicketPilot 业务表、迁移校验和领域状态约束；
- 创建/查询工单 API，以及 tenant token 到可信 principal 的映射；
- 创建工单、首条消息和审计事件的单事务写入；
- 3 个 tenant、120 张订单的确定性 seed 数据包；
- 带 tenant/customer SQL 约束和运单脱敏的 `query_order`；
- 带语料 hash、固定 citation、无命中和超时分类的 `search_policy`；
- API、Repository、Tool 和 PostgreSQL 集成测试。

截至 Day 10，TicketPilot graph、审批恢复和幂等退款仍未实现，对外介绍时必须保留这条边界。

## 10. Day 11–12 已落地的个人实现

- TicketPilot 专用 LangGraph state、runtime context、节点与条件边；
- LLM 结构化分类和基于订单/政策证据的回答接口；
- `query_order`、`search_policy` 两个 ToolNode 与 ToolMessage 结果捕获；
- 模型抽取订单号后的 tenant/customer 二次校验与业务关联；
- 确定性退款风险覆盖和合法提案规划；
- PENDING approval、`interrupt()`、审批 API 与 `Command(resume=...)`；
- 恢复后从数据库复验 approval 和最新订单；
- 幂等 Mock 退款、拒绝、冲突决定和审批后订单变化阻断；
- PostgreSQL saver 关闭并重建后的恢复集成测试。

## 11. Day 13 已落地的个人实现

- `GET /v1/runs/{run_id}/events` 与稳定的审计响应合同；
- CUSTOMER 的 tenant + ticket 归属 SQL 过滤，以及其他业务角色的 tenant 过滤；
- `query_order/search_policy` 稳定 Tool 名、脱敏结果分类和近似耗时事件；
- 重复审批的 `APPROVAL_DECISION_REPLAYED` 证据，不改变幂等业务结果；
- Streamlit 工单状态、审批入口与审计时间线最小控制台；
- 普通咨询和退款 HITL 两条真实 PostgreSQL 事件序列测试。

## 12. Day 14 已落地的个人实现

- `POST /v1/tickets/{ticket_id}/messages`，同 ticket/thread 的多 run 语义；
- PostgreSQL 消息幂等唯一约束、冲突检测和提交后未调度的最小恢复；
- Streamlit 追加消息入口；
- 确定性 demo reasoner、合成数据初始化和 Docker overlay；
- 普通咨询、HITL 退款、消息重试、审批重放和跨 tenant 隔离的 E2E 脚本；
- Tool timeout、reasoner 异常与容器健康检查收口。

截至 Day 14，MVP 的五张表、五个 API 和两条主链路已闭环。真实支付、TicketPilot SSE、生产级身份系统、outbox/故障接管和模型质量评测仍未完成。

## 13. M1 可靠性加固

- 创建工单按 tenant、调用主体和 `Idempotency-Key` 去重，并校验规范化请求摘要；
- 同 key 同正文返回原 ticket/run，同 key 换正文返回 409；
- 退款提案以触发消息 ID 作为稳定 `action_id`，节点重放复用原审批；
- 新消息再次申请相同金额生成新的 action/approval，审批执行仍由订单锁和终态检查保证只扣减一次；
- PostgreSQL 并发创建、动作重放、同金额二次退款和重复批准回归测试。
- 将工单生命周期 `status` 与单次处理 `processing_result` 分离；
- 区分成功回答、缺信息、待审批、依赖失败、证据不足和未分类处理失败；
- 修复订单依赖超时被回答成“订单不存在”并错误进入 `RESOLVED` 的问题；
- 在 PostgreSQL、Pydantic、API、审计事件和 Streamlit 中统一结果语义，并用数据库约束拒绝矛盾组合。

## 14. M2 真实模型评测与演示收口

- `scripts/evaluate_ticketpilot_classification.py`：对真实 `LangChainTicketReasoner` 的意图、订单号、明确金额和全额标志做确定性评分，记录模型、temperature、数据与代码哈希、逐条输出与耗时；
- 18 条合成中文开发集与 6 条针对性迁移样本；提示词 v1→v2 对照：15/18→18/18、4/6→5/6，单次实验，不宣称线上准确率；
- 修复兼容服务拒绝 `Decimal` 正则 JSON Schema 的集成问题（wire schema 用 `number | null`，返回后仍由 Pydantic 校验）；
- `/info` 暴露 `ticketpilot_reasoner_mode`，界面据此说明当前是真实模型还是确定性演示；
- Streamlit 工作台：身份切换与后端令牌校验、演示场景按钮、聊天视图与政策引用、自动生成的 `Idempotency-Key` 与同 key 重试对照、按角色门控的审批卡、中文审计时间线、开发者详情；
- `scripts/ticketpilot_ui_e2e.py`：Playwright 驱动工作台走完整黄金链路并断言业务结果，同时生成 `media/ticketpilot/` 截图和可选录像。

## 15. 百万级关系数据与数据工程增量

- `history_manifest.json`：12 租户、730 天、1,000,000 订单的确定性参数、许可与无真实个人数据声明；
- `history_data.py`：按订单流式构建工单、消息、审批和审计时间线，固定 seed/UUID，加入租户差异、季节性和业务状态约束；
- `history_loader.py`：外键有序 PostgreSQL `COPY`、分批短事务、数据集注册与指纹、重跑跳过和部分装载续传；
- `history_quality.py`：21 条跨表质量规则、合成/真实运行来源隔离、六个业务分析视图快照；
- `0007`/`0008`：客户最近工单索引、数据集 registry、来源投影和分析视图；
- `ticketpilot_history_data.py`：统一生成、装载、质量、视图耗时、存储与 `EXPLAIN ANALYZE` 证据报告。

实测为 1,000,000 订单、3,246,760 关联行，装载 196.360 秒，21/21 质量检查通过。以上是第一方合成历史与本地 PostgreSQL 证据，不代表真实用户、百万次 LLM 推理或生产吞吐。详见 [`HISTORY_DATASET.md`](HISTORY_DATASET.md)。

截至 2026-09-15，仍未完成且对外必须保留的边界：真实支付与跨系统 exactly-once、进程强杀后的自动接管、TicketPilot 专用 SSE、独立封存评测集与端到端评测、生产级身份系统。
