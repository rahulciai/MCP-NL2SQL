"""Durable caller-bound sessions and Workspace Agent request state."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ACTIVE_REQUEST_STATUSES = {
    "created",
    "triggering",
    "queued",
    "accepted",
    "suspended",
    "trigger_unknown",
    "timed_out",
}


class SessionStoreError(Exception):
    """Base error for durable chat state."""


class SessionNotFound(SessionStoreError):
    pass


class OwnershipError(SessionStoreError):
    pass


class RequestNotFound(SessionStoreError):
    pass


class ActiveRequestError(SessionStoreError):
    def __init__(self, request_id: str) -> None:
        super().__init__("Another request for this session is still active.")
        self.request_id = request_id


class DeliveryTokenError(SessionStoreError):
    pass


class RequestStateError(SessionStoreError):
    pass


class ResultValidationError(SessionStoreError):
    pass


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    caller_id: str
    conversation_key: str | None
    model_response_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class RequestRecord:
    request_id: str
    caller_id: str
    session_id: str
    conversation_key: str
    message: str
    status: str
    callback_status: str
    delivery_token_expires_at: float
    callback_deadline_at: float
    idempotency_key: str
    trigger_run_id: str | None
    conversation_url: str | None
    agent_status: str | None
    agent_status_updated_at: float | None
    answer: str | None
    data: list[dict[str, Any]]
    sql: str | None
    error_code: str | None
    error_message: str | None
    created_at: str
    updated_at: str
    completed_at: str | None


@dataclass(frozen=True)
class PendingRequest:
    record: RequestRecord
    delivery_token: str


@dataclass(frozen=True)
class SubmissionResult:
    request_id: str
    session_id: str
    duplicate: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_delivery_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _fingerprint(answer: str, data_json: str, sql: str | None) -> str:
    return hashlib.sha256(
        (answer + "\x00" + data_json + "\x00" + (sql or "")).encode("utf-8")
    ).hexdigest()


class SessionStore:
    """SQLite-backed session and request/result state.

    Each operation opens its own connection so the same SQLite file can be
    shared by the FastAPI process and the FastMCP callback process/container.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        max_answer_chars: int = 20_000,
        max_sql_chars: int = 100_000,
        max_data_bytes: int = 1_000_000,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_answer_chars = max_answer_chars
        self.max_sql_chars = max_sql_chars
        self.max_data_bytes = max_data_bytes
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    caller_id TEXT NOT NULL,
                    conversation_key TEXT NOT NULL UNIQUE,
                    model_response_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            session_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(sessions)")
            }
            if "conversation_key" not in session_columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN conversation_key TEXT"
                )
            if "model_response_id" not in session_columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN model_response_id TEXT"
                )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    caller_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    conversation_key TEXT NOT NULL,
                    message TEXT NOT NULL,
                    delivery_token_hash TEXT NOT NULL,
                    delivery_token_expires_at REAL NOT NULL,
                    callback_deadline_at REAL NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    callback_status TEXT NOT NULL DEFAULT 'pending',
                    trigger_run_id TEXT,
                    conversation_url TEXT,
                    agent_status TEXT,
                    agent_status_updated_at REAL,
                    answer TEXT,
                    data_json TEXT NOT NULL DEFAULT '[]',
                    sql_text TEXT,
                    result_fingerprint TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                )
                """
            )

            # Safe, additive migration for the previous callback schema.
            request_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(requests)")
            }
            migrations = {
                "callback_deadline_at": "REAL",
                "callback_status": "TEXT NOT NULL DEFAULT 'pending'",
                "conversation_url": "TEXT",
                "agent_status_updated_at": "REAL",
                "sql_text": "TEXT",
            }
            for name, definition in migrations.items():
                if name not in request_columns:
                    connection.execute(
                        f"ALTER TABLE requests ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                """
                UPDATE requests
                SET callback_deadline_at = delivery_token_expires_at
                WHERE callback_deadline_at IS NULL
                """
            )
            connection.execute(
                """
                UPDATE requests
                SET callback_status = CASE
                    WHEN status = 'completed' THEN 'completed'
                    ELSE COALESCE(callback_status, 'pending')
                END
                WHERE callback_status IS NULL OR callback_status = ''
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS requests_session_idx "
                "ON requests(session_id, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS requests_caller_idx "
                "ON requests(caller_id, created_at DESC)"
            )

    @staticmethod
    def new_session_id() -> str:
        return f"sess_{uuid.uuid4().hex}"

    @staticmethod
    def new_conversation_key() -> str:
        return f"mmm_{uuid.uuid4().hex}"

    @staticmethod
    def new_request_id() -> str:
        return f"req_{uuid.uuid4().hex}"

    def create_session(self, caller_id: str) -> SessionRecord:
        session_id = self.new_session_id()
        conversation_key = self.new_conversation_key()
        timestamp = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions
                    (session_id, caller_id, conversation_key, model_response_id,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, caller_id, conversation_key, None, timestamp, timestamp),
            )
        return SessionRecord(
            session_id=session_id,
            caller_id=caller_id,
            conversation_key=conversation_key,
            model_response_id=None,
            created_at=timestamp,
            updated_at=timestamp,
        )

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> SessionRecord:
        keys = set(row.keys())
        return SessionRecord(
            session_id=row["session_id"],
            caller_id=row["caller_id"],
            conversation_key=(
                row["conversation_key"] if "conversation_key" in keys else None
            ),
            model_response_id=(
                row["model_response_id"] if "model_response_id" in keys else None
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_session(self, session_id: str) -> SessionRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise SessionNotFound("Session was not found.")
        return self._session_from_row(row)

    def get_session_for_caller(self, session_id: str, caller_id: str) -> SessionRecord:
        session = self.get_session(session_id)
        if session.caller_id != caller_id:
            raise OwnershipError("Session belongs to another caller.")
        return session

    def list_sessions_for_caller(self, caller_id: str) -> list[SessionRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM sessions
                WHERE caller_id = ?
                ORDER BY updated_at DESC
                """,
                (caller_id,),
            ).fetchall()
        return [self._session_from_row(row) for row in rows]

    @staticmethod
    def _request_from_row(row: sqlite3.Row) -> RequestRecord:
        try:
            parsed_data = json.loads(row["data_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            parsed_data = []
        if not isinstance(parsed_data, list):
            parsed_data = []
        keys = set(row.keys())
        callback_status = (
            row["callback_status"]
            if "callback_status" in keys and row["callback_status"]
            else ("completed" if row["status"] == "completed" else "pending")
        )
        callback_deadline = row["callback_deadline_at"]
        if callback_deadline is None:
            callback_deadline = row["delivery_token_expires_at"]
        return RequestRecord(
            request_id=row["request_id"],
            caller_id=row["caller_id"],
            session_id=row["session_id"],
            conversation_key=row["conversation_key"],
            message=row["message"],
            status=row["status"],
            callback_status=callback_status,
            delivery_token_expires_at=float(row["delivery_token_expires_at"]),
            callback_deadline_at=float(callback_deadline),
            idempotency_key=row["idempotency_key"],
            trigger_run_id=row["trigger_run_id"],
            conversation_url=(
                row["conversation_url"] if "conversation_url" in keys else None
            ),
            agent_status=row["agent_status"],
            agent_status_updated_at=(
                float(row["agent_status_updated_at"])
                if "agent_status_updated_at" in keys
                and row["agent_status_updated_at"] is not None
                else None
            ),
            answer=row["answer"],
            data=parsed_data,
            sql=(row["sql_text"] if "sql_text" in keys else None),
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    def get_request(self, request_id: str) -> RequestRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            raise RequestNotFound("Request was not found.")
        return self._request_from_row(row)

    def get_request_for_caller(
        self, request_id: str, caller_id: str
    ) -> RequestRecord:
        request = self.get_request(request_id)
        if request.caller_id != caller_id:
            # Do not reveal another caller's request IDs.
            raise RequestNotFound("Request was not found.")
        return request

    def list_requests_for_session(
        self,
        *,
        session_id: str,
        caller_id: str,
        limit: int,
        offset: int,
    ) -> tuple[list[RequestRecord], bool]:
        self.get_session_for_caller(session_id, caller_id)
        bounded_limit = max(1, min(limit, 100))
        bounded_offset = max(0, offset)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM requests
                WHERE session_id = ? AND caller_id = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (session_id, caller_id, bounded_limit + 1, bounded_offset),
            ).fetchall()
        has_more = len(rows) > bounded_limit
        return [self._request_from_row(row) for row in rows[:bounded_limit]], has_more

    def create_request(
        self,
        *,
        caller_id: str,
        session_id: str,
        conversation_key: str,
        message: str,
        callback_token_ttl_seconds: float | None = None,
        callback_deadline_seconds: float | None = None,
        # Compatibility with the previous implementation.
        delivery_token_ttl_seconds: float | None = None,
    ) -> PendingRequest:
        ttl_seconds = callback_token_ttl_seconds or delivery_token_ttl_seconds
        if ttl_seconds is None:
            raise ValueError("A callback token TTL is required.")
        deadline_seconds = callback_deadline_seconds or ttl_seconds
        request_id = self.new_request_id()
        idempotency_key = f"mmm-chat-{request_id}"
        delivery_token = secrets.token_urlsafe(32)
        timestamp = _utc_now()
        now = time.time()
        expires_at = now + ttl_seconds
        callback_deadline_at = min(now + deadline_seconds, expires_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                placeholders = ", ".join("?" for _ in ACTIVE_REQUEST_STATUSES)
                active = connection.execute(
                    f"""
                    SELECT request_id FROM requests
                    WHERE session_id = ? AND status IN ({placeholders})
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (session_id, *sorted(ACTIVE_REQUEST_STATUSES)),
                ).fetchone()
                if active is not None:
                    raise ActiveRequestError(active["request_id"])

                connection.execute(
                    """
                    INSERT INTO requests
                        (request_id, caller_id, session_id, conversation_key, message,
                         delivery_token_hash, delivery_token_expires_at,
                         callback_deadline_at, idempotency_key, status,
                         callback_status, data_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        caller_id,
                        session_id,
                        conversation_key,
                        message,
                        _hash_delivery_token(delivery_token),
                        expires_at,
                        callback_deadline_at,
                        idempotency_key,
                        "created",
                        "pending",
                        "[]",
                        timestamp,
                        timestamp,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return PendingRequest(
            record=self.get_request(request_id),
            delivery_token=delivery_token,
        )

    def mark_triggering(self, request_id: str) -> RequestRecord:
        timestamp = _utc_now()
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE requests
                SET status = CASE WHEN callback_status = 'completed'
                                  THEN 'completed' ELSE 'queued' END,
                    agent_status = 'triggering', agent_status_updated_at = ?,
                    updated_at = ?
                WHERE request_id = ?
                """,
                (now, timestamp, request_id),
            )
            if cursor.rowcount != 1:
                raise RequestNotFound("Request was not found.")
        return self.get_request(request_id)

    def mark_trigger_accepted(
        self,
        request_id: str,
        run_id: str | None,
        agent_status: str | None,
        conversation_url: str | None = None,
    ) -> RequestRecord:
        timestamp = _utc_now()
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE requests
                SET status = CASE WHEN callback_status = 'completed'
                                  THEN 'completed' ELSE 'queued' END,
                    trigger_run_id = COALESCE(?, trigger_run_id),
                    conversation_url = COALESCE(?, conversation_url),
                    agent_status = COALESCE(?, agent_status),
                    agent_status_updated_at = ?,
                    error_code = CASE WHEN callback_status = 'completed'
                                      THEN error_code ELSE NULL END,
                    error_message = CASE WHEN callback_status = 'completed'
                                         THEN error_message ELSE NULL END,
                    updated_at = ?
                WHERE request_id = ?
                """,
                (run_id, conversation_url, agent_status, now, timestamp, request_id),
            )
            if cursor.rowcount != 1:
                raise RequestNotFound("Request was not found.")
        return self.get_request(request_id)

    def mark_trigger_unknown(
        self, request_id: str, error_code: str, error_message: str
    ) -> RequestRecord:
        timestamp = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE requests
                SET status = CASE WHEN callback_status = 'completed'
                                  THEN 'completed' ELSE 'trigger_unknown' END,
                    error_code = CASE WHEN callback_status = 'completed'
                                      THEN error_code ELSE ? END,
                    error_message = CASE WHEN callback_status = 'completed'
                                         THEN error_message ELSE ? END,
                    updated_at = ?
                WHERE request_id = ?
                """,
                (error_code, error_message, timestamp, request_id),
            )
            if cursor.rowcount != 1:
                raise RequestNotFound("Request was not found.")
        return self.get_request(request_id)

    def mark_agent_status(
        self,
        request_id: str,
        agent_status: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> RequestRecord:
        normalized = (agent_status or "unknown").lower()
        if normalized == "failed":
            request_status = "failed"
        elif normalized == "suspended":
            request_status = "suspended"
        else:
            request_status = "queued"
        timestamp = _utc_now()
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE requests
                SET status = CASE WHEN callback_status = 'completed'
                                  THEN 'completed' ELSE ? END,
                    agent_status = ?,
                    agent_status_updated_at = CASE WHEN agent_status = ?
                                                   THEN agent_status_updated_at
                                                   ELSE ? END,
                    error_code = CASE WHEN callback_status = 'completed'
                                      THEN error_code ELSE ? END,
                    error_message = CASE WHEN callback_status = 'completed'
                                         THEN error_message ELSE ? END,
                    updated_at = ?
                WHERE request_id = ?
                """,
                (
                    request_status,
                    normalized,
                    normalized,
                    now,
                    error_code,
                    error_message,
                    timestamp,
                    request_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RequestNotFound("Request was not found.")
        return self.get_request(request_id)

    def mark_failed(
        self, request_id: str, error_code: str, error_message: str
    ) -> RequestRecord:
        timestamp = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE requests
                SET status = CASE WHEN callback_status = 'completed'
                                  THEN 'completed' ELSE 'failed' END,
                    error_code = CASE WHEN callback_status = 'completed'
                                      THEN error_code ELSE ? END,
                    error_message = CASE WHEN callback_status = 'completed'
                                         THEN error_message ELSE ? END,
                    updated_at = ?
                WHERE request_id = ?
                """,
                (error_code, error_message, timestamp, request_id),
            )
            if cursor.rowcount != 1:
                raise RequestNotFound("Request was not found.")
        return self.get_request(request_id)

    def mark_suspended(self, request_id: str, message: str) -> RequestRecord:
        return self.mark_agent_status(
            request_id,
            "suspended",
            error_code="agent_suspended",
            error_message=message,
        )

    def mark_callback_missing(self, request_id: str, message: str) -> RequestRecord:
        timestamp = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE requests
                SET status = CASE
                        WHEN callback_status = 'completed' THEN 'completed'
                        WHEN status IN ('failed', 'suspended') THEN status
                        ELSE 'callback_missing'
                    END,
                    callback_status = CASE
                        WHEN callback_status = 'completed' THEN callback_status
                        WHEN status IN ('failed', 'suspended') THEN callback_status
                        ELSE 'missing'
                    END,
                    error_code = CASE
                        WHEN callback_status = 'completed' OR status IN ('failed', 'suspended')
                            THEN error_code
                        ELSE 'callback_missing'
                    END,
                    error_message = CASE
                        WHEN callback_status = 'completed' OR status IN ('failed', 'suspended')
                            THEN error_message
                        ELSE ?
                    END,
                    updated_at = ?
                WHERE request_id = ?
                """,
                (message, timestamp, request_id),
            )
            if cursor.rowcount != 1:
                raise RequestNotFound("Request was not found.")
        return self.get_request(request_id)

    def mark_callback_expired(self, request_id: str, message: str) -> RequestRecord:
        timestamp = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE requests
                SET status = CASE WHEN callback_status = 'completed'
                                  THEN 'completed' ELSE 'callback_expired' END,
                    callback_status = CASE WHEN callback_status = 'completed'
                                           THEN callback_status ELSE 'expired' END,
                    error_code = CASE WHEN callback_status = 'completed'
                                      THEN error_code ELSE 'callback_expired' END,
                    error_message = CASE WHEN callback_status = 'completed'
                                         THEN error_message ELSE ? END,
                    updated_at = ?
                WHERE request_id = ?
                """,
                (message, timestamp, request_id),
            )
            if cursor.rowcount != 1:
                raise RequestNotFound("Request was not found.")
        return self.get_request(request_id)

    def mark_timed_out(self, request_id: str) -> RequestRecord:
        """Compatibility alias for callers of the previous implementation."""
        return self.mark_callback_expired(
            request_id,
            "The Workspace Agent did not deliver a callback before the deadline.",
        )

    def submit_answer(
        self,
        *,
        request_id: str,
        delivery_token: str,
        answer: str,
        data: list[dict[str, Any]],
        sql: str | None = None,
    ) -> SubmissionResult:
        if not isinstance(answer, str) or not answer.strip():
            raise ResultValidationError("answer must be a non-empty string.")
        if sql is not None and not isinstance(sql, str):
            raise ResultValidationError("sql must be a string when supplied.")
        normalized_sql = sql.strip() if isinstance(sql, str) else None
        if normalized_sql and len(normalized_sql) > self.max_sql_chars:
            raise ResultValidationError("sql exceeds the configured size limit.")
        if len(answer) > self.max_answer_chars:
            raise ResultValidationError("answer exceeds the configured size limit.")
        if not isinstance(data, list) or any(
            not isinstance(item, dict) for item in data
        ):
            raise ResultValidationError("data must be a list of table objects.")
        try:
            data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ResultValidationError("data must contain JSON-compatible values.") from exc
        if len(data_json.encode("utf-8")) > self.max_data_bytes:
            raise ResultValidationError("data exceeds the configured size limit.")

        submitted_fingerprint = _fingerprint(answer, data_json, normalized_sql)
        timestamp = _utc_now()
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM requests WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if row is None:
                    raise RequestNotFound("Request was not found.")
                if not hmac.compare_digest(
                    row["delivery_token_hash"],
                    _hash_delivery_token(delivery_token),
                ):
                    raise DeliveryTokenError("Delivery token is invalid.")
                if now > float(row["delivery_token_expires_at"]):
                    raise DeliveryTokenError("Delivery token has expired.")

                if row["status"] == "completed" or row["callback_status"] == "completed":
                    if row["result_fingerprint"] == submitted_fingerprint:
                        connection.commit()
                        return SubmissionResult(
                            request_id=request_id,
                            session_id=row["session_id"],
                            duplicate=True,
                        )
                    raise RequestStateError("A different result was already submitted.")
                if row["status"] in {"failed", "callback_missing", "callback_expired"}:
                    raise RequestStateError("This request is no longer accepting results.")

                connection.execute(
                    """
                    UPDATE requests
                    SET status = 'completed', callback_status = 'completed',
                        answer = ?, data_json = ?, sql_text = ?,
                        result_fingerprint = ?, error_code = NULL,
                        error_message = NULL, updated_at = ?, completed_at = ?
                    WHERE request_id = ?
                    """,
                    (
                        answer,
                        data_json,
                        normalized_sql,
                        submitted_fingerprint,
                        timestamp,
                        timestamp,
                        request_id,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return SubmissionResult(
            request_id=request_id,
            session_id=row["session_id"],
            duplicate=False,
        )
