"""Load and search the MMM semantic catalog used by the NL2SQL agent.

``mmm.json`` is intentionally kept separate from the live database schema.  It
contains business meaning, aliases, and examples that PostgreSQL cannot
provide through ``information_schema``.  The live schema remains authoritative
for whether a query can actually run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MMM_FILE = PROJECT_ROOT / "mmm.json"


@dataclass(frozen=True)
class ColumnMetadata:
    name: str
    description: str = ""
    data_type: str = ""
    data_format: str = ""
    aliases: tuple[str, ...] = ()
    sample_values: tuple[str, ...] = ()


@dataclass
class TableMetadata:
    name: str
    description: str = ""
    categories: set[str] = field(default_factory=set)
    columns: list[ColumnMetadata] = field(default_factory=list)


def _column_from_json(raw: dict[str, Any]) -> ColumnMetadata:
    return ColumnMetadata(
        name=str(raw.get("name") or raw.get("name1") or ""),
        description=str(raw.get("description") or ""),
        data_type=str(raw.get("data_type") or ""),
        data_format=str(raw.get("data_format") or ""),
        aliases=tuple(str(value) for value in raw.get("aliases_synonyms", []) if value),
        sample_values=tuple(str(value) for value in raw.get("sample_values", []) if value),
    )


@lru_cache(maxsize=1)
def load_catalog() -> tuple[TableMetadata, ...]:
    """Read and de-duplicate tables repeated across MMM categories."""
    if not MMM_FILE.exists():
        raise RuntimeError(f"MMM catalog was not found at {MMM_FILE}")

    try:
        document = json.loads(MMM_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to load MMM catalog: {exc}") from exc

    tables: dict[str, TableMetadata] = {}
    for item in document.get("MMM", []):
        for category in item.get("Category", []):
            category_name = str(category.get("name") or "")
            for raw_table in category.get("Tables", []):
                table_name = str(raw_table.get("name") or "").strip()
                if not table_name:
                    continue

                table = tables.setdefault(table_name, TableMetadata(name=table_name))
                table.description = table.description or str(raw_table.get("description") or "")
                if category_name:
                    table.categories.add(category_name)

                known_columns = {column.name for column in table.columns}
                for raw_column in raw_table.get("columns", []):
                    column = _column_from_json(raw_column)
                    if column.name and column.name not in known_columns:
                        table.columns.append(column)
                        known_columns.add(column.name)

    return tuple(tables.values())


def render_catalog_summary() -> str:
    """Render compact catalog context for MCP server instructions."""
    lines = ["MMM semantic catalog (use exact PostgreSQL identifiers from the live schema):"]
    for table in load_catalog():
        categories = ", ".join(sorted(table.categories))
        lines.append(f"- {table.name} [{categories}]: {table.description}")
        lines.append("  Columns: " + ", ".join(column.name for column in table.columns))
    return "\n".join(lines)
