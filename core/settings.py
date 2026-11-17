"""Environment-backed settings for the Workspace Agent callback service."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / "configuration" / "environment.env"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_WORKSPACE_AGENT_TRIGGER_URL = (
    "https://api.chatgpt.com/v1/workspace_agents/"
    "agtch_6ad9defc5530819191176f091926911c/trigger"
)


def load_environment_file() -> None:
    """Load simple KEY=VALUE entries without overriding process variables."""
    if not ENV_FILE.exists():
        return

    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _resolve_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _parse_float(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number.") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return value


def _parse_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return value


def _parse_float_alias(
    name: str,
    alias: str,
    default: float,
) -> float:
    """Read a preferred setting while preserving the previous env name."""
    if os.getenv(name) is None and os.getenv(alias) is not None:
        os.environ[name] = os.environ[alias]
    return _parse_float(name, default)


def _parse_token_map() -> tuple[tuple[str, str], ...]:
    """Parse optional caller=token entries for multi-caller deployments."""
    raw_value = os.getenv("CHAT_API_TOKENS", "").strip()
    if not raw_value:
        return ()

    entries: list[tuple[str, str]] = []
    for entry in raw_value.split(","):
        if "=" not in entry:
            raise ValueError("CHAT_API_TOKENS entries must use caller_id=token format.")
        caller_id, token = entry.split("=", 1)
        caller_id = caller_id.strip()
        token = token.strip()
        if not caller_id or not token:
            raise ValueError("CHAT_API_TOKENS cannot contain empty caller IDs or tokens.")
        entries.append((caller_id, token))
    return tuple(entries)


@dataclass(frozen=True)
class AppSettings:
    agent_access_token: str
    workspace_agent_trigger_url: str
    chat_api_token: str
    chat_api_tokens: tuple[tuple[str, str], ...]
    database_schema: str
    session_db_path: Path
    request_timeout_seconds: float
    callback_token_ttl_seconds: float
    callback_grace_seconds: float
    agent_http_timeout_seconds: float
    agent_poll_interval_seconds: float
    agent_poll_timeout_seconds: float
    max_message_chars: int
    max_answer_chars: int
    max_sql_chars: int
    max_data_bytes: int
    mcp_host: str
    mcp_port: int

    @classmethod
    def from_env(cls) -> "AppSettings":
        load_environment_file()

        database_schema = os.getenv("DATABASE_SCHEMA", "mmm").strip()
        if not _IDENTIFIER_RE.fullmatch(database_schema):
            raise ValueError("DATABASE_SCHEMA must be a simple PostgreSQL identifier.")

        trigger_url = os.getenv("WORKSPACE_AGENT_TRIGGER_URL", "").strip()
        if not trigger_url:
            # Keep existing deployments working while they migrate to the
            # explicit full trigger URL setting.
            legacy_trigger_id = os.getenv("WORKSPACE_AGENT_TRIGGER_ID", "").strip()
            if legacy_trigger_id:
                legacy_base = os.getenv(
                    "AGENT_API_BASE_URL",
                    "https://api.chatgpt.com/v1",
                ).strip().rstrip("/")
                trigger_url = (
                    f"{legacy_base}/workspace_agents/{legacy_trigger_id}/trigger"
                )
            else:
                trigger_url = DEFAULT_WORKSPACE_AGENT_TRIGGER_URL
        parsed_url = urlparse(trigger_url)
        if (
            parsed_url.scheme != "https"
            or not parsed_url.netloc
            or not parsed_url.path.rstrip("/").endswith("/trigger")
        ):
            raise ValueError(
                "WORKSPACE_AGENT_TRIGGER_URL must be an absolute HTTPS /trigger URL."
            )

        return cls(
            agent_access_token=os.getenv("AGENT_ACCESS_TOKEN", "").strip(),
            workspace_agent_trigger_url=trigger_url.rstrip("/"),
            chat_api_token=os.getenv("CHAT_API_TOKEN", "").strip(),
            chat_api_tokens=_parse_token_map(),
            database_schema=database_schema,
            session_db_path=_resolve_path(
                os.getenv("SESSION_DB_PATH", "data/chat_sessions.sqlite3")
            ),
            request_timeout_seconds=_parse_float_alias(
                "REQUEST_TIMEOUT_SECONDS", "CHAT_TIMEOUT_SECONDS", 120.0
            ),
            callback_token_ttl_seconds=_parse_float_alias(
                "CALLBACK_TOKEN_TTL_SECONDS", "DELIVERY_TOKEN_TTL_SECONDS", 900.0
            ),
            callback_grace_seconds=_parse_float(
                "CALLBACK_GRACE_SECONDS", 30.0
            ),
            agent_http_timeout_seconds=_parse_float(
                "AGENT_HTTP_TIMEOUT_SECONDS", 20.0
            ),
            agent_poll_interval_seconds=_parse_float(
                "AGENT_POLL_INTERVAL_SECONDS", 1.0
            ),
            agent_poll_timeout_seconds=_parse_float(
                "AGENT_POLL_TIMEOUT_SECONDS", 1800.0
            ),
            max_message_chars=_parse_int("CHAT_MAX_MESSAGE_CHARS", 8_000),
            max_answer_chars=_parse_int("CHAT_MAX_ANSWER_CHARS", 20_000),
            max_sql_chars=_parse_int("CHAT_MAX_SQL_CHARS", 100_000),
            max_data_bytes=_parse_int("CHAT_MAX_DATA_BYTES", 1_000_000),
            mcp_host=os.getenv("MCP_HOST", "127.0.0.1").strip(),
            mcp_port=_parse_int("MCP_PORT", 8000),
        )

    @property
    def has_chat_auth(self) -> bool:
        return bool(self.chat_api_token or self.chat_api_tokens)

    @property
    def workspace_agent_configured(self) -> bool:
        return bool(self.agent_access_token and self.workspace_agent_trigger_url)

    @property
    def workspace_agent_trigger_id(self) -> str:
        """Return the ID from the full URL for diagnostics/backward compatibility."""
        parts = [part for part in self.workspace_agent_trigger_url.split("/") if part]
        return parts[-2] if len(parts) >= 2 and parts[-1] == "trigger" else ""

    @property
    def chat_timeout_seconds(self) -> float:
        """Backward-compatible name for the bounded request deadline."""
        return self.request_timeout_seconds

    @property
    def delivery_token_ttl_seconds(self) -> float:
        """Backward-compatible name for callback token TTL."""
        return self.callback_token_ttl_seconds
