# ADR 0001：冻结 TicketPilot MVP 范围

- 状态：Accepted
- 日期：2026-09-06
- 决策者：TicketPilot 项目维护者
- 关联文档：[`../ARCHITECTURE.md`](../ARCHITECTURE.md)、
  [`../CONTRIBUTION_MAP.md`](../CONTRIBUTION_MAP.md)

## 背景

上游 `agent-service-toolkit` 已经提供 FastAPI、LangGraph Agent 注册、SSE、RAG/HITL 示例、
checkpointer/store、Streamlit、Docker Compose 和测试基础。继续增加通用示例不能形成清楚的个人贡献，
也无法证明 Agent 能安全处理真实业务状态。

项目需要在有限时间内形成一条可运行、可恢复、可审计和可评测的 Agent 业务闭环，同时避免为了
技术名词覆盖而引入无法验收的基础设施。

## 决策

### 1. 选择企业售后工单场景

TicketPilot 聚焦两条 MVP 链路：

1. 普通订单咨询：查询订单事实和售后政策后生成有依据的回答；
2. 高风险退款：创建审批、暂停、人工决定、恢复、复验并幂等执行。

该场景同时覆盖 Tool Calling、RAG、LangGraph 状态、HITL、持久化、权限、审计和评测，且每一项
都能通过明确断言验证。项目不把“聊天自然”作为唯一成功标准。

### 2. MVP 使用 PostgreSQL Mock 订单源

当前没有获得真实企业订单 API、用户授权和脱敏数据。MVP 在 PostgreSQL 中构造可追溯的测试订单，
通过 `OrderRepository` 接口访问。未来真实订单系统通过 Adapter 替换 repository 实现，不改变 Agent
和 Tool 合同。

Mock 数据必须覆盖正常、异常和权限边界，保留来源、生成规则和人工验收集；不得把随机生成数据
描述为真实企业数据。

### 3. 一个 PostgreSQL 实例，分离两类数据职责

- `public` schema 由 LangGraph saver/store 保存执行状态；
- `ticketpilot` schema 保存五张业务表；
- 业务 repository 使用独立连接池和显式事务；
- checkpoint 不能代替订单、工单、审批和审计表。

选择 PostgreSQL 是因为当前 Compose 和异步 psycopg 已经验证，关系约束、事务、JSONB 和后续
pgvector 可以覆盖 MVP 与 Week 3 需求，不需要额外部署新的核心数据库。

### 4. 暂不引入 Redis

MVP 的执行规模为单服务、本地演示和小型评测集。当前没有需要跨进程协调的大量后台任务、分布式锁、
高吞吐事件消费或缓存压力。幂等性首先由 PostgreSQL 唯一约束、条件更新和业务事务保证。

只有出现以下证据之一才重新评估 Redis：

- Agent run 必须脱离 HTTP 生命周期并由多个 worker 竞争消费；
- PostgreSQL 无法满足已经测量的热点缓存或锁竞争需求；
- 明确需要带确认、重投和消费组的任务队列。

### 5. 暂不引入 Kubernetes

MVP 使用 Docker Compose 运行 PostgreSQL、FastAPI 和 Streamlit。当前目标是证明业务正确性、恢复、
权限和评测，不是证明集群编排能力。

只有出现多实例部署、滚动升级、弹性伸缩、资源隔离或生产 SLO 的真实需求与测量证据后，才评估
Kubernetes。README 中不得把 Docker Compose 描述为生产级高可用部署。

### 6. 不连接真实支付与生产客户数据

退款使用幂等 Mock Tool。任何真实支付凭据、真实客户订单、地址和联系方式都不进入仓库、Prompt、
测试夹具或审计日志。

### 7. Multi-Agent 不进入 MVP 主线

上游 Supervisor 示例保留为学习材料。TicketPilot 第一版使用一个显式 graph 与确定性业务节点。
只有单 Agent 的上下文、工具权限或评测结果证明需要角色拆分时，才新增子 Agent。

## 结果

### 正面结果

- 个人贡献集中在业务模型、权限、审批、幂等、恢复和评测；
- 开发和测试数据可重复，故障场景可控；
- 基础设施数量有限，可以在四周内形成完整证据；
- PostgreSQL 事务和约束可以承担业务事实的一致性保护；
- 未来接真实订单 API、pgvector、Redis 或 Kubernetes 时保留清楚的扩展边界。

### 代价与限制

- Mock 订单不能证明真实电商 API 的网络、限流和协议适配能力；
- 单进程执行不能证明大规模并发调度能力；
- Docker Compose 不提供生产集群高可用；
- 本地知识库规模不能代表生产检索效果；
- PostgreSQL 业务提交与 LangGraph checkpoint 不是一个原子事务，需要幂等和恢复复验弥补间隙。

这些限制必须在 README、演示和面试中主动说明。

## 被否决的替代方案

### 直接连接公开电商页面或爬取用户订单

否决。公开页面不提供合法的个人订单授权接口，页面爬取既不稳定，也不能构造可信的订单所有权与退款
测试；还会引入隐私和平台条款风险。

### 立即接入多个数据库和消息中间件

否决。PostgreSQL、Chroma/Milvus、Redis、Kafka 同时存在会显著增加运维和一致性问题，但当前没有
数据证明这些组件是 MVP 必需的。

### 只做 RAG 客服聊天机器人

否决。它无法证明订单事实查询、高风险审批、状态恢复和审计能力，个人贡献也容易退化为换 Prompt
和知识文档。

### 让模型直接生成 SQL 或修改业务表

否决。模型生成内容不能承担 tenant 权限、SQL 安全、状态迁移和幂等约束。所有数据库访问必须通过
受控 repository 和 Tool 合同。

## 重新评估条件

满足以下任一条件时创建新 ADR，而不是直接修改本决策：

- 获得具有明确授权、字段文档和测试环境的真实订单 API；
- MVP 两条链路和 Day 14 E2E 已通过，需要进入可靠性扩展；
- 评测证明当前 RAG 存储或检索方式成为主要瓶颈；
- 实测并发和任务持续时间证明需要独立 worker/queue；
- 部署目标从本地演示变为多实例生产环境。
