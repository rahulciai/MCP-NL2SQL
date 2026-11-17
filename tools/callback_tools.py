"""FastMCP callback tool for Workspace Agent results."""

from __future__ import annotations

import json
import logging
from typing import Any

from core.session_store import (
    DeliveryTokenError,
    RequestNotFound,
    RequestStateError,
    ResultValidationError,
    SessionStore,
)


logger = logging.getLogger(__name__)

_callback_store: SessionStore | None = None


def configure_callback_store(store: SessionStore) -> None:
    """Bind callbacks to the same durable store used by the FastAPI app."""
    global _callback_store
    _callback_store = store
    logger.info("workspace_callback_store_configured db_path=%s", store.db_path)


def register_callback_tools(mcp: Any) -> None:
    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "openWorldHint": False,
        }
    )
    def submit_answer(
        request_id: str,
        delivery_token: str,
        answer: str,
        data: list[dict[str, Any]],
        sql: str | None = None,
    ) -> str:
        """Persist the final Workspace Agent answer and actual query tables.

        The request ID and delivery token must come from the current backend
        trigger input. This tool is intentionally not read-only: it writes the
        completed result to the backend's durable request store.
        """
        if _callback_store is None:
            raise RuntimeError("The callback store is not configured.")
        try:
            result = _callback_store.submit_answer(
                request_id=request_id,
                delivery_token=delivery_token,
                answer=answer,
                data=data,
                sql=sql,
            )
        except RequestNotFound:
            logger.warning(
                "workspace_callback request_id=%s outcome=request_not_found",
                request_id,
            )
            raise
        except DeliveryTokenError as exc:
            outcome = (
                "delivery_token_expired"
                if str(exc) == "Delivery token has expired."
                else "delivery_token_invalid"
            )
            logger.warning(
                "workspace_callback request_id=%s outcome=%s",
                request_id,
                outcome,
            )
            raise
        except RequestStateError:
            logger.warning(
                "workspace_callback request_id=%s outcome=request_state_rejected",
                request_id,
            )
            raise
        except ResultValidationError:
            logger.warning(
                "workspace_callback request_id=%s outcome=payload_rejected",
                request_id,
            )
            raise
        else:
            logger.info(
                "workspace_callback request_id=%s outcome=%s",
                request_id,
                "duplicate" if result.duplicate else "accepted",
            )
        return json.dumps(
            {
                "accepted": True,
                "duplicate": result.duplicate,
                "request_id": result.request_id,
            }
        )
