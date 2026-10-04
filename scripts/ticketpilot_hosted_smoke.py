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
        assert client.get("/", auth=auth).status_code == 200
        for mode, role in (("customer", "CUSTOMER"), ("approver", "APPROVER")):
            response = client.get("/api/v1/me", auth=auth, headers={"X-TicketPilot-Identity": mode})
            response.raise_for_status()
            assert response.json()["role"] == role
        assert client.get("/api/history", auth=auth).status_code == 404
        response = client.post(
            "/api/v1/tickets",
            auth=auth,
            headers={"X-TicketPilot-Identity": "customer", "Idempotency-Key": uuid4().hex},
            json={"subject": "Hosted smoke", "message": "我想查询物流"},
        )
        response.raise_for_status()
        assert response.json()["ticket"]["status"] == "WAITING_INFORMATION"
    print(
        "Hosted HTTPS smoke passed: login, bearer bypass rejection, both roles, route isolation, ticket execution."
    )


if __name__ == "__main__":
    main()
