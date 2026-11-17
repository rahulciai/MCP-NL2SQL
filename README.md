# MMM NL2SQL Workspace Agent bridge

This project exposes the existing read-only PostgreSQL FastMCP tools and a
backend bridge for a published ChatGPT Workspace Agent:

```text
Python/API client
    -> POST /chat (202 + request_id)
    -> ChatGPT Workspace Agent /trigger
    -> published agent calls this server's FastMCP database tools
    -> PostgreSQL read-only transaction
    -> published agent calls submit_answer
    -> SQLite result row
    -> GET /chat/requests/{request_id}
    -> actual answer, SQL, and data
```

The bridge intentionally does not use `OPENAI_API_KEY`, `OPENAI_MODEL`, Azure,
the Responses API, browser scraping, or a separately configured model. The
Workspace Agent trigger API is asynchronous and does not return final answer
text; the callback is the result-delivery mechanism.

## Requirements

- Python 3.10+
- PostgreSQL containing the MMM data
- A published Workspace Agent with an API channel
- A Workspace Agent access token with the Workspace Agents scope
- An externally reachable HTTPS MCP URL for the agent connection
- A persistent filesystem location for SQLite

The installed environment currently uses FastMCP 4.x. The project keeps the
compatibility import for older FastMCP releases already supported by the code.

## Configuration

Copy `.env.example` to `configuration/environment.env`, or export the same
variables. Process environment variables take precedence. The real file and
database credentials must remain outside source control.

```env
DATABASE_URL=postgresql://readonly_user:URL_ENCODED_PASSWORD@db-host:5432/database_name
DATABASE_SCHEMA=mmm

AGENT_ACCESS_TOKEN=replace_with_chatgpt_workspace_agent_access_token
WORKSPACE_AGENT_TRIGGER_URL=https://api.chatgpt.com/v1/workspace_agents/agtch_replace_with_published_api_channel_trigger_id/trigger

CHAT_API_TOKEN=replace_with_application_client_token
CHAT_API_TOKENS=

SESSION_DB_PATH=data/chat_sessions.sqlite3
REQUEST_TIMEOUT_SECONDS=120
CALLBACK_TOKEN_TTL_SECONDS=900
CALLBACK_GRACE_SECONDS=30
AGENT_HTTP_TIMEOUT_SECONDS=20
AGENT_POLL_INTERVAL_SECONDS=1
AGENT_POLL_TIMEOUT_SECONDS=1800
CHAT_MAX_MESSAGE_CHARS=8000
CHAT_MAX_ANSWER_CHARS=20000
CHAT_MAX_SQL_CHARS=100000
CHAT_MAX_DATA_BYTES=1000000
MCP_HOST=127.0.0.1
MCP_PORT=8000
```

Credential purposes are separate:

- `AGENT_ACCESS_TOKEN`: backend to `api.chatgpt.com`.
- `CHAT_API_TOKEN` or `CHAT_API_TOKENS`: external client to this backend.
- `delivery_token`: random, request-scoped capability supplied only in the
  agent trigger input; only its hash is stored in SQLite.

`WORKSPACE_AGENT_TRIGGER_URL` is the complete `/trigger` URL. The old
`WORKSPACE_AGENT_TRIGGER_ID` plus `AGENT_API_BASE_URL` names remain accepted as
a migration fallback, but new deployments should use the full URL.

There is no `MCP_SERVER_URL` setting in this callback flow. `MCP_HOST` and
`MCP_PORT` only bind the local listener. Configure the externally reachable
MCP URL in the Workspace Agent connection, for example:

```text
https://YOUR-NGROK-DOMAIN.ngrok-free.dev/mcp
```

The current repository did not contain a separate MCP bearer-auth setting.
If the deployment adds tunnel or MCP authentication, configure it at that
MCP connection/tunnel and preserve the same shared SQLite path for the API and
MCP processes.

## Configure the published Workspace Agent

1. Publish/save the agent and copy its API channel URL into
   `WORKSPACE_AGENT_TRIGGER_URL`.
2. Configure the agent's MCP connection with the public HTTPS URL ending in
   `/mcp` and refresh/reconnect it after tool changes.
