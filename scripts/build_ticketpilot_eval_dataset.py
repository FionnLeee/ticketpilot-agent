import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data/ticketpilot/evals/classification_eval_v2.json"


def expected(category, order=None, amount=None, full=False):
    return {
        "category": category,
        "order_reference": order,
        "requested_refund_amount": amount,
        "full_refund_requested": full,
    }


cases = []


def add(family, messages, category, *, difficulty="medium"):
    for message, order, known, amount, full in messages:
        cases.append(
            {
                "case_id": f"E{len(cases) + 1:03d}",
                "scenario_family": family,
                "difficulty": difficulty,
                "message": message,
                "known_order_reference": known,
                "expected": expected(category, order, amount, full),
            }
        )


def add_mixed(family, messages, *, difficulty="medium"):
    for message, category, order, known, amount, full in messages:
        cases.append(
            {
                "case_id": f"E{len(cases) + 1:03d}",
                "scenario_family": family,
                "difficulty": difficulty,
                "message": message,
                "known_order_reference": known,
                "expected": expected(category, order, amount, full),
            }
        )


add(
    "logistics",
    [
        ("订单 TP-1001 到哪儿了？", "TP-1001", None, None, False),
        ("帮我看看 O-CN-8842 什么时候能送到", "O-CN-8842", None, None, False),
        ("快递显示三天没动，单号 TP-1099", "TP-1099", None, None, False),
        ("查下订单 TP-2003 的签收状态", "TP-2003", None, None, False),
        ("O-SH-20260915-77", "O-SH-20260915-77", None, None, False),
        ("这个包裹是哪个承运商？", "TP-3001", "TP-3001", None, False),
        ("预计送达时间有变化吗", "TP-3002", "TP-3002", None, False),
        ("我只想查物流，订单 TP-3003", "TP-3003", None, None, False),
        ("where is order O-INTL-91", "O-INTL-91", None, None, False),
        ("TP-4110 现在是已发货还是处理中？", "TP-4110", None, None, False),
        ("之前那单什么时候送达", "TP-4111", "TP-4111", None, False),
        ("物流轨迹断了，麻烦查 TP-4112", "TP-4112", None, None, False),
        ("订单 O-A9-B8-C7 的配送信息", "O-A9-B8-C7", None, None, False),
        ("查新订单 TP-5002，不是之前那单", "TP-5002", "TP-5001", None, False),
        ("我昨天下单的 TP-5003 发货了吗", "TP-5003", None, None, False),
        ("能给我 TP-5004 的脱敏运单号吗", "TP-5004", None, None, False),
        ("只查询，不做任何变更：O-READ-88", "O-READ-88", None, None, False),
        ("包裹还在路上吗？", "TP-5005", "TP-5005", None, False),
        ("订单 TP-5006 是不是取消了", "TP-5006", None, None, False),
        ("麻烦确认 O-2026-X1 当前履约状态", "O-2026-X1", None, None, False),
    ],
    "ORDER_STATUS",
)

amount_rows = [
    ("订单 TP-6101 退 20 元", "TP-6101", None, "20", False),
    ("O-R-6102 请退款12.50元", "O-R-6102", None, "12.50", False),
    ("TP-6103 我只要退0.01元", "TP-6103", None, "0.01", False),
    ("这单退一百块，订单 TP-6104", "TP-6104", None, "100", False),
    ("订单 TP-6105 实付500元，请退30元", "TP-6105", None, "30", False),
    ("9月15日买的 TP-6106，退款15元", "TP-6106", None, "15", False),
    ("把 O-R-6107 的 88.80 CNY 退回来", "O-R-6107", None, "88.80", False),
    ("上次说的订单退6元", "TP-6108", "TP-6108", "6", False),
    ("不是60元，是退16元，TP-6109", "TP-6109", None, "16", False),
    ("订单号 TP-6110，账户余额900元，本次退款9元", "TP-6110", None, "9", False),
    ("请为 O-R-6111 发起 120.25 元退款", "O-R-6111", None, "120.25", False),
    ("TP-6112 退人民币7块5毛", "TP-6112", None, "7.5", False),
    ("订单 TP-6113 退 7.5 元，不是全额", "TP-6113", None, "7.5", False),
    ("只退25元，其余不要动，TP-6114", "TP-6114", None, "25", False),
    ("refund 19 yuan for order O-R-6115", "O-R-6115", None, "19", False),
    ("O-R-6116 退款 3.33", "O-R-6116", None, "3.33", False),
    ("订单 TP-6117 请退3.33元", "TP-6117", None, "3.33", False),
    ("之前 TP-0001 不管了，给 TP-6118 退40元", "TP-6118", "TP-0001", "40", False),
    ("退 1 元测试一下，订单 O-R-6119", "O-R-6119", None, "1", False),
    ("我要为 TP-6120 申请部分退款 200 元", "TP-6120", None, "200", False),
    ("订单 TP-6121 的商品不对，退55.55元", "TP-6121", None, "55.55", False),
    ("请原路退回 18 元到 TP-6122 的支付账户", "TP-6122", None, "18", False),
    ("TP-6123 本次明确申请退款 66 元", "TP-6123", None, "66", False),
    ("我想退2块钱，单子是 O-R-6124", "O-R-6124", None, "2", False),
    ("订单 TP-6125，支付日是12号，只退12.01元", "TP-6125", None, "12.01", False),
]
add("refund_amount", amount_rows, "REFUND", difficulty="hard")

