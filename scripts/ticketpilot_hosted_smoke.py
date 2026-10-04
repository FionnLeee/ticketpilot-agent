"""Verify the authenticated hosted demo without model calls."""

import os
import ssl
from uuid import uuid4

import httpx


def main() -> None:
    auth = httpx.BasicAuth(os.environ["DEMO_USERNAME"], os.environ["DEMO_PASSWORD"])
    verify = ssl.create_default_context(cafile=os.environ.get("DEMO_CA_FILE"))
    with httpx.Client(
        base_url=os.environ["DEMO_BASE_URL"], verify=verify, timeout=30, trust_env=False
    ) as client:
        assert client.get("/").status_code == 401
        assert (
            client.get(
                "/api/v1/me", headers={"Authorization": "Bearer demo-approver-token"}
            ).status_code
            == 401
        )
        page = client.get("/", auth=auth)
        assert page.status_code == 200, page.status_code
        for mode, role in (("customer", "CUSTOMER"), ("approver", "APPROVER")):
            response = client.get("/api/v1/me", auth=auth, headers={"X-TicketPilot-Identity": mode})
            response.raise_for_status()
            assert response.json()["role"] == role
        assert client.get("/api/history", auth=auth).status_code == 404
        headers = {"X-TicketPilot-Identity": "customer", "Idempotency-Key": uuid4().hex}
        payload = {
            "subject": "Hosted smoke",
            "message": "请查询订单 TP-0009 的物流和预计送达时间",
            "order_reference": "TP-0009",
        }
        response = client.post("/api/v1/tickets", auth=auth, headers=headers, json=payload)
        response.raise_for_status()
        result = response.json()
        assert result["ticket"]["status"] == "RESOLVED", result["ticket"]["status"]
        assert result["ticket"]["processing_result"] == "ANSWERED"
        replay = client.post("/api/v1/tickets", auth=auth, headers=headers, json=payload)
        replay.raise_for_status()
        assert replay.json()["ticket"]["id"] == result["ticket"]["id"]
        assert replay.json()["run_id"] == result["run_id"]
    print(
        "Hosted HTTPS smoke passed: login, bearer bypass rejection, both roles, route isolation, ticket execution."
    )


if __name__ == "__main__":
    main()
