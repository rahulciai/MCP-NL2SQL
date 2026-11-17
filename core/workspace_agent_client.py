"""Client for the asynchronous ChatGPT Workspace Agent trigger API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from core.settings import AppSettings


class WorkspaceAgentError(Exception):
    """Safe classification of a Workspace Agent API failure."""

    def __init__(self, message: str, *, code: str, http_status: int) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class TriggerResult:
    run_id: str | None
    status: str | None
    conversation_url: str | None = None


@dataclass(frozen=True)
class RunStatus:
    status: str
    error_code: str | None = None
    error_message: str | None = None


class WorkspaceAgentClient:
    def __init__(
        self,
        settings: AppSettings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._client = client

    @property
    def _owns_client(self) -> bool:
        return self._client is None

    def _headers(self, *, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.settings.agent_access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # The beta header enables agent_trigger_run_id in the response.
            "OpenAI-Beta": "workspace_agent_runs=v1",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _run_url(self, run_id: str) -> str:
        trigger_url = self.settings.workspace_agent_trigger_url.rstrip("/")
        if not trigger_url.endswith("/trigger"):
            raise WorkspaceAgentError(
                "Workspace Agent trigger URL is invalid.",
                code="agent_not_configured",
                http_status=503,
            )
        return f"{trigger_url[:-len('/trigger')]}/runs/{run_id}"

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        client = self._client
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(
                timeout=self.settings.agent_http_timeout_seconds
            )
        try:
            return await client.request(
                method,
                url,
                json=json_body,
                headers=self._headers(idempotency_key=idempotency_key),
            )
        except httpx.TimeoutException as exc:
            raise WorkspaceAgentError(
                "The Workspace Agent request timed out.",
                code="agent_http_timeout",
                http_status=504,
            ) from exc
        except httpx.HTTPError as exc:
            raise WorkspaceAgentError(
                "The backend could not connect to the Workspace Agent API.",
                code="agent_connection_failed",
                http_status=502,
            ) from exc
        finally:
            if owns_client:
                await client.aclose()

    @staticmethod
    def _error_for_response(response: httpx.Response) -> WorkspaceAgentError:
        status = response.status_code
        if status == 401:
            return WorkspaceAgentError(
                "Workspace Agent authentication failed.",
                code="agent_authentication_failed",
                http_status=502,
            )
        if status == 403:
            return WorkspaceAgentError(
                "The Workspace Agent token cannot run this agent.",
                code="agent_permission_denied",
                http_status=502,
            )
        if status == 404:
            return WorkspaceAgentError(
                "The Workspace Agent trigger was not found or is not visible to the token owner.",
                code="agent_trigger_not_found",
                http_status=502,
            )
        if status == 409:
            return WorkspaceAgentError(
                "The Workspace Agent or API channel is not runnable.",
                code="agent_not_runnable",
                http_status=409,
            )
        if status == 429:
            return WorkspaceAgentError(
                "The Workspace Agent API rate limit was reached.",
                code="agent_rate_limited",
                http_status=429,
            )
        return WorkspaceAgentError(
            "The Workspace Agent API rejected the request.",
            code="agent_request_failed",
            http_status=502,
        )

    @staticmethod
    def _json_body(response: httpx.Response) -> dict[str, Any]:
        try:
            parsed = response.json()
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    async def trigger(
        self,
        *,
        conversation_key: str,
        input_text: str,
        idempotency_key: str,
    ) -> TriggerResult:
        if not self.settings.workspace_agent_configured:
            raise WorkspaceAgentError(
                "Workspace Agent trigger credentials are not configured.",
                code="agent_not_configured",
                http_status=503,
            )
        response = await self._request(
            "POST",
            self.settings.workspace_agent_trigger_url,
            json_body={"conversation_key": conversation_key, "input": input_text},
            idempotency_key=idempotency_key,
        )
        if response.status_code not in {200, 202}:
            raise self._error_for_response(response)
        body = self._json_body(response)
        run_id = body.get("agent_trigger_run_id")
        conversation_url = body.get("conversation_url")
        return TriggerResult(
            run_id=str(run_id) if run_id else None,
            status=str(body.get("status") or "queued").lower(),
            conversation_url=(
                str(conversation_url) if conversation_url else None
            ),
        )

    async def get_run_status(self, run_id: str) -> RunStatus:
        if not self.settings.workspace_agent_configured:
            raise WorkspaceAgentError(
                "Workspace Agent trigger credentials are not configured.",
                code="agent_not_configured",
                http_status=503,
            )
        response = await self._request(
            "GET",
            self._run_url(run_id),
        )
        if response.status_code != 200:
            raise self._error_for_response(response)
        body = self._json_body(response)
        error = body.get("error")
        error_code = error.get("code") if isinstance(error, dict) else None
        error_message = None
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            error_message = error["message"][:500]
        return RunStatus(
            status=str(body.get("status") or "unknown").lower(),
            error_code=str(error_code) if error_code else None,
            error_message=error_message,
        )
