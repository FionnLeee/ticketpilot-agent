# TicketPilot 数据资产与评测方案

> 调研日期：2026-09-06
>
> 状态：候选源已审计，尚未把任何第三方原始数据复制进仓库
>
> 目标：为订单咨询、退款审批、tenant 隔离、RAG 和恢复能力建立可复现的开发数据与盲测数据

## 1. 先区分数据库、数据集和评测集

这三个词在项目中不是一回事：

| 名称 | TicketPilot 中的含义 | 当前载体 |
| --- | --- | --- |
| 业务数据库 | 运行时订单、工单、消息、审批和审计事实 | PostgreSQL `ticketpilot` schema |
| 开发数据集 | 本地联调所需的 Mock 订单、政策和已知场景 | 后续由固定 seed 脚本生成并写入 PostgreSQL |
| 盲测集 | 开发期间不看答案、不调 Prompt 的任务和最终状态断言 | 独立版本化的 evaluation fixtures |

项目缺的不是“再下载一个大 CSV”，而是带来源、约束、任务答案、隔离规则和可重复导入过程的数据资产。
真实交易表通常没有客服目标和 Tool 最终状态；Agent benchmark 通常又是合成环境。因此最终采用分层组合，而不是宣称某一个公开数据集能够解决全部问题。

## 2. 候选源审计结果

### 2.1 第一优先级：τ-bench retail

