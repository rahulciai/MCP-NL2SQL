# MMM NL2SQL MCP Server for Claude Desktop

This project exposes a read-only PostgreSQL database to Claude Desktop through
the Model Context Protocol (MCP). Claude can use the business definitions in
`mmm.json` to translate questions such as:

> Which physician segments had the highest Cardizor TRx last month?

into PostgreSQL SQL, show the SQL, and run it when the user requests results.

## What is included

- `mmm.json`: startup-only source for the NL2SQL system instructions.
- `list_database_tables`: checks the live PostgreSQL database.
- `inspect_table_schema`: checks live columns and database metadata.
- `validate_sql_query`: asks PostgreSQL to validate the SQL plan without
  returning data.
- `execute_read_only_query`: runs one validated `SELECT`/`WITH` query, capped at
  150 rows and 30 seconds.
- Server instructions: the NL2SQL workflow is sent to Claude during MCP
  initialization.

The MMM catalog currently documents `Segmentation_Master_Dataset`,
`Pri_Nat_MMM_Results`, `Pri_Seg_MMM_Results`, `Pri_Nat_RC_Results`, and
`Pri_Seg_RC_Results` (some tables occur in more than one business category).

## 1. Install

From PowerShell:

```powershell
cd "C:\Users\RahulKumar\Documents\MCP NLSQL"
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Create `configuration\environment.env` from
`configuration\environment.env.example` and set the PostgreSQL connection:

```text
DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@localhost:5432/company_ai
```

URL-encode special characters in the password. For example, `@` becomes
`%40`.

## 2. Add it to Claude Desktop

For current Claude Desktop versions, install the included
`mmm-nl2sql.mcpb` extension package:

To recreate the package after changing the code, run this from PowerShell in
the project folder:

```powershell
cd "C:\Users\RahulKumar\Documents\MCP NLSQL"

Compress-Archive `
  -Path manifest.json, pyproject.toml, main.py, mmm.json, core, tools `
  -DestinationPath mmm-nl2sql.zip `
  -Force

Rename-Item mmm-nl2sql.zip mmm-nl2sql.mcpb -Force
```

This creates:

```text
C:\Users\RahulKumar\Documents\MCP NLSQL\mmm-nl2sql.mcpb
```

Then install that `.mcpb` file:

1. Open **Settings → Extensions → Advanced settings**.
2. Choose **Install Extension…**.
3. Select `mmm-nl2sql.mcpb` from this project folder.
4. When Claude asks for the database connection string, enter your PostgreSQL
   URL, for example:

   ```text
   postgresql://postgres:YOUR_PASSWORD@localhost:5432/company_ai
   ```

5. Save the extension settings and restart Claude Desktop.
6. Open the chat tools/connectors view and confirm that `mmm-nl2sql` is listed.

If your password contains special characters, URL-encode them. For example,
`@` becomes `%40`:

```text
postgresql://postgres:abc%40123@localhost:5432/company_ai
```

The extension stores the connection string as a sensitive setting, installs
the Python dependencies with the UV runtime, and starts the local server
automatically. When using the `.mcpb` extension, you do not need to create
`configuration\environment.env`.

For older Claude Desktop versions that support manual configuration, use the
JSON entry below instead. In that older flow, create
`configuration\environment.env` as described in the Install section.

Open:

```text
%APPDATA%\Claude\claude_desktop_config.json
```

Add the `mcpServers` entry from `config.json` (or copy the complete example):

```json
{
  "mcpServers": {
    "mmm-nl2sql": {
      "command": "C:\\Users\\RahulKumar\\Documents\\MCP NLSQL\\.venv\\Scripts\\python.exe",
      "args": [
        "C:\\Users\\RahulKumar\\Documents\\MCP NLSQL\\main.py"
      ]
    }
  }
}
```

Fully quit Claude Desktop from the system tray and reopen it. This is a local
stdio MCP server; it is not added through Claude's online connector directory.

## 3. Use it

Try:

```text
Find the top 10 physician segments by total Cardizor TRx and show me the SQL.
```

Other examples:

```text
Show total Cardizor TRx and unique physician count by state, ordered from highest to lowest. Show the validated SQL but do not execute it.
```

```text
Compare Cardizor NRx and Cardizor TRx by month using MONTH_YEAR for the most recent 12 months. Validate the SQL against PostgreSQL before showing it.
```

```text
For each region, calculate Cardizor TRx as a percentage of MARKET_TRX. Use REGION_NAME, CARDIZOR_TRX, and MARKET_TRX, validate the query, and then show me the SQL.
```

Then, if the SQL looks right:

```text
Run that query and summarize the results.
```

Claude receives the MMM-derived instructions at startup, verifies the live
schema, executes normal analytics queries against PostgreSQL, and returns the
actual SQL and rows from the database. A successful `EXPLAIN` alone is not
treated as verification. The server enforces read-only checks and runs each
query in a PostgreSQL read-only transaction. No web search or MMM catalog tool
is called during the workflow.

## Troubleshooting

Check the server can import without starting the stdio loop:

```powershell
.\.venv\Scripts\python.exe -c "from core.server_manifest import mcp; print(type(mcp).__name__)"
```

If Claude does not show `mmm-nl2sql`, verify the paths in
`claude_desktop_config.json`, check that PostgreSQL is running, and restart
Claude Desktop completely. Do not add print statements to `main.py`: stdout is
reserved for MCP protocol messages.
