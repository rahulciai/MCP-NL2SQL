"""MMM catalog and live PostgreSQL schema discovery tools."""

from __future__ import annotations

import re
from typing import Any

from core.database_service import inspect_table_schema_text, list_database_tables_text


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_table_name(table_name: str) -> str:
    normalized = table_name.strip()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError("Table name must be a simple PostgreSQL identifier.")
    return normalized


def register_discovery_tools(mcp: Any) -> None:
    @mcp.tool()
    def list_database_tables() -> str:
        """List tables currently available in the configured PostgreSQL schema."""
        return list_database_tables_text()

    @mcp.tool()
    def inspect_table_schema(table_name: str) -> str:
        """Inspect columns, data types, nullability, defaults, and keys."""
        return inspect_table_schema_text(table_name)