3. Confirm that the tools `list_database_tables`, `inspect_table_schema`,
   `validate_sql_query`, `execute_read_only_query`, and `submit_answer` are
   visible.
4. Paste the following instructions into the published agent.
5. Review the approval policy for `submit_answer`. It writes application
   state, so the tool is intentionally annotated `readOnlyHint: false`.

FastMCP annotations are hints for tool consumers; they cannot override ChatGPT
workspace/admin approval policy. If `submit_answer` is approval-gated, the
agent can suspend and unattended operation remains blocked. The backend does
not bypass that policy.

### Instructions to paste into the agent

```text
You are the published MMM NL2SQL Workspace Agent. Use only the connected MMM
FastMCP tools for database work.

For every database task:
1. Inspect live database tables and relevant schemas before writing SQL.
2. Treat the live schema as authoritative. Never invent tables, columns, joins,
   measures, definitions, or rows.
3. Use only read-only SELECT or WITH SQL. Validate and execute through the
   provided database tools.
4. Ground every factual claim in actual returned tool results.
5. Use SQL internally, but do not expose query text in the answer. Return a
brief interpretation and one compact table containing only actual returned

For an API-triggered run, trusted backend context contains exactly:
- Request ID
- Delivery token

Preserve those values exactly. Never invent, alter, log, disclose, or include
the delivery token in user-facing answer text.

When trusted callback context is present, this is a mandatory API-delivery run.
Call submit_answer exactly once after the task is complete; do not finish with
only a normal assistant message:
- request_id: the supplied Request ID, unchanged
- delivery_token: the supplied Delivery token, unchanged
- answer: the brief final answer without SQL text
- data: actual returned tabular results, or [] when there is no table
- sql: omit this optional argument; SQL is internal and not returned

Send clarifications and honest error answers through submit_answer too, with
data: [] when no query result exists. Wait for a successful tool acknowledgement
before claiming delivery. If the tool call is rejected or requires approval,
report that honestly and do not claim the backend received the answer.

In ordinary ChatGPT conversations without trusted callback context, do not call
submit_answer; answer normally. Never substitute a conversation URL for the
callback result and never fabricate rows or callback identifiers.
```

## Start locally

The combined Uvicorn process hosts FastAPI and FastMCP:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt

# Terminal 1: expose the MCP endpoint to the published agent
ngrok http 8000

# Terminal 2: start the combined server
PYTHONPATH=. python main.py
```

Local routes:

```text
GET  /healthz
POST /chat
GET  /chat/requests/{request_id}
GET  /chat/sessions/{session_id}/requests
MCP  /mcp
```

The SQLite parent directory is created automatically. In a container, mount a
persistent volume at the directory containing `SESSION_DB_PATH`; otherwise
request/session state will be lost when the container is replaced.

## API contract

### Submit a question

```bash
curl -i -X POST http://127.0.0.1:8000/chat \
  -H 'Authorization: Bearer YOUR_CHAT_API_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"message":"Find the top 10 physician segments by total Cardizor TRx and show the SQL.","session_id":null}'
```

Accepted response (`202`):

```json
{
  "request_id": "req_...",
  "session_id": "sess_...",
  "status": "queued"
}
```

The backend persists the request before triggering the agent. It sends a
stable `conversation_key`, the exact request ID, the callback token, and a
persisted `Idempotency-Key`. It never silently retries an uncertain trigger
submission with a new key.

### Retrieve a result

```bash
curl -s http://127.0.0.1:8000/chat/requests/req_... \
  -H 'Authorization: Bearer YOUR_CHAT_API_TOKEN'
```

Pending response:

```json
{
  "request_id": "req_...",
  "session_id": "sess_...",
  "status": "queued",
  "agent_status": "in_progress",
  "callback_status": "pending",
  "answer": null,
  "data": [],
  "sql": null
}
```

Completed response:

```json
{
  "request_id": "req_...",
  "session_id": "sess_...",
  "status": "completed",
  "agent_status": "completed",
  "callback_status": "completed",
  "answer": "Actual answer",
  "data": [
    {"columns": ["segment", "total_trx"], "rows": [["Example", 123]]}
  ],
  "sql": "SELECT ..."
}
```

The API stores `agent_status` separately from callback delivery. An agent run
with status `completed` is not an answer until a valid `submit_answer` callback
has been persisted.

Unknown requests and another caller's requests return the same response:

```json
{
  "error": {
    "code": "request_not_found",
    "message": "Request was not found."
  }
}
```

### List requests for a session

```bash
curl -s 'http://127.0.0.1:8000/chat/sessions/sess_.../requests?limit=25&offset=0' \
  -H 'Authorization: Bearer YOUR_CHAT_API_TOKEN'
