import argparse
import json
import sys
import time
from typing import Any
from uuid import uuid4

import httpx


class DemoFailure(RuntimeError):
    pass


class DemoClient:
    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self.client.close()

    def wait_until_ready(self, wait_seconds: float) -> None:
        deadline = time.monotonic() + wait_seconds
        last_error = "service did not respond"
        while time.monotonic() < deadline:
            try:
                response = self.client.get(f"{self.base_url}/health")
                if response.status_code == 200:
                    return
                last_error = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                last_error = str(exc)
            time.sleep(1)
        raise DemoFailure(f"TicketPilot did not become ready: {last_error}")

    def request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        expected_status: int = 200,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        response = self.client.request(
            method,
            f"{self.base_url}{path}",
            headers=headers,
            json=payload,
        )
        if response.status_code != expected_status:
            raise DemoFailure(
                f"{method} {path} expected {expected_status}, got "
                f"{response.status_code}: {response.text}"
            )
        body = response.json()
        if not isinstance(body, dict):
            raise DemoFailure(f"{method} {path} returned a non-object response")
        return body


def event_types(response: dict[str, Any]) -> list[str]:
    return [event["event_type"] for event in response.get("events", [])]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise DemoFailure(message)


def run_demo(args: argparse.Namespace) -> dict[str, Any]:
    client = DemoClient(args.base_url, args.timeout)
    try:
        client.wait_until_ready(args.wait_seconds)
        normal = client.request(
            "POST",
            "/v1/tickets",
            args.customer_token,
            expected_status=201,
            payload={
                "subject": "Day 14 物流查询",
                "message": "请查询订单 TP-0009 的物流和预计送达时间",
                "order_reference": "TP-0009",
            },
        )
        require(normal["ticket"]["status"] == "RESOLVED", "normal flow did not resolve")
        normal_events = client.request(
            "GET", f"/v1/runs/{normal['run_id']}/events", args.customer_token
        )
        require(
            {"RUN_STARTED", "TICKET_TRIAGED", "TOOL_SUCCEEDED", "TICKET_RESOLVED"}
            <= set(event_types(normal_events)),
            "normal flow audit evidence is incomplete",
        )

        message_key = f"day14-message-{uuid4()}"
        follow_up_payload = {"message": "请再次确认 TP-0009 当前状态"}
        follow_up = client.request(
            "POST",
            f"/v1/tickets/{normal['ticket']['id']}/messages",
            args.customer_token,
            payload=follow_up_payload,
            idempotency_key=message_key,
        )
        replay = client.request(
            "POST",
            f"/v1/tickets/{normal['ticket']['id']}/messages",
            args.customer_token,
            payload=follow_up_payload,
            idempotency_key=message_key,
        )
        require(
            follow_up["ticket"]["thread_id"] == normal["ticket"]["thread_id"],
            "follow-up changed the graph thread",
        )
        require(replay["run_id"] == follow_up["run_id"], "message retry was not idempotent")
        conflict = client.request(
            "POST",
            f"/v1/tickets/{normal['ticket']['id']}/messages",
            args.customer_token,
            payload={"message": "同一个键却换了正文"},
            idempotency_key=message_key,
            expected_status=409,
        )
        require(conflict.get("error", {}).get("code") == "STATE_CONFLICT", "wrong conflict")

        refund = client.request(
            "POST",
            "/v1/tickets",
            args.customer_token,
            expected_status=201,
            payload={
                "subject": "Day 14 退款申请",
                "message": "订单 TP-0005 申请退款 1 元",
                "order_reference": "TP-0005",
            },
        )
        require(
            refund["ticket"]["status"] == "WAITING_APPROVAL",
            "refund flow did not pause for approval",
        )
        approval = refund.get("pending_approval")
        require(isinstance(approval, dict), "refund flow did not return pending approval")
        refund_events = client.request(
            "GET", f"/v1/runs/{refund['run_id']}/events", args.customer_token
        )
        require(
            "REFUND_APPROVAL_REQUESTED" in event_types(refund_events),
            "refund pause was not audited",
        )

        hidden = client.request(
            "GET",
            f"/v1/runs/{refund['run_id']}/events",
            args.other_tenant_token,
            expected_status=404,
        )
        require(hidden.get("error", {}).get("code") == "RESOURCE_NOT_FOUND", "tenant leaked")

        approved = client.request(
            "POST",
            f"/v1/approvals/{approval['id']}:decide",
            args.approver_token,
            payload={"decision": "APPROVE", "reason": "Day 14 E2E 人工复核通过"},
        )
        require(approved["ticket"]["status"] == "RESOLVED", "approved refund did not resolve")
        approval_events = client.request(
            "GET", f"/v1/runs/{approved['run_id']}/events", args.approver_token
        )
        require(
            event_types(approval_events) == ["APPROVAL_DECIDED", "REFUND_EXECUTED"],
            "approved refund event sequence is incorrect",
        )

        repeated_approval = client.request(
            "POST",
            f"/v1/approvals/{approval['id']}:decide",
            args.approver_token,
            payload={"decision": "APPROVE", "reason": "Day 14 幂等重试"},
        )
        replay_events = client.request(
            "GET",
            f"/v1/runs/{repeated_approval['run_id']}/events",
            args.approver_token,
        )
        require(
            event_types(replay_events) == ["APPROVAL_DECISION_REPLAYED"],
            "approval retry was not safely replayed",
        )
        return {
            "status": "passed",
            "normal": {
                "ticket_id": normal["ticket"]["id"],
                "thread_id": normal["ticket"]["thread_id"],
                "initial_run_id": normal["run_id"],
                "follow_up_run_id": follow_up["run_id"],
            },
            "refund": {
                "ticket_id": refund["ticket"]["id"],
                "thread_id": refund["ticket"]["thread_id"],
                "initial_run_id": refund["run_id"],
                "approval_run_id": approved["run_id"],
                "approval_replay_run_id": repeated_approval["run_id"],
            },
            "assertions": [
                "normal consultation resolved with audit evidence",
                "follow-up reused thread and message retry reused run",
                "conflicting idempotency payload returned 409",
                "refund paused before approval and executed once after approval",
                "cross-tenant run lookup returned 404",
                "repeated approval produced replay evidence only",
            ],
        }
    finally:
        client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the TicketPilot Day 14 demo acceptance")
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--customer-token", default="demo-customer-token")
    parser.add_argument("--approver-token", default="demo-approver-token")
    parser.add_argument("--other-tenant-token", default="other-tenant-token")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--wait-seconds", type=float, default=90.0)
    return parser.parse_args()


def main() -> int:
    try:
        result = run_demo(parse_args())
    except (DemoFailure, httpx.HTTPError, KeyError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
