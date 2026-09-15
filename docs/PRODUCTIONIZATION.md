# TicketPilot：检索、评测与运行治理

更新：2026-09-15。本文对应本轮已实现的功能；各次实验的原始记录统一放在 `data/ticketpilot/evals/productionization_20260915/`。

## 本轮实现

- **政策检索**：163 条虚构商家售后条款，包含 20 个主题、过期/未来版本和租户覆盖规则。先按租户、生效时间、规则版本过滤，再进行 BM25、BGE 向量召回、RRF 融合和可选 Cross-Encoder 重排。
- **适用版本**：同一规则优先使用当前租户特例，否则采用通用版本；过期和未生效条款不进入候选。引用携带条款 ID、版本、生效时间与原文。
- **回答校验**：结构化回答的引用 ID 必须属于本轮检索结果；政策回答缺引用或伪造引用时返回依据不足。ID 合法不等于每个论断都获语义支持，仍保留失败样例。
- **退款边界**：模型将“我要退款”误判为全额时，没有显式全额表达就撤销该标记并要求补充金额。沿用现有审批、动作幂等与执行前数据库复验，不增加多 Agent。
- **执行限制**：每次运行默认 120 秒、单次模型调用 45 秒、最多 6 次模型调用、16,000 token 预留预算、每次最多输出 1,200 token；只对超时、连接故障、429 和部分 5xx 最多重试一次，SDK 自动重试关闭。
- **观测**：沿用 PostgreSQL 审计，新增逐次模型调用与运行汇总事件，通过 `run_id` 关联 `action_id`、模型/工具耗时、usage、重试次数、语料指纹、检索策略与提示词版本。前端展示这些指标和可展开的政策原文。
- **资源控制**：每进程同时最多接纳 2 个检索计算；超限受控失败。ONNX 计算无法被协程取消时，直到工作线程退出才释放容量，避免超时后计算无限排队。

预算是**单次执行范围**，审批恢复属于新运行；不是跨所有会话的全局账单上限。未知 usage 保留为 null，不填零；尚未配置供应商价格表，费用显示未配置。输入 token 先用 UTF-8 字节数加余量预留，返回真实 usage 后结算，因此不是供应商侧硬计费限额。日志不保存模型内部推理。

## 检索实验

固定 90 问，按政策主题分为开发集 45 问和预留测试集 45 问，各含 40 条有答案问题及 5 条无答案问题。标签和问题为作者编写的诊断数据，不是独立人工盲标或真实客户流量。

开发集先对比方案，随后固定参数运行测试集：BGE-small-zh-v1.5（512 维）、BGE-reranker-base、候选 12 条、RRF 常数 60、最低向量相似度 0.50、评价 k=5。服务回答使用 top-5。

| 测试集策略 | Recall@5 | MRR@5 | nDCG@5 | 无答案正确拒答 | 检索 P95 |
| --- | --- | --- | --- | --- | --- |
| 关键词 | 25.0% | 0.1233 | 0.1540 | 4/5 | 0.44 ms |
| BM25 | 62.5% | 0.5313 | 0.5548 | 0/5 | 1.90 ms |
| 向量 | 87.5% | 0.7583 | 0.7887 | 4/5 | 12.84 ms |
| BM25 + 向量 + RRF | 90.0% | 0.6912 | 0.7430 | 4/5 | 15.30 ms |
| 融合 + 重排 | **95.0%（38/40）** | **0.8188** | **0.8521** | 4/5 | **719.25 ms** |

这是本机 Linux Docker/CPU、模型预热后的串行检索耗时，不包含生成回答、HTTP 或数据库流程。重排增加约 0.6 秒中位延迟，换取召回和排序收益；真实 LLM 演示配置默认开启，低延迟场景可切换 hybrid。小语料采用内存索引，无需先引入向量数据库。

## 分类与业务验收

