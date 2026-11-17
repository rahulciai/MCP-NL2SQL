"""Shared database operations used by MCP tools and the Workspace Agent callback backend."""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from core.connection_engine import fetch_all
from core.settings import load_environment_file


MAX_ROWS = 150
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_READ_ONLY_START_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE | re.DOTALL)
_FORBIDDEN_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|merge|call|"
    r"execute|copy|vacuum|analyze|refresh|reindex|lock|set|reset|into)\b",
    re.IGNORECASE,
)


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _database_schema() -> str:
    load_environment_file()
    schema = os.getenv("DATABASE_SCHEMA", "mmm").strip()
    if not _IDENTIFIER_RE.fullmatch(schema):
        raise ValueError("DATABASE_SCHEMA must be a simple PostgreSQL identifier.")
    return schema


def _validate_table_name(table_name: str) -> str:
    normalized = table_name.strip()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError("Table name must be a simple PostgreSQL identifier.")
    return normalized


def list_database_tables_rows() -> list[dict[str, Any]]:
    schema = _database_schema()
    return fetch_all(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = %s
          AND table_type = 'BASE TABLE'
        ORDER BY table_name;
        """,
        (schema,),
    )


def format_database_tables(rows: list[dict[str, Any]]) -> str:
    schema = _database_schema()
    if not rows:
        return f"No tables were found in the connected database schema '{schema}'."
    return f"Tables in schema '{schema}':\n" + "\n".join(
        f"- {row['table_name']}" for row in rows
    )


def list_database_tables_text() -> str:
    return format_database_tables(list_database_tables_rows())


def inspect_table_schema_rows(table_name: str) -> tuple[str, list[dict[str, Any]]]:
    schema = _database_schema()
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
        WHERE c.table_schema = %s
          AND c.table_name = %s
        ORDER BY c.ordinal_position;
        """,
        (schema, safe_table_name),
    )
    return safe_table_name, rows


def inspect_table_schema_text(table_name: str) -> str:
    schema = _database_schema()
    safe_table_name, rows = inspect_table_schema_rows(table_name)
    if not rows:
        return f"No table named '{safe_table_name}' was found in schema '{schema}'."

    payload = {
        "table": f"{schema}.{safe_table_name}",
        "columns": [dict(row) for row in rows],
    }
    return json.dumps(payload, indent=2, default=str)


def _mask_literals_and_comments(sql: str) -> str:
    """Replace literals/comments so validation checks SQL tokens, not values."""
    output: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < length else ""

        if char == "-" and next_char == "-":
            output.extend("  ")
            index += 2
            while index < length and sql[index] not in "\r\n":
                output.append(" ")
                index += 1
            continue

        if char == "/" and next_char == "*":
            output.extend("  ")
            index += 2
            while index + 1 < length and not (sql[index] == "*" and sql[index + 1] == "/"):
                output.append(" ")
                index += 1
            if index + 1 >= length:
                raise ValueError("Unterminated SQL comment is not allowed.")
            output.extend("  ")
            index += 2
            continue

        if char in "'\"" + chr(96):
            quote = char
            output.append(" ")
            index += 1
            while index < length:
                if sql[index] == quote:
                    if index + 1 < length and sql[index + 1] == quote:
                        output.extend("  ")
                        index += 2
                        continue
                    output.append(" ")
                    index += 1
                    break
                output.append(" ")
                index += 1
            else:
                raise ValueError("Unterminated SQL quoted value or identifier is not allowed.")
            continue

        output.append(char)
        index += 1

    return "".join(output)


def _validate_read_only_sql(sql: str) -> str:
    cleaned = sql.strip()
    if not cleaned:
        raise ValueError("SQL query cannot be empty.")
    if not _READ_ONLY_START_RE.match(cleaned):
        raise ValueError("Only SELECT or WITH read-only queries are allowed.")

    masked = _mask_literals_and_comments(cleaned)
    without_trailing_semicolon = masked[:-1].strip() if masked.endswith(";") else masked
    if ";" in without_trailing_semicolon:
        raise ValueError("Only one SQL statement is allowed.")
    if _FORBIDDEN_RE.search(masked):
        raise ValueError("Query contains a keyword that is not allowed in read-only mode.")

    return cleaned[:-1].strip() if cleaned.endswith(";") else cleaned


def validate_sql_query_result(sql: str) -> dict[str, Any]:
    try:
        safe_sql = _validate_read_only_sql(sql)
        fetch_all(f"EXPLAIN (FORMAT JSON) {safe_sql}")
    except Exception as exc:
        return {"validated": False, "executed": False, "sql": sql, "error": str(exc)}

    return {
        "validated": True,
        "executed": False,
        "sql": safe_sql,
        "message": (
            "PostgreSQL planned the query only. No result rows were returned; "
            "this is not execution verification."
        ),
    }


def execute_read_only_query_result(sql: str) -> dict[str, Any]:
    safe_sql = _validate_read_only_sql(sql)
    limited_sql = f"SELECT * FROM ({safe_sql}) AS mcp_read_only_query LIMIT %s"
    rows = fetch_all(limited_sql, (MAX_ROWS,))
    return {
        "sql": safe_sql,
        "executed": True,
        "verified_by_postgresql_execution": True,
        "row_count": len(rows),
        "max_rows": MAX_ROWS,
        "rows": rows,
    }


def execute_read_only_query_json(sql: str) -> str:
    try:
        payload = execute_read_only_query_result(sql)
    except Exception as exc:
        return f"Query error: {exc}"
    return json.dumps(payload, default=_json_default, indent=2)

