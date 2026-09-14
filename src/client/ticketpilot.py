from typing import Any, Literal

import httpx

NIL_UUID = "00000000-0000-0000-0000-000000000000"

IdentityProbe = Literal["ok", "unauthorized", "forbidden", "unreachable", "error"]


class TicketPilotClientError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class TicketPilotClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.client = client

    def create_ticket(
        self,
        token: str,
        *,
        subject: str,
        message: str,
        idempotency_key: str,
        order_reference: str | None = None,
    ) -> dict[str, Any]:
        payload = {"subject": subject, "message": message}
        if order_reference:
            payload["order_reference"] = order_reference
        return self._request(
            "POST",
            "/v1/tickets",
            token,
            json=payload,
            headers={"Idempotency-Key": idempotency_key},
        )

    def get_ticket(self, token: str, ticket_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/tickets/{ticket_id}", token)

    def add_message(
        self,
        token: str,
        ticket_id: str,
        *,
        message: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/tickets/{ticket_id}/messages",
            token,
            json={"message": message},
            headers={"Idempotency-Key": idempotency_key},
        )

    def get_run_events(self, token: str, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/runs/{run_id}/events", token)

    def decide_approval(
        self,
        token: str,
        approval_id: str,
        *,
        decision: str,
        reason: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/approvals/{approval_id}:decide",
            token,
            json={"decision": decision, "reason": reason},
        )

    def get_info(self) -> dict[str, Any]:
        request = self.client.request if self.client is not None else httpx.request
        try:
            response = request("GET", f"{self.base_url}/info", timeout=self.timeout)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise TicketPilotClientError(f"TicketPilot service request failed: {exc}") from exc
        payload = response.json()
        if not isinstance(payload, dict):
            raise TicketPilotClientError("TicketPilot returned an invalid response")
        return payload

    def probe_identity(self, token: str) -> IdentityProbe:
        # A nil ticket id can never exist, so 404 proves the token was accepted
        # without touching real data.
        try:
            self._request("GET", f"/v1/tickets/{NIL_UUID}", token)
        except TicketPilotClientError as exc:
            if exc.status_code == 404:
                return "ok"
            if exc.status_code == 401:
                return "unauthorized"
            if exc.status_code == 403:
                return "forbidden"
            if exc.status_code is None:
                return "unreachable"
            return "error"
        return "ok"

    def _request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request = self.client.request if self.client is not None else httpx.request
        request_headers = {"Authorization": f"Bearer {token}"}
        if headers:
            request_headers.update(headers)
        try:
            response = request(
                method,
                f"{self.base_url}{path}",
                headers=request_headers,
                json=json,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            error = self._api_error(exc.response)
            raise TicketPilotClientError(
                error.get("message", f"TicketPilot returned HTTP {exc.response.status_code}"),
                code=error.get("code"),
                status_code=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise TicketPilotClientError(f"TicketPilot service request failed: {exc}") from exc

        payload = response.json()
        if not isinstance(payload, dict):
            raise TicketPilotClientError("TicketPilot returned an invalid response")
        return payload

    @staticmethod
    def _api_error(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            return {}
        if not isinstance(payload, dict):
            return {}
        error = payload.get("error")
        return error if isinstance(error, dict) else {}
