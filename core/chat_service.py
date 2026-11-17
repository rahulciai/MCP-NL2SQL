"""Application service for the authenticated Workspace Agent bridge."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from core.session_store import (
    ActiveRequestError,
    OwnershipError,
    RequestNotFound,
    RequestRecord,
    SessionNotFound,
    SessionStore,
)
from core.settings import AppSettings
from core.workspace_agent_client import (
    RunStatus,
    WorkspaceAgentClient,
    WorkspaceAgentError,
)
from core.workspace_agent_prompt import build_trigger_input


class ChatServiceError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        http_status: int,
        request_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status
        self.request_id = request_id
        self.session_id = session_id


@dataclass(frozen=True)
class AcceptedChat:
    request_id: str
    session_id: str
    status: str


class ChatService:
    """Create trigger requests and refresh durable callback state.

    The Workspace Agent API is asynchronous and does not return the agent's
    answer. This service therefore returns after trigger acceptance; the
    SQLite record and ``submit_answer`` callback are the source of truth.
    """

    _TERMINAL_STATUSES = {
        "completed",
        "failed",
        "callback_missing",
        "callback_expired",
    }

    def __init__(
        self,
        *,
        settings: AppSettings,
        store: SessionStore,
        agent_client: WorkspaceAgentClient | Any,
    ) -> None:
        self.settings = settings
        self.store = store
        self.agent_client = agent_client
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_locks_guard = asyncio.Lock()

    async def _lock_for_session(self, session_id: str) -> asyncio.Lock:
        async with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[session_id] = lock
            return lock

    def _validate_message(self, message: str) -> str:
        if not isinstance(message, str) or not message.strip():
            raise ChatServiceError(
                "message must be a non-empty string.",
                code="invalid_message",
                http_status=400,
            )
        if len(message) > self.settings.max_message_chars:
            raise ChatServiceError(
                "message exceeds the configured size limit.",
                code="message_too_large",
                http_status=413,
            )
        return message.strip()

    @staticmethod
    def _session_error(
        exc: Exception,
        *,
        session_id: str,
    ) -> ChatServiceError:
        if isinstance(exc, OwnershipError):
            return ChatServiceError(
                "Session belongs to another caller.",
                code="session_forbidden",
                http_status=403,
                session_id=session_id,
            )
        return ChatServiceError(
            "Session was not found.",
            code="session_not_found",
            http_status=404,
            session_id=session_id,
        )

    def _callback_deadline_seconds(self) -> float:
        # The shorter of the request and legacy polling caps is persisted for
        # each request, so a restart cannot turn this into an unbounded wait.
        return min(
            self.settings.request_timeout_seconds,
            self.settings.agent_poll_timeout_seconds,
        )

    async def accept_chat(
        self,
        *,
        caller_id: str,
        session_id: str | None,
        message: str,
    ) -> AcceptedChat:
        message = self._validate_message(message)
        if session_id:
            try:
                session = self.store.get_session_for_caller(session_id, caller_id)
            except (OwnershipError, SessionNotFound) as exc:
                raise self._session_error(exc, session_id=session_id) from exc
        else:
            session = self.store.create_session(caller_id)

        if not session.conversation_key:
            raise ChatServiceError(
                "This session has no Workspace Agent conversation key; start a new session.",
                code="session_state_missing",
                http_status=409,
                session_id=session.session_id,
            )

        lock = await self._lock_for_session(session.session_id)
        request_id: str | None = None
        await lock.acquire()
        try:
            try:
                pending = self.store.create_request(
                    caller_id=caller_id,
                    session_id=session.session_id,
                    conversation_key=session.conversation_key,
                    message=message,
                    callback_token_ttl_seconds=self.settings.callback_token_ttl_seconds,
                    callback_deadline_seconds=self._callback_deadline_seconds(),
                )
            except ActiveRequestError as exc:
                raise ChatServiceError(
                    str(exc),
                    code="session_request_in_progress",
                    http_status=409,
                    request_id=exc.request_id,
                    session_id=session.session_id,
                ) from exc

            request_id = pending.record.request_id
            self.store.mark_triggering(request_id)
            trigger_input = build_trigger_input(
                request_id=request_id,
                delivery_token=pending.delivery_token,
                user_message=message,
            )
            try:
                trigger = await asyncio.wait_for(
                    self.agent_client.trigger(
                        conversation_key=session.conversation_key,
                        input_text=trigger_input,
                        idempotency_key=pending.record.idempotency_key,
                    ),
                    timeout=self.settings.agent_http_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                self.store.mark_trigger_unknown(
                    request_id,
                    "agent_http_timeout",
                    "The Workspace Agent trigger timed out. The persisted idempotency key was not retried automatically.",
                )
                raise ChatServiceError(
                    "The Workspace Agent trigger timed out. The request was not automatically resent.",
                    code="agent_http_timeout",
                    http_status=504,
                    request_id=request_id,
                    session_id=session.session_id,
                ) from exc
            except WorkspaceAgentError as exc:
                if exc.code in {"agent_http_timeout", "agent_connection_failed"}:
                    self.store.mark_trigger_unknown(request_id, exc.code, exc.message)
                else:
                    self.store.mark_failed(request_id, exc.code, exc.message)
                raise ChatServiceError(
                    exc.message,
                    code=exc.code,
                    http_status=exc.http_status,
                    request_id=request_id,
                    session_id=session.session_id,
                ) from exc

            # The callback may have won the race while the trigger HTTP call
            # was still in flight. The store preserves completed callback state.
            self.store.mark_trigger_accepted(
                request_id,
                trigger.run_id,
                trigger.status or "queued",
                trigger.conversation_url,
            )
            return AcceptedChat(
                request_id=request_id,
                session_id=session.session_id,
                status="queued",
            )
        except RequestNotFound as exc:
            raise ChatServiceError(
                "The request state disappeared before trigger acceptance.",
                code="request_state_missing",
                http_status=500,
                request_id=request_id,
                session_id=session.session_id,
            ) from exc
        finally:
            lock.release()

    async def refresh_request(
        self,
        *,
        caller_id: str,
        request_id: str,
    ) -> RequestRecord:
        """Refresh one request from the optional run-status API.

        Callback completion always wins. Every store transition is guarded by
        ``callback_status`` so a late status response cannot overwrite a saved
        answer.
        """
        record = self.store.get_request_for_caller(request_id, caller_id)
        if record.status in self._TERMINAL_STATUSES:
            return record

        now = time.time()
        if record.trigger_run_id:
            try:
                run_status = await asyncio.wait_for(
                    self.agent_client.get_run_status(record.trigger_run_id),
                    timeout=self.settings.agent_http_timeout_seconds,
                )
            except asyncio.TimeoutError:
                run_status = None
            except WorkspaceAgentError as exc:
                if exc.code in {
                    "agent_authentication_failed",
                    "agent_permission_denied",
                    "agent_trigger_not_found",
                }:
                    return self.store.mark_failed(
                        request_id,
                        exc.code,
                        exc.message,
                    )
                run_status = None

            current = self.store.get_request_for_caller(request_id, caller_id)
            if current.callback_status == "completed" or current.status == "completed":
                return current
            if run_status is not None:
                current = self._record_run_status(current, run_status)
                if current.status in self._TERMINAL_STATUSES:
                    return current
                if run_status.status == "suspended":
                    if now >= current.callback_deadline_at:
                        return self.store.mark_callback_expired(
                            request_id,
                            "The Workspace Agent remained suspended until the callback deadline expired.",
                        )
                    return current
                if run_status.status == "completed":
                    completed_at = current.agent_status_updated_at or now
                    if now >= min(
                        current.callback_deadline_at,
                        completed_at + self.settings.callback_grace_seconds,
                    ):
                        if now >= current.callback_deadline_at:
                            return self.store.mark_callback_expired(
                                request_id,
                                "The agent completed, but its callback token expired before submit_answer delivered a result.",
                            )
                        return self.store.mark_callback_missing(
                            request_id,
                            "The agent completed without delivering submit_answer within the callback grace period.",
                        )
                    return current

        current = self.store.get_request_for_caller(request_id, caller_id)
        if current.status in self._TERMINAL_STATUSES:
            return current
        if now >= current.callback_deadline_at:
            return self.store.mark_callback_expired(
                request_id,
                "The Workspace Agent did not deliver submit_answer before the callback deadline.",
            )
        return current

    def _record_run_status(
        self,
        record: RequestRecord,
        run_status: RunStatus,
    ) -> RequestRecord:
        status = run_status.status or "unknown"
        if status == "failed":
            return self.store.mark_agent_status(
                record.request_id,
                status,
                error_code=run_status.error_code or "agent_run_failed",
                error_message=run_status.error_message
                or "The Workspace Agent run failed.",
            )
        if status == "suspended":
            return self.store.mark_agent_status(
                record.request_id,
                status,
                error_code="agent_suspended",
                error_message=(
                    "The Workspace Agent run is suspended and may require an "
                    "external action or approval."
                ),
            )
        return self.store.mark_agent_status(record.request_id, status)
