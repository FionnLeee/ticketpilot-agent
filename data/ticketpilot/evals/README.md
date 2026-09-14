# TicketPilot 分类开发集

18 条第一方合成中文样本，无真实客户数据。此开发集用于发现问题和对比修改，不能作为封存测试集或线上准确率证明。

## 评分契约

评分四项：意图、订单号、明确退款金额、全额退款标志。四项全对才计整条正确；调用异常和超时保留在分母内，另列 error_count。订单号忽略大小写，金额按 Decimal 数值比较。priority 不评分，因为现有提示词尚未定义统一优先级策略。

纯订单号按 ORDER_STATUS 标注；没有新订单号时保留已知订单。取消退款按 OTHER，明确查询物流时按 ORDER_STATUS。账户余额、实付额、日期与订单号中的数字不能代替申请金额。标签由项目开发者编写，尚无独立人工复核。

## 运行

在仓库根目录执行，先配置本地真实模型环境：

```powershell
uv run python scripts/evaluate_ticketpilot_classification.py --dry-run
uv run python scripts/evaluate_ticketpilot_classification.py --limit 3 --output <新的输出文件路径>
uv run python scripts/evaluate_ticketpilot_classification.py --output <新的完整结果文件路径>
```

真实运行会调用当前 DEFAULT_MODEL，可能产生供应商费用；顺序调用，每条超时 60 秒，SDK 内部可能重试。报告不记录凭据、地址或原始异常消息；保存逐条输入、期望、输出、评分、错误类型、耗时、模型名称、temperature、数据和 reasoner 哈希、Git 版本及工作区是否有修改。相同输入仍可能产生不同输出；复现实验条件不保证输出逐字相同。

输出文件不能覆盖已有文件。仓库外结果建议存到工作区 outputs/YYYY-MM-DD/intermediate/。dry-run 只验证数据，不调用模型。

## 范围

这里只评测真实 LangChainTicketReasoner 的分类与字段抽取，不运行数据库、检索、审批、退款或答案生成，不代表端到端任务成功率。后续应补独立封存集、按场景统计和端到端证据；保留错误样本，不能为提高分数删除难例。

兼容服务的金额 wire schema 使用 number/null，避开部分服务无法解析的 Decimal 正则；返回后仍由原 TicketClassification 校验正数、最多两位小数和金额上限。其他供应商路径保持原有结构化输出方式。
