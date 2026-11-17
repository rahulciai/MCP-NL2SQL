"""Formatting helpers for Workspace Agent answers."""

from __future__ import annotations

import re


_SQL_LABEL_LINE_RE = re.compile(
    r"(?im)^[ \t]*(?:executed[ \t]+)?sql[ \t]*:[ \t]*\n?"
)
_EMPTY_SQL_FENCE_RE = re.compile(
    r"(?is)```[ \t]*sql[ \t]*\s*```[ \t]*"
)


def normalize_answer_sql(answer: str, sql: str | None) -> str:
    """Keep a supplied exact SQL statement to at most one fenced copy.

    Workspace Agent tool results contain an ``sql`` field. A model can copy
    that field into both a fenced block and a separate ``SQL:`` section. The
    callback/API layer uses this helper to make the displayed answer stable.
    Answers that do not contain the supplied SQL are left unchanged so the
    separate structured ``sql`` response field remains backward compatible.
    """
    if not sql or not sql.strip():
        return answer

    canonical_sql = sql.strip()
    match_sql = canonical_sql[:-1].rstrip() if canonical_sql.endswith(";") else canonical_sql
    tokens = match_sql.split()
    if not tokens:
        return answer

    sql_pattern = re.compile(
        r"\s+".join(re.escape(token) for token in tokens)
        + r"[ \t]*;?",
        re.IGNORECASE,
    )
    if sql_pattern.search(answer) is None:
        return answer

    cleaned = sql_pattern.sub("", answer)
    cleaned = _SQL_LABEL_LINE_RE.sub("", cleaned)
    cleaned = _EMPTY_SQL_FENCE_RE.sub("", cleaned)
    cleaned = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", cleaned)
    cleaned = cleaned.strip()

    sql_block = f"```sql\n{canonical_sql}\n```"
    return f"{cleaned}\n\n{sql_block}" if cleaned else sql_block
