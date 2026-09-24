from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, status
from fastapi.responses import JSONResponse

from core.settings import TicketPilotReasonerMode, settings
from ticketpilot.auth import get_request_principal
from ticketpilot.db import BusinessPool
from ticketpilot.errors import TicketPilotError, TicketPilotUnavailable
from ticketpilot.observability import ExecutionLimits
from ticketpilot.orders import PostgresOrderRepository
from ticketpilot.reasoning import DeterministicDemoReasoner, LangChainTicketReasoner
from ticketpilot.repositories import TicketRepository
from ticketpilot.schemas import (
    AddTicketMessageRequest,
    ApprovalDecisionRequest,
    ApprovalListResponse,
    CreateTicketRequest,
    DashboardSummary,
    ErrorResponse,
    IdentityResponse,
    RequestPrincipal,
    RunEventsResponse,
    TicketDetail,
    TicketListResponse,
    TicketRunResult,
)
from ticketpilot.services import TicketService
from ticketpilot.workflow_repository import TicketWorkflowRepository

router = APIRouter(prefix="/v1", tags=["ticketpilot"])


def get_business_pool(request: Request) -> BusinessPool:
    pool = getattr(request.app.state, "ticketpilot_pool", None)
    if pool is None:
        raise TicketPilotUnavailable
    return pool


def get_ticket_service(
    request: Request,
    pool: Annotated[BusinessPool, Depends(get_business_pool)],
) -> TicketService:
    workflow = getattr(request.app.state, "ticketpilot_graph", None)
    if workflow is None:
        return TicketService(TicketRepository(pool))
    reasoner = (
        DeterministicDemoReasoner()
        if settings.TICKETPILOT_REASONER_MODE is TicketPilotReasonerMode.DETERMINISTIC_DEMO
        else LangChainTicketReasoner()
    )
    return TicketService(
        TicketRepository(pool),
        workflow=workflow,
        workflow_repository=TicketWorkflowRepository(pool),
        order_reader=PostgresOrderRepository(pool),
        policy_retriever=request.app.state.ticketpilot_retriever,
        reasoner=reasoner,
        execution_limits=ExecutionLimits(
            deadline_seconds=settings.TICKETPILOT_RUN_DEADLINE,
            call_timeout_seconds=settings.TICKETPILOT_CALL_TIMEOUT,
            max_model_calls=settings.TICKETPILOT_MAX_MODEL_CALLS,
            max_tokens=settings.TICKETPILOT_TOKEN_BUDGET,
        ),
    )


@router.get("/me", response_model=IdentityResponse, responses={401: {"model": ErrorResponse}})
async def get_identity(
    principal: Annotated[RequestPrincipal, Depends(get_request_principal)],
) -> IdentityResponse:
    return IdentityResponse(**principal.model_dump())


@router.get(
    "/dashboard/summary",
    response_model=DashboardSummary,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def get_dashboard_summary(
    principal: Annotated[RequestPrincipal, Depends(get_request_principal)],
    service: Annotated[TicketService, Depends(get_ticket_service)],
) -> DashboardSummary:
    return await service.get_dashboard(principal)


@router.get(
    "/tickets",
    response_model=TicketListResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def list_tickets(
    principal: Annotated[RequestPrincipal, Depends(get_request_principal)],
    service: Annotated[TicketService, Depends(get_ticket_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 40,
) -> TicketListResponse:
    return await service.list_tickets(principal, limit)


@router.get(
    "/approvals",
    response_model=ApprovalListResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def list_approvals(
    principal: Annotated[RequestPrincipal, Depends(get_request_principal)],
    service: Annotated[TicketService, Depends(get_ticket_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 40,
) -> ApprovalListResponse:
    return await service.list_approvals(principal, limit)


@router.post(
    "/tickets",
    response_model=TicketRunResult,
    status_code=status.HTTP_201_CREATED,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def create_ticket(
    payload: CreateTicketRequest,
    principal: Annotated[RequestPrincipal, Depends(get_request_principal)],
    service: Annotated[TicketService, Depends(get_ticket_service)],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=128),
    ],
) -> TicketRunResult:
    return await service.create_ticket(principal, payload, idempotency_key)


@router.get(
    "/tickets/{ticket_id}",
    response_model=TicketDetail,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def get_ticket(
    ticket_id: UUID,
    principal: Annotated[RequestPrincipal, Depends(get_request_principal)],
    service: Annotated[TicketService, Depends(get_ticket_service)],
) -> TicketDetail:
    return await service.get_ticket(principal, ticket_id)


@router.post(
    "/tickets/{ticket_id}/messages",
    response_model=TicketRunResult,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def add_ticket_message(
    ticket_id: UUID,
    payload: AddTicketMessageRequest,
    principal: Annotated[RequestPrincipal, Depends(get_request_principal)],
    service: Annotated[TicketService, Depends(get_ticket_service)],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=128),
    ],
) -> TicketRunResult:
    return await service.add_message(principal, ticket_id, payload, idempotency_key)


@router.get(
    "/runs/{run_id}/events",
    response_model=RunEventsResponse,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def get_run_events(
    run_id: UUID,
    principal: Annotated[RequestPrincipal, Depends(get_request_principal)],
    service: Annotated[TicketService, Depends(get_ticket_service)],
) -> RunEventsResponse:
    return await service.get_run_events(principal, run_id)


@router.post(
    "/approvals/{approval_id}:decide",
    response_model=TicketRunResult,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def decide_approval(
    approval_id: UUID,
    payload: ApprovalDecisionRequest,
    principal: Annotated[RequestPrincipal, Depends(get_request_principal)],
    service: Annotated[TicketService, Depends(get_ticket_service)],
) -> TicketRunResult:
    return await service.decide_approval(principal, approval_id, payload)


async def ticketpilot_error_handler(_: Request, exc: TicketPilotError) -> JSONResponse:
    response = ErrorResponse(
        error={"code": exc.code, "message": exc.message, "details": exc.details}
    )
    return JSONResponse(status_code=exc.status_code, content=response.model_dump(mode="json"))


def install_ticketpilot_api(app: FastAPI) -> None:
    app.add_exception_handler(TicketPilotError, ticketpilot_error_handler)  # type: ignore[arg-type]
    app.include_router(router)
