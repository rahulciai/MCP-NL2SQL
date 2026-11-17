"""FastAPI application for the Workspace Agent bridge and FastMCP app."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.auth import AuthenticatedCaller, require_bearer
from core.chat_service import AcceptedChat, ChatService, ChatServiceError
from core.server_manifest import mcp
from core.session_store import (
    OwnershipError,
    RequestNotFound,
    RequestRecord,
    SessionNotFound,
    SessionStore,
)
from core.settings import AppSettings
from core.workspace_agent_client import WorkspaceAgentClient
from tools.callback_tools import configure_callback_store


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: str | None = None


class ChatAcceptedResponse(BaseModel):
    request_id: str
    session_id: str
    status: str


class RequestStatusResponse(BaseModel):
    request_id: str
    session_id: str
    status: str
    agent_status: str | None = None
    callback_status: str
    agent_run_id: str | None = None
    conversation_url: str | None = None
    answer: str | None = None
    data: list[dict[str, Any]] = Field(default_factory=list)
    sql: str | None = None
    error: dict[str, str] | None = None
    created_at: str
    updated_at: str


class RequestListItem(BaseModel):
    request_id: str
    status: str
    agent_status: str | None = None
    callback_status: str
    created_at: str
    updated_at: str


class RequestListResponse(BaseModel):
    session_id: str
    items: list[RequestListItem]
    limit: int
    offset: int
    next_offset: int | None = None


def _error_payload(exc: ChatServiceError) -> dict[str, Any]:
    error: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.request_id:
        error["request_id"] = exc.request_id
    if exc.session_id:
        error["session_id"] = exc.session_id
    return {"error": error}


def _request_status_payload(record: RequestRecord) -> RequestStatusResponse:
    answer = record.answer
    error = None
    if record.error_code:
        error = {
            "code": record.error_code,
            "message": record.error_message or "The request did not complete.",
        }
    return RequestStatusResponse(
        request_id=record.request_id,
        session_id=record.session_id,
        status=record.status,
        agent_status=record.agent_status,
        callback_status=record.callback_status,
        agent_run_id=record.trigger_run_id,
        conversation_url=record.conversation_url,
        answer=answer,
        data=record.data,
        sql=record.sql,
        error=error,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _accepted_payload(result: AcceptedChat) -> ChatAcceptedResponse:
    return ChatAcceptedResponse(
        request_id=result.request_id,
        session_id=result.session_id,
        status=result.status,
    )


def create_app(
    *,
    settings: AppSettings | None = None,
    store: SessionStore | None = None,
    agent_client: WorkspaceAgentClient | Any | None = None,
    mount_mcp: bool = True,
) -> FastAPI:
    settings = settings or AppSettings.from_env()
    store = store or SessionStore(
        settings.session_db_path,
        max_answer_chars=settings.max_answer_chars,
        max_sql_chars=settings.max_sql_chars,
        max_data_bytes=settings.max_data_bytes,
    )
    agent_client = agent_client or WorkspaceAgentClient(settings)
    configure_callback_store(store)
    chat_service = ChatService(
        settings=settings,
        store=store,
        agent_client=agent_client,
    )

    mcp_app = (
        mcp.http_app(path="/mcp", transport="streamable-http")
        if mount_mcp
        else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if mcp_app is None:
            yield
            return
        async with mcp_app.lifespan(app):
            yield

    app = FastAPI(title="MMM NL2SQL Workspace Agent API", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.chat_service = chat_service
    app.state.agent_client = agent_client

    @app.exception_handler(ChatServiceError)
    async def chat_service_error_handler(
        request: Request,
        exc: ChatServiceError,
    ) -> JSONResponse:
        del request
        return JSONResponse(status_code=exc.http_status, content=_error_payload(exc))

    @app.exception_handler(HTTPException)
    async def http_error_handler(
        request: Request,
        exc: HTTPException,
    ) -> JSONResponse:
        del request
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            content = {"error": detail}
        else:
            content = {
                "error": {
                    "code": "http_error",
                    "message": str(detail),
                }
            }
        return JSONResponse(status_code=exc.status_code, content=content)

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "configuration_error", "message": str(exc)}},
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "workspace_agent_configured": settings.workspace_agent_configured,
            "chat_auth_configured": settings.has_chat_auth,
            "mcp_callback_registered": True,
        }

    @app.post(
        "/chat",
        response_model=ChatAcceptedResponse,
        status_code=202,
    )
    async def chat(
        payload: ChatRequest,
        caller: AuthenticatedCaller = Depends(require_bearer),
    ) -> ChatAcceptedResponse:
        result = await chat_service.accept_chat(
            caller_id=caller.caller_id,
            session_id=payload.session_id,
            message=payload.message,
        )
        return _accepted_payload(result)

    @app.get(
        "/chat/requests/{request_id}",
        response_model=RequestStatusResponse,
    )
    async def get_chat_request(
        request_id: str,
        caller: AuthenticatedCaller = Depends(require_bearer),
    ) -> RequestStatusResponse:
        try:
            record = await chat_service.refresh_request(
                caller_id=caller.caller_id,
                request_id=request_id,
            )
        except RequestNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "request_not_found",
                    "message": "Request was not found.",
                },
            ) from exc
        return _request_status_payload(record)

    @app.get(
        "/chat/sessions/{session_id}/requests",
        response_model=RequestListResponse,
    )
    async def list_chat_requests(
        session_id: str,
        caller: AuthenticatedCaller = Depends(require_bearer),
        limit: int = Query(default=25, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=100_000),
    ) -> RequestListResponse:
        try:
            records, has_more = store.list_requests_for_session(
                session_id=session_id,
                caller_id=caller.caller_id,
                limit=limit,
                offset=offset,
            )
        except (RequestNotFound, SessionNotFound, OwnershipError) as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "session_not_found",
                    "message": "Session was not found.",
                },
            ) from exc
        return RequestListResponse(
            session_id=session_id,
            items=[
                RequestListItem(
                    request_id=record.request_id,
                    status=record.status,
                    agent_status=record.agent_status,
                    callback_status=record.callback_status,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                )
                for record in records
            ],
            limit=limit,
            offset=offset,
            next_offset=offset + limit if has_more else None,
        )

    if mcp_app is not None:
        app.mount("/", mcp_app)

    return app


settings = AppSettings.from_env()
app = create_app(settings=settings)
