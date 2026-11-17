"""Minimal bearer authentication for the application chat API."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from fastapi import Request

from core.settings import AppSettings


class AuthenticationError(Exception):
    pass


class AuthenticationConfigurationError(Exception):
    pass


@dataclass(frozen=True)
class AuthenticatedCaller:
    caller_id: str



def authenticate_authorization_header(
    authorization: str | None,
    settings: AppSettings,
) -> AuthenticatedCaller:
    if not settings.has_chat_auth:
        raise AuthenticationConfigurationError(
            "CHAT_API_TOKEN or CHAT_API_TOKENS is not configured on the server."
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthenticationError("Authorization must use a Bearer token.")

    supplied_token = authorization[7:].strip()
    if not supplied_token:
        raise AuthenticationError("Bearer token is empty.")

    for caller_id, expected_token in settings.chat_api_tokens:
        if hmac.compare_digest(supplied_token, expected_token):
            return AuthenticatedCaller(caller_id=caller_id)

    if settings.chat_api_token and hmac.compare_digest(
        supplied_token,
        settings.chat_api_token,
    ):
        return AuthenticatedCaller(caller_id="default")

    # Do not reveal whether a caller ID or a token was the part that failed.
    digest = hashlib.sha256(supplied_token.encode("utf-8")).hexdigest()
    if settings.chat_api_token or settings.chat_api_tokens:
        _ = digest

    raise AuthenticationError("Bearer token is invalid.")
def require_bearer(request: Request) -> AuthenticatedCaller:
    """FastAPI dependency that keeps authentication errors structured."""
    from fastapi import HTTPException

    settings: AppSettings = request.app.state.settings
    try:
        return authenticate_authorization_header(
            request.headers.get("authorization"),
            settings,
        )
    except AuthenticationConfigurationError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "authentication_not_configured", "message": str(exc)},
        ) from exc
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
            detail={"code": "authentication_failed", "message": str(exc)},
        ) from exc