add(
    "full_refund",
    [
        ("订单 TP-7001 我要全额退款", "TP-7001", None, None, True),
        ("O-FULL-02 支付的钱全部退给我", "O-FULL-02", None, None, True),
        ("TP-7003 整单金额原路退回", "TP-7003", None, None, True),
        ("这单我一分钱都不留，全部退款", "TP-7004", "TP-7004", None, True),
        ("full refund for O-FULL-05", "O-FULL-05", None, None, True),
        ("订单 TP-7006 请按可退余额全部退", "TP-7006", None, None, True),
        ("我要退掉整笔付款，TP-7007", "TP-7007", None, None, True),
        ("O-FULL-08 申请全退，不是部分退款", "O-FULL-08", None, None, True),
        ("把上一单的所有可退款金额退回", "TP-7009", "TP-7009", None, True),
        ("TP-7010 全部退回支付账户", "TP-7010", None, None, True),
        ("订单 O-FULL-11 我要求整单退款", "O-FULL-11", None, None, True),
        ("不接受换货，TP-7012 请全额退款", "TP-7012", None, None, True),
        ("我要全退 O-FULL-13 的款项", "O-FULL-13", None, None, True),
        ("TP-7014 按剩余可退金额全部退掉", "TP-7014", None, None, True),
        ("这次不是问规则，订单 TP-7015 确认申请全额退款", "TP-7015", None, None, True),
    ],
    "REFUND",
    difficulty="hard",
)

add(
    "refund_missing_slot",
    [
        ("订单 TP-7201 我要退款，金额稍后说", "TP-7201", None, None, False),
        ("帮我发起退款", None, None, None, False),
        ("上一单想退一部分", "TP-7203", "TP-7203", None, False),
        ("O-PART-04 退款，但不是全退", "O-PART-04", None, None, False),
        ("这个订单能给我退钱吗？我要申请，不是问政策", "TP-7205", "TP-7205", None, False),
        ("TP-7206 实付300元，我想退款，数额还没定", "TP-7206", None, None, False),
        ("申请退款 O-PART-07", "O-PART-07", None, None, False),
        ("东西坏了，我要退费", None, None, None, False),
        ("TP-7209 先登记退款诉求", "TP-7209", None, None, False),
        ("我要把这单退掉，但暂时不要猜金额", "TP-7210", "TP-7210", None, False),
    ],
    "REFUND",
    difficulty="hard",
)

add(
    "policy_only",
    [
        ("退款规则是什么？", None, None, None, False),
        ("签收超过七天还能退吗", None, None, None, False),
        ("我只是咨询全额退款条件，不要发起申请", None, None, None, False),
        ("订单 TP-8004 适用哪条退款政策？先不退", "TP-8004", None, None, False),
        ("退货运费由谁承担", None, None, None, False),
        ("优惠券退款后会返还吗", None, None, None, False),
        ("换货的时限是多少天", None, None, None, False),
        ("O-POLICY-08 是否支持部分退款？仅咨询", "O-POLICY-08", None, None, False),
        ("保修政策覆盖人为损坏吗", None, None, None, False),
        ("发票开出后退款有什么规则", None, None, None, False),
        ("积分抵扣部分如何计算退款", None, None, None, False),
        ("跨境订单的退货期限", None, None, None, False),
        ("我不申请退款，只想知道政策", None, None, None, False),
        ("全额退款的审核条件有哪些", None, None, None, False),
        ("上一个订单 TP-8015 的规则是什么", "TP-8015", None, None, False),
    ],
    "POLICY",
    difficulty="hard",
)