- 已有 120 条分类/槽位诊断集完成整集复测：**114/120（95%）**，调用错误 0；口径为分类加确定性订单号/全额表达校验，四个字段全部匹配才记成功。保留旧的 101/120 基线及本轮输出预算导致截断的失败记录，不能只统计成功响应。
- 60 项工作流验收使用真实 PostgreSQL 业务库与 checkpoint；45 项调用真实分类/回答、6 项向回答阶段注入污染证据、8 项使用确定性故障、1 项为确定性正常对照。审批动作由脚本扮演授权审批人，退款仍是 Mock。
- 首轮完整验收 **57/60**；15 项预设注入/越权检查通过。缺金额被误判为全额及一条生鲜政策漏召回是明确的失败项。
- 配额不足及本机并行构建期间的一次完整尝试为 **31/60**，原始失败记录保留，不用该批次计算正常服务性能。
- 按用户要求停止全量复测，后续仅验证关键场景。两轮定向覆盖 6 个任务，最新结果为 **5 项通过、1 项仍未通过**：补金额、越权审批与污染证据检查通过，生鲜规则漏召回仍保留。不能据此声称最新版本已全量达到 59/60 或 100%。所有轮次放在同一实验目录，运行条件见各记录元数据。

验收断言覆盖工单处理结果、应否创建审批、补槽后恢复、审批拒绝/通过、审批时余额变化、重复执行次数、最终余额与关键引用。它不是完整自然语言回答正确率，也不是 HTTP 压测。

### 已知失败与范围

1. “买的水果不想吃了适用七天无理由吗？”仍会召回通用退货规则，漏掉生鲜例外；回答表达了适用范围不确定，但缺少目标 `fresh-01`，验收判失败。
2. 测试集 5 条无答案问题中仍有 1 条误召回；不能宣称全面拒答或抗注入能力。
3. 保守的全额表达校验可能把少见同义问法送入澄清，优先避免擅自使用全部可退余额。
4. 运行事件在运行结束时落库，进程被强制杀死可能丢失本轮尚未写入的遥测；业务事实与幂等仍由原有事务约束保护。
5. 没有新建多 Agent、K8s、真实支付或长期向量记忆，也没有第二次扩充百万订单数据。

## 启动与回退

保留已有 `.env` 中 OpenAI-compatible 凭据和模型配置。运行：

```powershell
docker compose -f compose.yaml -f docker/compose.ticketpilot-demo.yaml -f docker/compose.ticketpilot-llm.yaml up -d --build
```

页面：`http://localhost:3000`。首次加载 BGE 模型需要下载，模型缓存在 `ticketpilot_model_cache` volume。移除最后一个 override 可恢复不调用模型的 deterministic_demo；检索策略可设为 keyword/bm25/dense/hybrid/rerank。原生 Python 默认 BM25，Docker 默认重排；当前机器 Windows ONNX Runtime 存在 DLL 加载问题，向量及重排实验在 Linux Docker 中完成。

复现实验（容器或具备 `uv sync --extra retrieval` 的环境）：

```text
python scripts/evaluate_ticketpilot_retrieval.py --split test --cache-dir /model-cache --output /tmp/retrieval.json
python scripts/evaluate_ticketpilot_classification.py --dataset data/ticketpilot/evals/classification_eval_v2.json --output /tmp/classification.json
python scripts/evaluate_ticketpilot_workflow.py --cache-dir /model-cache --output /tmp/workflow.json
python scripts/evaluate_ticketpilot_workflow.py --case W023 --case W027 --output /tmp/targeted.json --cache-dir /model-cache
```

以上是复现命令，不是额外自动执行的计划。结果文件存在时拒绝覆盖；工作流脚本使用唯一 eval 租户，结束时清理自身业务数据和 checkpoint。

## 参考

- [FastEmbed 支持的模型](https://qdrant.github.io/fastembed/examples/Supported_Models/)
- [BGE 中文模型卡](https://huggingface.co/BAAI/bge-small-zh-v1.5)
- [Qwen thinking 参数](https://www.alibabacloud.com/help/en/model-studio/deep-thinking)
- [DeepSeek thinking 参数](https://api-docs.deepseek.com/guides/thinking_mode/)

供应商适配针对本项目实际调用验证：Qwen 使用 JSON Schema，当前 DeepSeek-compatible 路由使用 Function Calling 提交输出契约；两者本地均再次做 Pydantic 验证。
