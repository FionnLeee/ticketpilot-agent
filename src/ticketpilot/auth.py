from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from core import settings
from ticketpilot.errors import AuthenticationRequired
from ticketpilot.schemas import RequestPrincipal

ticketpilot_bearer = HTTPBearer(
    description="TicketPilot tenant-scoped bearer token", auto_error=False
)


def get_request_principal(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(ticketpilot_bearer),
    ],
) -> RequestPrincipal:
    if credentials is None:
        raise AuthenticationRequired

    principal_config = settings.TICKETPILOT_AUTH_TOKENS.get(credentials.credentials)
    if principal_config is None:
        raise AuthenticationRequired

    try:
        return RequestPrincipal.model_validate(principal_config)
    except ValidationError as exc:
        raise AuthenticationRequired from exc
