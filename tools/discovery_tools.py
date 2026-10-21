"""MMM catalog and live PostgreSQL schema discovery tools."""

from __future__ import annotations

import json
import re
from typing import Any

from core.connection_engine import fetch_all


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_table_name(table_name: str) -> str:
    normalized = table_name.strip()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError("Table name must be a simple PostgreSQL identifier.")
    return normalized


def register_discovery_tools(mcp: Any) -> None:
    @mcp.tool()
    def list_database_tables() -> str:
        """List tables currently available in the public PostgreSQL schema."""
        rows = fetch_all(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_type = 'BASE TABLE'
            ORDER BY table_name;
            """
        )

        if not rows:
            return "No public tables were found in the connected database."

        return "Public tables:\n" + "\n".join(f"- {row['table_name']}" for row in rows)

    @mcp.tool()
    def inspect_table_schema(table_name: str) -> str:
        """Inspect columns, data types, nullability, defaults, and keys."""
        safe_table_name = _validate_table_name(table_name)
        rows = fetch_all(
            """
            SELECT
                c.ordinal_position,
                c.column_name,
                c.data_type,
                c.is_nullable,
                c.column_default,
                CASE WHEN tc.constraint_type = 'PRIMARY KEY' THEN 'YES' ELSE 'NO' END
                    AS is_primary_key
            FROM information_schema.columns c
            LEFT JOIN information_schema.key_column_usage kcu
                ON c.table_schema = kcu.table_schema
               AND c.table_name = kcu.table_name
               AND c.column_name = kcu.column_name
            LEFT JOIN information_schema.table_constraints tc
                ON kcu.constraint_schema = tc.constraint_schema
               AND kcu.constraint_name = tc.constraint_name
               AND tc.constraint_type = 'PRIMARY KEY'
            WHERE c.table_schema = 'public'
              AND c.table_name = %s
            ORDER BY c.ordinal_position;
            """,
            (safe_table_name,),
        )

        if not rows:
            return f"No table named '{safe_table_name}' was found in the public schema."

        payload = {
            "table": safe_table_name,
            "columns": [dict(row) for row in rows],
        }
        return json.dumps(payload, indent=2, default=str)