- 官方仓库：[`sierra-research/tau2-bench`](https://github.com/sierra-research/tau2-bench)
- 本次审计 revision：`672227c6b6676edc20d57ea53b7000262aae77b9`
- 仓库许可证：MIT；本次检查未发现 `data/tau2/domains/retail` 的独立许可证文件，正式复制数据前仍需再次确认该 revision 的许可边界并保留版权声明。
- 官方定位：每个 domain 同时提供 policy、tools、tasks 和数据库，适合评测有状态的客服 Agent，而不只是文本分类。
- 本地结构审计：50 个 products、500 个 users、1,000 个 orders、114 个 retail tasks。
- 官方 split 文件的本地实测：`train=74`、`test=40`、`base=114`。
- 订单状态分布：`pending=423`、`processed=102`、`delivered=373`、`cancelled=102`。
- 任务包含订单查询、取消、退货、换货、修改地址/商品/付款方式和转人工等参考动作。

关键文件：

- [`db.json`](https://github.com/sierra-research/tau2-bench/blob/main/data/tau2/domains/retail/db.json)
- [`tasks.json`](https://github.com/sierra-research/tau2-bench/blob/main/data/tau2/domains/retail/tasks.json)
- [`split_tasks.json`](https://github.com/sierra-research/tau2-bench/blob/main/data/tau2/domains/retail/split_tasks.json)
- [`policy.md`](https://github.com/sierra-research/tau2-bench/blob/main/data/tau2/domains/retail/policy.md)
- [domain 数据与评测语义说明](https://github.com/sierra-research/tau2-bench/blob/main/src/tau2/domains/README.md)

需要特别注意：`evaluation_criteria.actions` 是用来推导目标数据库终态的一条参考轨迹，不等于 Agent 必须逐次复刻相同调用。TicketPilot 应比较最终业务状态、必要沟通和安全约束，不能把“Tool 序列完全一致”当成唯一成功标准。

结论：它是最适合 TicketPilot 的主参考源，但当前五表 MVP 没有商品行、地址和换货模型，不能整库硬搬。Day 10 只转换与订单事实和退款链路兼容的字段与任务；其余任务标记为未来扩展，不伪装成当前已支持。

### 2.2 第二优先级：Microsoft STATE-Bench Customer Support

- 官方仓库：[`microsoft/STATE-Bench`](https://github.com/microsoft/STATE-Bench)
- 本次审计 revision：`5644b1838d96bc4483da29642d058ecaa6f80f7f`
- 许可证：MIT。
- 官方总量：三个企业 domain 共 450 个任务；Customer Support 为 150 个。
- 官方明确披露：数据由大模型合成，不能把结果表述为真实企业流量表现。
- 本地审计：150 个 task 文件和 150 个 task-local 环境数据库；另有 100 条 Customer Support 训练轨迹。
- Customer Support 类型分布：`return_item=33`、`compound=33`、`shipping_claim=24`、`exchange_item=15`、`warranty_claim=13`、`edge_case=12`、`cancel_order=10`、`price_match_refund=10`。
- 每个任务包含对话要求和数据库终态要求，适合补充复合请求、边界条件和错误操作阻断测试。

结论：不作为 TicketPilot 运行库，而作为二级对抗/边界用例来源。只人工改写与当前 MVP 一致的退款、物流和 compound 场景，并保留原任务 ID、revision 和改写记录。

### 2.3 中文场景参考：ECom-Bench

- 官方仓库：[`XiaoduoAILab/ECom-Bench`](https://github.com/XiaoduoAILab/ECom-Bench)
- 本次审计 revision：`bc5018daaf45eea12330941bce68cebd293cfa85`
- 许可证：Apache-2.0。
- 官方说明这是非生产用途的模拟用户数据，并提供中文电商客服场景、Tool 调用和多轮用户模拟。
- 本地版本只有一个初始提交；`users_info.json` 顶层包含 28 个模拟用户，并含姓名、电话、地址形态字段。

结论：用来参考中文客服说法、工具命名和场景分类，不直接导入其中的身份记录。即使官方标为模拟数据，TicketPilot 也应重新生成姓名、电话、地址等值，避免在演示中出现看似真实的个人信息。

### 2.4 真实交易分布参考：UCI Online Retail II

- 官方数据页：[UCI Online Retail II](https://archive.ics.uci.edu/dataset/502/online%2Bretail)
- 许可证：CC BY 4.0。
- 数据规模：1,067,371 条英国线上零售交易，时间跨度为 2009-12 至 2011-12。
- 字段包含发票、商品、数量、价格、客户和国家，取消交易可由发票号识别。

结论：可用于金额、商品数、复购和取消比例等分布校准，但年代、国家、币种和业务流程都与 TicketPilot 演示不同。它没有客服目标、审批、政策证据或 Tool 终态，不能单独作为 Agent 验收集。

### 2.5 可选来源

| 来源 | 可用点 | 不作为主数据的原因 |
| --- | --- | --- |
| [Olist Brazilian E-Commerce](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce) | 约 10 万条匿名化真实商业订单，含付款、物流、评价 | 2016–2018 巴西数据；缺少 Agent 动作真值；正式下载和再分发前需在 Kaggle 页面人工确认当时的具体许可条款 |
| [Bitext Customer Support](https://huggingface.co/datasets/bitext/Bitext-customer-support-llm-chatbot-training-dataset) | 英文客服问法、intent 和语言变体；数据卡标注为 CDLA-Sharing-1.0 | 主要是问答/意图语料，不是有状态订单环境；回答文本不能充当退款执行真值 |
| [Banking77](https://huggingface.co/datasets/PolyAI/banking77) | 细粒度 intent 分类方法和基线 | 银行业域与 TicketPilot 电商售后不匹配 |

## 3. 正式采用方案

### 3.1 P0：TicketPilot 原生确定性 seed pack

先生成一套完全受控的中文 Mock 数据写入现有五张业务表：

- 3 个 tenant，确保同订单号可以跨 tenant 重复但不能串读；
- 每个 tenant 至少 4 个 customer；
- 第一版 120–300 个 order，覆盖全部 payment/fulfillment 状态组合中的合法子集；
- 金额覆盖 `0.00`、小额、普通、高额、已部分退款和全额不可退边界；
- 包含无物流、已发货、已送达、已取消、ETA 过期和 tracking 需要脱敏的记录；
- 所有 UUID、时间和金额由固定 seed 生成；重复导入结果一致；
- 任何“中文姓名/电话/地址”均使用明显虚构值，MVP 不需要的个人字段不生成。

这套数据负责稳定演示，不依赖外部站点可用性。

### 3.2 P1：τ-bench 兼容子集

- `train` 的 74 个任务只用于开发、Prompt/Tool 调整和 Bad Case 归因；
- `test` 的 40 个任务保持盲测，开发文档和 Prompt 不抄入其目标答案；
- 只选择当前 TicketPilot 支持的订单查询、退款/退货判断和转人工语义；
- 英文任务转换为中文时保留 `source_task_id`、原文 hash、译文、翻译方式和人工复核状态；
- 不声称转换后的 TicketPilot 成绩等于官方 τ-bench 榜单成绩。

### 3.3 P2：STATE-Bench 对抗集

从 150 个 Customer Support 任务中挑选与 MVP 一致的 edge case、compound、shipping claim 和 refund 场景，专门测试：

- 用户同时提出事实查询和高风险写操作；
- 缺少必要信息时不得猜测；
- 订单状态在审批期间发生变化；
- 越权 tenant/customer 的资源表现为不可见；
- 拒绝审批、重复审批和恢复重试不产生第二次退款。

## 4. 数据血缘清单

任何进入仓库或构建产物的数据包都必须附 manifest，至少包含：

```json
{
  "dataset_id": "ticketpilot-seed-v1",
  "source_url": "https://github.com/sierra-research/tau2-bench",
  "source_revision": "672227c6b6676edc20d57ea53b7000262aae77b9",
  "source_license": "MIT",
  "source_file_sha256": {},
  "transform_version": "1",
  "generator_seed": 20260906,
  "split": "development",
  "contains_real_personal_data": false
}
```

本次本地审计的 τ-bench 文件 hash：

| 文件 | SHA-256 |
| --- | --- |
| `retail/db.json` | `DBDE692E380BB4AD17F9F7841172CF1E69BEBAD2DAA405628CCDC52A42B3B9B0` |
| `retail/tasks.json` | `F44563D8F36362E2DC58282BB02CFB85010D22F27D9439808758D3E9B2BC16D2` |
| `retail/split_tasks.json` | `5F9949B833E6B99584B87E84EF22F9D81789EA67A52E534C6F7AA91EFF08ADC7` |

这些 hash 只描述本次审计 revision，未来更新上游时必须重新计算，不能静默替换。

## 5. 导入前质量门

导入器和 CI 至少验证：

1. `tenant_id + order_reference` 唯一，跨 tenant 可以出现相同 reference；
2. `refundable_amount <= paid_amount`，金额统一使用 decimal；
3. 订单状态组合合法，退款/取消状态与可退金额一致；
4. ticket、message、approval、audit 的复合 tenant 外键全部可解析；
5. 开发任务 ID 与盲测任务 ID 不相交；
6. 原始文件 revision、hash、license 与 manifest 相符；
7. 不含 API key、支付凭据、真实电话、真实地址或未授权对话；
8. 相同 generator seed 重建后得到相同逻辑记录和统计摘要；
9. 每个验收场景都有初始数据库状态、用户目标、允许/禁止动作和预期最终状态；
10. 失败样例不能只靠 LLM judge，权限、金额、调用次数和状态必须用确定性断言。

## 6. 最终验收指标

| 维度 | 最低验收口径 |
| --- | --- |
| 订单事实正确率 | 状态、金额、ETA 等结构化字段与数据库逐字段一致 |
| 越权泄漏 | 跨 tenant/customer 泄漏次数必须为 0 |
| 审批安全 | `PENDING` 时退款 Tool 执行次数必须为 0 |
| 幂等 | 同一审批和幂等键重复请求后成功执行次数必须为 1 |
| 恢复 | interrupt 后重启服务仍可用原 `thread_id` 完成同一流程 |
| 任务完成 | 同时满足最终数据库状态、必要沟通和禁止动作三类断言 |
| RAG | 报告 Recall@K、引用命中率和无依据断言率，并保留 Bad Case |
| 可复现 | 固定代码 revision、数据 revision、模型、参数和 seed 后可以重跑 |

## 7. 当前进度与下一步

Day 10 已完成：

1. 原生 seed generator、manifest 与幂等导入；
2. 3 个 tenant、12 个客户、120 张订单的 PostgreSQL 实测；
3. `PostgresOrderRepository` 的 tenant/customer 归属测试；
4. 订单状态、金额、ETA 和 tracking 脱敏的确定性 Tool 测试；
5. 6 个合成政策 chunk、manifest/hash 和固定 citation 检索基线。

2026-09-15 数据规模增量已完成：

1. 默认 manifest 在 PostgreSQL 16 实际落库 1,000,000 订单及 2,246,760 条关联工单、消息、审批和审计记录，覆盖 12 租户与 730 天；
2. 流式生成、20,000 订单分批、外键有序 `COPY`，完整装载 196.360 秒；
3. 数据集指纹与注册表支持幂等跳过和部分批次续传，第二次装载阶段 0.560 秒且不新增行；
4. 21 条跨表 SQL 质量规则全部通过，另提供来源隔离投影和六个业务分析视图；
5. `SYNTHETIC_HISTORY` 与真实 Service/LangGraph 产生的 `LIVE_RUN` 分开统计，不把预生成轨迹计作模型运行量。

完整证据和边界见 [`HISTORY_DATASET.md`](HISTORY_DATASET.md)。

可选后续数据工作（不阻塞当前投递）：

1. 只在转换规则和许可复核完成后加入 τ-bench `train` 子集；
2. 把 τ-bench `test` split 存放在 Prompt 和 few-shot 不会读取的位置；
3. 为复合问题建立多标签 evidence 真值，不强行规定唯一 Top-K 顺序；
4. 在 Week 3 比较 sparse、vector、hybrid 和 rerank 的 Recall@K、延迟与成本。