add_mixed(
    "negation_cancel",
    [
        ("订单 TP-9001 不要退款，只查物流", "ORDER_STATUS", "TP-9001", None, None, False),
        ("取消退款申请，不用退了", "OTHER", "TP-9002", "TP-9002", None, False),
        (
            "O-NO-03 no refund, tell me delivery status",
            "ORDER_STATUS",
            "O-NO-03",
            None,
            None,
            False,
        ),
        ("别退钱，帮我看 TP-9004 到哪了", "ORDER_STATUS", "TP-9004", None, None, False),
        ("撤销刚才对 TP-9005 的退款", "OTHER", "TP-9005", None, None, False),
        ("我没说要退款，只是在描述问题", "OTHER", None, None, None, False),
        ("暂时不退了，谢谢", "OTHER", "TP-9007", "TP-9007", None, False),
        ("订单 O-NO-08 的退款不要执行", "OTHER", "O-NO-08", None, None, False),
        ("不是申请退款，是问物流 TP-9009", "ORDER_STATUS", "TP-9009", None, None, False),
        ("请停止 TP-9010 的退费流程", "OTHER", "TP-9010", None, None, False),
        ("退款两个字不要触发任何动作", "OTHER", None, None, None, False),
        ("我改变主意了，不退 TP-9012", "OTHER", "TP-9012", None, None, False),
        ("不要全额退款，只告诉我政策", "POLICY", None, None, None, False),
        ("取消，不需要退回20元", "OTHER", "TP-9014", "TP-9014", None, False),
        ("TP-9015 不退款，继续正常发货", "OTHER", "TP-9015", None, None, False),
    ],
    difficulty="hard",
)

add(
    "other",
    [
        ("谢谢你的帮助", None, None, None, False),
        ("怎么修改收货地址", None, None, None, False),
        ("人工客服几点上班", None, None, None, False),
        ("商品有色差", None, None, None, False),
        ("帮我催一下", None, None, None, False),
        ("你好", None, None, None, False),
        ("我想投诉配送员", None, None, None, False),
        ("订单 TP-9908 的备注能改吗", "TP-9908", None, None, False),
        ("请转人工", None, None, None, False),
        ("这个回答解决了我的问题", None, None, None, False),
    ],
    "OTHER",
)

add(
    "context_reference",
    [
        ("TP-9501", "TP-9501", None, None, False),
        ("O-BARE-02", "O-BARE-02", None, None, False),
        ("查新单 TP-9503", "TP-9503", "TP-0001", None, False),
        ("还是之前那一单", "TP-9504", "TP-9504", None, False),
        ("订单号是 TP-9505", "TP-9505", None, None, False),
        ("not O-OLD-1, check O-NEW-6", "O-NEW-6", "O-OLD-1", None, False),
        ("就这个订单", "TP-9507", "TP-9507", None, False),
        ("麻烦看一下 O-CONTEXT-8", "O-CONTEXT-8", None, None, False),
        ("我说的是 TP-9509", "TP-9509", "TP-9508", None, False),
        ("TP-9510？", "TP-9510", None, None, False),
    ],
    "ORDER_STATUS",
    difficulty="hard",
)

if len(cases) != 120:
    raise RuntimeError(f"Expected 120 cases, got {len(cases)}")

payload = {
    "dataset_id": "ticketpilot-classification-eval-v2",
    "split": "evaluation",
    "provenance": "First-party synthetic holdout authored for TicketPilot; no real customer data.",
    "label_policy": (
        "Exact match over category, order reference, explicit amount and full-refund flag. "
        "Dates, balances and negated amounts are not refund amounts. Explicit references override "
        "conversation context; otherwise known context is preserved."
    ),
    "cases": cases,
}
OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"output": str(OUTPUT), "cases": len(cases)}))
