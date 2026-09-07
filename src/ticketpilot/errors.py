from typing import Any


class TicketPilotError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


class AuthenticationRequired(TicketPilotError):
    def __init__(self) -> None:
        super().__init__(401, "UNAUTHORIZED", "A valid TicketPilot bearer token is required")


class Forbidden(TicketPilotError):
    def __init__(self, message: str = "This principal cannot perform the requested action") -> None:
        super().__init__(403, "FORBIDDEN", message)


class ResourceNotFound(TicketPilotError):
    def __init__(self, resource: str) -> None:
        super().__init__(404, "RESOURCE_NOT_FOUND", f"{resource} was not found")


class StateConflict(TicketPilotError):
    def __init__(self, message: str) -> None:
        super().__init__(409, "STATE_CONFLICT", message)


class TicketPilotUnavailable(TicketPilotError):
    def __init__(self) -> None:
        super().__init__(503, "DEPENDENCY_UNAVAILABLE", "TicketPilot database is unavailable")
