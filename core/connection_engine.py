"""Safe PostgreSQL connection management for the MCP server."""

from __future__ import annotations

import os
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / "configuration" / "environment.env"
DEFAULT_MIN_CONNECTIONS = 1
DEFAULT_MAX_CONNECTIONS = 5
DEFAULT_STATEMENT_TIMEOUT_MS = 30_000

_pool: SimpleConnectionPool | None = None
_pool_lock = Lock()


def _load_environment_file() -> None:
    """Load simple KEY=VALUE entries without overriding Claude's env values."""
    if not ENV_FILE.exists():
        return

    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_database_url() -> str:
    _load_environment_file()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is missing. Set it in the Claude Desktop env block "
            "or in configuration/environment.env."
        )
    return database_url


def get_connection_pool() -> SimpleConnectionPool:
    global _pool
    if _pool is not None:
        return _pool

    with _pool_lock:
        if _pool is None:
            _pool = SimpleConnectionPool(
                DEFAULT_MIN_CONNECTIONS,
                DEFAULT_MAX_CONNECTIONS,
                dsn=get_database_url(),
                cursor_factory=RealDictCursor,
            )
    return _pool


def fetch_all(sql: str, params: Iterable[Any] | None = None) -> list[dict[str, Any]]:
    """Run a query in a read-only transaction and return dictionary rows."""
    pool = get_connection_pool()
    connection = pool.getconn()
    try:
        connection.set_session(readonly=True, autocommit=False)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(DEFAULT_STATEMENT_TIMEOUT_MS),),
            )
            cursor.execute(sql, tuple(params or ()))
            rows = [dict(row) for row in cursor.fetchall()]
        connection.rollback()
        return rows
    except Exception:
        connection.rollback()
        raise
    finally:
        pool.putconn(connection)


def close_connection_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None
