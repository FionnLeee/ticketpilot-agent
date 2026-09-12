import httpx
import pytest

from client import TicketPilotClient, TicketPilotClientError


def test_ticketpilot_client_sends_bearer_token_and_reads_events() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"run_id": "run-1", "events": [{"event_type": "RUN_STARTED"}]},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = TicketPilotClient("http://ticketpilot.test/", client=http_client)
        result = client.get_run_events("opaque-token", "run-1")

    assert result["events"][0]["event_type"] == "RUN_STARTED"
    assert requests[0].url == "http://ticketpilot.test/v1/runs/run-1/events"
    assert requests[0].headers["Authorization"] == "Bearer opaque-token"


def test_ticketpilot_client_add_message_sends_idempotency_key() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"run_id": "run-2", "ticket": {}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = TicketPilotClient("http://ticketpilot.test", client=http_client)
        client.add_message(
            "opaque-token",
            "ticket-1",
            message="继续查询",
            idempotency_key="message-attempt-1",
        )

    assert requests[0].url == "http://ticketpilot.test/v1/tickets/ticket-1/messages"
    assert requests[0].headers["Idempotency-Key"] == "message-attempt-1"
    assert requests[0].headers["Authorization"] == "Bearer opaque-token"
    assert requests[0].read() == b'{"message":"\xe7\xbb\xa7\xe7\xbb\xad\xe6\x9f\xa5\xe8\xaf\xa2"}'


def test_ticketpilot_client_create_ticket_sends_idempotency_key() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"run_id": "run-1", "ticket": {}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = TicketPilotClient("http://ticketpilot.test", client=http_client)
        client.create_ticket(
            "opaque-token",
            subject="查询物流",
            message="订单在哪里？",
            idempotency_key="create-attempt-1",
        )

    assert requests[0].url == "http://ticketpilot.test/v1/tickets"
    assert requests[0].headers["Idempotency-Key"] == "create-attempt-1"
    assert requests[0].headers["Authorization"] == "Bearer opaque-token"


def test_ticketpilot_client_surfaces_structured_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            request=request,
            json={
                "error": {
                    "code": "RESOURCE_NOT_FOUND",
                    "message": "Run was not found",
                    "details": {},
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = TicketPilotClient("http://ticketpilot.test", client=http_client)
        with pytest.raises(TicketPilotClientError) as raised:
            client.get_run_events("opaque-token", "missing-run")

    assert raised.value.code == "RESOURCE_NOT_FOUND"
    assert raised.value.status_code == 404
    assert str(raised.value) == "Run was not found"
