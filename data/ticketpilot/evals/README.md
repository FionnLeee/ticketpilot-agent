# TicketPilot 真实模型分类评测

这里保存第一方合成的中文诊断集，不含真实客户数据。它们用于验证 `LangChainTicketReasoner` 的意图分类与槽位抽取，不能等同于线上准确率。

## 数据集

| 文件 | 规模 | 定位 |
| --- | ---: | --- |
| `classification_dev_v1.json` | 18 | 早期提示词开发集 |
| `classification_transfer_v1.json` | 6 | 针对旧错误固定的新表述与反例 |
| `classification_eval_v2.json` | 120 | 扩展诊断集：8 类场景、90 条 hard、30 条 medium |

`classification_eval_v2.json` 由 `scripts/build_ticketpilot_eval_dataset.py` 按固定模板生成，SHA-256 为 `965070f72e1d2cec37a6987a349ba6123365f34bc2e3d62678d4e5911f849d61`。场景覆盖物流、明确金额退款、全额退款、退款缺槽位、政策、否定/取消、其他意图和上下文继承。标签由项目开发者定义，尚无独立人工双标，所以它是可复现诊断集，不是第三方 benchmark。

## 评分契约

评分四项：意图、订单号、明确退款金额、全额退款标志。四项全对才计整条正确；调用异常和超时保留在分母内并计入 `error_count`。订单号忽略大小写，金额按 `Decimal` 数值比较。`priority` 不评分，因为当前提示词没有统一优先级策略。

评测器输出：Wilson 95% 置信区间、四字段准确率、按场景/难度准确率、各类别 precision/recall/F1、混淆矩阵，以及 P50/P95/P99 模型调用耗时。`--concurrency` 只控制客户端并发上限，不能被解释为模型服务的生产吞吐。

## 2026-09-15 实测

环境：`qwen3.7-flash`、temperature=0.5、并发 4、每条超时 60 秒。

第一轮完整 120 条：严格整条准确率 **101/120 = 84.17%**，Wilson 95% CI **[76.59%, 89.62%]**，调用错误 3 条。四字段准确率分别为：意图 95.83%、订单号 95.00%、金额 97.50%、全额标志 86.67%。物流、取消/否定、缺槽位和上下文场景均为 100%，但全额退款只有 2/15，暴露出一个集中失败族。

定位发现，OpenAI-compatible 路由发送的 JSON Schema 保留字段默认值、同时没有把所有字段列入 `required`。兼容服务会倾向省略 `full_refund_requested`，本地默认值再把缺失掩盖成 `false`。修复 wire schema 后，仅对受影响和相邻回归族做 45 条定向复测：**44/45 = 97.78%**，Wilson 95% CI **[88.43%, 99.61%]**，0 调用错误；全额退款从 **2/15 提升为 15/15**，否定/取消仍为 15/15，政策为 14/15。

这不是“120 条修复后总分 97.78%”：后一个数字只属于预先声明的 45 条定向复测。完整 120 条首次结果与定向回归分开报告，避免混淆样本口径。模型输出具有随机性，单次结果不保证逐次相同。

版本化聚合结果见 [`classification_eval_v2_results_20260915.json`](classification_eval_v2_results_20260915.json)；含逐条输入、输出和耗时的原始报告保存在执行当日的工作区 `outputs/2026-09-15/intermediate/`。

## 复跑

在仓库根目录配置真实模型环境后执行：

```powershell
uv run python scripts/build_ticketpilot_eval_dataset.py
uv run python scripts/evaluate_ticketpilot_classification.py --dry-run --dataset data/ticketpilot/evals/classification_eval_v2.json
uv run python scripts/evaluate_ticketpilot_classification.py --dataset data/ticketpilot/evals/classification_eval_v2.json --concurrency 4 --output <新结果路径>
uv run python scripts/evaluate_ticketpilot_classification.py --dataset data/ticketpilot/evals/classification_eval_v2.json --scenario full_refund --scenario policy_only --scenario negation_cancel --concurrency 4 --output <新结果路径>
```

真实运行可能产生模型供应商费用。输出文件不能覆盖已有文件；建议把逐条原始报告写到工作区 `outputs/YYYY-MM-DD/intermediate/`。报告不保存凭据、服务地址或原始异常消息。

## 边界

本评测只运行分类和字段抽取，不经过数据库、检索、审批、退款执行或最终回答生成，因而不代表端到端任务成功率。下一阶段最有价值的是冻结一套独立人工复核测试集、跑修复后的完整 120 条，以及为 grounded answer 增加 citation faithfulness 评测；不应通过删除难例来提高分数。

兼容服务的金额 wire schema 使用 `number | null`，绕开部分服务无法解析 `Decimal` 正则的问题；响应返回后仍由 `TicketClassification` 校验正数、最多两位小数和金额上限。
