"""Read-only PostgreSQL query execution for the NL2SQL MCP server."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from core.connection_engine import fetch_all


MAX_ROWS = 150
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

        if char in "'\"`":
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

    # Return the original SQL so quoted identifiers and string values retain
    # their exact spelling when PostgreSQL executes the query.
    return cleaned[:-1].strip() if cleaned.endswith(";") else cleaned


def register_query_tools(mcp: Any) -> None:
    @mcp.tool()
    def validate_sql_query(sql: str) -> str:
        """Preflight a read-only query against PostgreSQL without returning data.

        PostgreSQL plans the query, so missing tables, columns, functions, and
        type errors are detected. This is not execution verification.
        """
        try:
            safe_sql = _validate_read_only_sql(sql)
            fetch_all(f"EXPLAIN (FORMAT JSON) {safe_sql}")
        except Exception as exc:
            return json.dumps(
                {"validated": False, "executed": False, "sql": sql, "error": str(exc)},
                indent=2,
            )

        return json.dumps(
            {
                "validated": True,
                "executed": False,
                "sql": safe_sql,
                "message": "PostgreSQL planned the query only. No result rows were returned; this is not execution verification.",
            },
            indent=2,
        )

    @mcp.tool()
    def execute_read_only_query(sql: str) -> str:
        """Execute one validated PostgreSQL SELECT/WITH query, returning up to 150 rows as JSON."""
        try:
            safe_sql = _validate_read_only_sql(sql)
            limited_sql = f"SELECT * FROM ({safe_sql}) AS mcp_read_only_query LIMIT %s"
            rows = fetch_all(limited_sql, (MAX_ROWS,))
        except Exception as exc:
            return f"Query error: {exc}"

        payload = {
            "sql": safe_sql,
            "executed": True,
            "verified_by_postgresql_execution": True,
            "row_count": len(rows),
            "max_rows": MAX_ROWS,
            "rows": rows,
        }
        return json.dumps(payload, default=_json_default, indent=2)
