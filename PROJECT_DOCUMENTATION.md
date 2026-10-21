# MMM NL2SQL MCP Server

## Architecture

Claude Desktop starts `main.py` as a local stdio MCP server. The server sends
an NL2SQL system instruction during MCP initialization. That instruction is
generated from `mmm.json` by `core/prompt_builder.py` and includes the catalog's
table and column names.

`core/mmm_catalog.py` loads the catalog once, de-duplicates tables repeated
across categories, and renders the startup instructions. It is not exposed as
a runtime MCP tool. `tools/discovery_tools.py` only exposes live PostgreSQL
schema discovery.

The live schema always wins over the catalog when the two differ. This matters
because `mmm.json` explains business meaning while PostgreSQL determines the
actual identifiers and types available for execution.

## Claude Desktop installation

Current Claude Desktop uses the packaged `mmm-nl2sql.mcpb` extension:

1. Open **Settings → Extensions → Advanced settings**.
2. Select **Install Extension…** and choose `mmm-nl2sql.mcpb`.
3. Enter the PostgreSQL connection string when prompted, for example
   `postgresql://postgres:YOUR_PASSWORD@localhost:5432/company_ai`.
4. URL-encode special password characters, save the extension, and restart
   Claude Desktop.
5. Confirm that the `mmm-nl2sql` tools appear in the chat tools/connectors view.

The extension passes the connection string as a sensitive `DATABASE_URL`
setting. A local `configuration/environment.env` file is only needed when
running the server manually or using the older JSON configuration flow.

## MCP tools

| Tool | Purpose |
| --- | --- |
| `list_database_tables` | List actual public PostgreSQL tables. |
| `inspect_table_schema` | Inspect actual columns and database metadata. |
| `validate_sql_query` | Validate SQL with PostgreSQL planning without returning data. |
| `execute_read_only_query` | Execute one safe `SELECT`/`WITH` query. |

The query tool rejects non-read-only statements, multiple statements, and
queries over 30 seconds. Results are capped at 150 rows. The database
connection also uses a PostgreSQL read-only transaction.

## File responsibilities

- `main.py`: silent Claude Desktop entrypoint; stdout is reserved for MCP.
- `core/server_manifest.py`: creates the `mmm-nl2sql` server and registers tools.
- `core/prompt_builder.py`: builds the startup system instructions from the catalog.
- `core/mmm_catalog.py`: parses and de-duplicates `mmm.json` for prompt generation.
- `core/connection_engine.py`: connection pooling and read-only transactions.
- `tools/discovery_tools.py`: semantic and live schema tools.
- `tools/query_tools.py`: PostgreSQL SQL validation and execution.
- `config.json`: ready-to-copy Claude Desktop server entry.
- `manifest.json`: Claude Desktop MCPB extension manifest.
- `pyproject.toml`: dependencies used when the MCPB extension is installed.
- `configuration/environment.env.example`: database URL template.

## Natural-language-to-SQL behavior

The MCP server does not contain a second LLM. Claude Desktop is the language
model: it receives the system instructions and uses the catalog/schema tools to
write SQL. This keeps the MCP server deterministic and makes the generated SQL
visible in the Claude conversation. `mmm.json` is used only while building the
startup instructions; runtime discovery uses PostgreSQL.

Recommended request pattern:

1. Ask Claude to show the SQL for a natural-language request.
2. Review the SQL and clarify any ambiguous business term.
3. Ask Claude to run the reviewed query and summarize the results.

## Security boundary

Use a PostgreSQL account that only has `SELECT` privileges on the analytics
schemas. The application-side validation is defense in depth, not a
replacement for database permissions or network security.
