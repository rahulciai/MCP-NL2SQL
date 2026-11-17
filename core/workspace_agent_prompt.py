"""Workspace Agent trigger input and callback instructions."""

from __future__ import annotations

from pathlib import Path


INSTRUCTIONS_FILE = (
    Path(__file__).resolve().parents[1]
    / "configuration"
    / "workspace_agent_instructions.md"
)


def load_workspace_agent_instructions() -> str:
    if INSTRUCTIONS_FILE.exists():
        return INSTRUCTIONS_FILE.read_text(encoding="utf-8").strip()
    return ""


def build_trigger_input(
    *,
    request_id: str,
    delivery_token: str,
    user_message: str,
) -> str:
    instructions = load_workspace_agent_instructions()
    return f"""{instructions}

--- CONFIDENTIAL BACKEND REQUEST METADATA ---
Request ID: {request_id}
Delivery token: {delivery_token}
The delivery token is a secret capability. Preserve it exactly for the callback, never include it in answer text, and never disclose it to the user.
--- END CONFIDENTIAL BACKEND REQUEST METADATA ---

--- USER REQUEST ---
{user_message}
--- END USER REQUEST ---

This is a mandatory API-delivery run. Complete the requested database task
using the connected MMM FastMCP tools. Do not finish with only a normal
assistant message. Before finishing, call submit_answer exactly once with the
supplied request_id and delivery_token, your final answer, and the actual
tabular results. If there are no tabular results, pass data as an empty list.
Do not pass the optional sql argument. SQL is an internal execution detail and
must not appear in the user-facing answer. Keep the answer text as a concise
interpretation without a SQL code block or SQL section. Put actual returned
rows only in data; the client renders those structured rows as a table.
Do not copy the same rows into answer text. Wait for a successful
submit_answer acknowledgement before claiming that delivery is complete. If
the tool call is rejected or requires approval, report that honestly and do
not claim that the backend received the answer.
""".strip()