```

Response:

```json
{
  "session_id": "sess_...",
  "items": [
    {
      "request_id": "req_...",
      "status": "completed",
      "agent_status": "completed",
      "callback_status": "completed",
      "created_at": "2026-01-01T00:00:00+00:00",
      "updated_at": "2026-01-01T00:00:02+00:00"
    }
  ],
  "limit": 25,
  "offset": 0,
  "next_offset": null
}
```

### Follow-up question

Use the returned session ID; the backend verifies ownership and reuses its
stable Workspace Agent `conversation_key`:

```bash
curl -i -X POST http://127.0.0.1:8000/chat \
  -H 'Authorization: Bearer YOUR_CHAT_API_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"message":"Explain the highest segment using the previous result.","session_id":"sess_..."}'
```

## Terminal client

```bash
CHAT_API_URL=http://127.0.0.1:8000 \
CHAT_API_TOKEN=YOUR_CHAT_API_TOKEN \
PYTHONPATH=. python agent_chat.py
```

`agent_chat.py` stores accepted request IDs and completed answer/data/SQL
metadata locally without storing credentials. It shows a loader during the
initial POST and result polling, supports `/new`, `/list`, `/use NUMBER`,
`/help`, and `/quit`, stops on terminal failures, and never automatically
resends uncertain submissions.

## Status and recovery behavior

- `queued`: trigger accepted or status is still pending; inspect
  `agent_status` and `callback_status`.
- `suspended`: the agent is waiting for an external action or may require
  approval. This is not automatically classified as approval in every case.
- `failed`: trigger/run failure with structured `error`.
- `callback_missing`: the agent completed but did not call `submit_answer`
  during the grace period.
- `callback_expired`: the bounded callback deadline/token expiry passed.
- `trigger_unknown`: the HTTP trigger outcome was uncertain. The persisted
  idempotency key is retained for an operator-controlled recovery; the client
  does not submit a duplicate automatically.

Callbacks are accepted atomically only while the request token is valid and
the request is accepting results. Identical repeated callbacks are acknowledged
as duplicates; conflicting replacements are rejected. A callback that wins a
race before trigger-response metadata is saved remains completed because all
later metadata transitions preserve callback completion.

The server logs callback outcomes without logging delivery tokens, answers, SQL,
or table data. Look for entries such as
`workspace_callback request_id=req_... outcome=accepted`, `duplicate`,
`delivery_token_invalid`, `delivery_token_expired`, or `request_not_found`.
Keep the Uvicorn terminal or service log available while diagnosing a run. A
Workspace Agent `completed` status without an `accepted` callback log is not a
successful answer.

## Database and MCP tools

The existing tools remain available:

- `list_database_tables`
- `inspect_table_schema`
- `validate_sql_query`
- `execute_read_only_query`
- `submit_answer` (application-result persistence, not read-only)

Database SQL is validated as a single `SELECT`/`WITH` statement and executed in
a PostgreSQL read-only transaction with row and statement limits. Use a
PostgreSQL read-only role as an additional permission boundary.

## Tests

Run mocked validation:

```bash
PYTHONPATH=. .venv/bin/python -m compileall -q api core tools main.py agent_chat.py tests
PYTHONPATH=. .venv/bin/pytest -q
git diff --check
```

The automated tests do not call the live Workspace Agent or modify production
data. A real end-to-end test still requires a valid Workspace Agent token,
published trigger, refreshed MCP connection, reachable `/mcp` URL, database
credentials, and an approval policy that permits unattended `submit_answer`.

## Official references

- [Trigger Workspace Agent runs](https://developers.openai.com/workspace-agents/trigger-runs)
- [Authenticate with Workspace Agent access tokens](https://developers.openai.com/workspace-agents/authentication)
- nl2sql-ngrok