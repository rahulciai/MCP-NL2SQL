# Public VM Deployment Guide

This guide explains how to make the MMM NL2SQL MCP server available to Claude
users through a public VM.

The current `mmm-nl2sql.mcpb` package is a local Claude Desktop extension. It
is suitable for one computer, but other Claude users cannot use it directly.
For shared access, PostgreSQL and the MCP server must run on the VM behind a
secure HTTPS endpoint.

## Target architecture

```text
Claude users
    |
    | HTTPS + authentication
    v
https://mcp.example.com/mcp
    |
    v
MCP server on the VM
    |
    | private localhost connection
    v
PostgreSQL on the VM
```

Only the HTTPS MCP endpoint should be public. PostgreSQL port `5432` should
remain private.

## Required VM resources

- A public VM IP address.
- A domain name, for example `mcp.example.com`.
- Ubuntu/Linux or Windows Server.
- Python 3.10 or newer.
- PostgreSQL.
- An HTTPS reverse proxy such as Caddy or Nginx.
- An authentication method: OAuth is preferred; unique bearer tokens are a
  simpler alternative for a small trusted group.

## 1. Install the project on a Linux VM

Example Ubuntu commands:

```bash
sudo apt update
sudo apt install -y python3 python3-venv postgresql openssh-server

sudo mkdir -p /opt/mmm-nl2sql
sudo chown "$USER":"$USER" /opt/mmm-nl2sql
cd /opt/mmm-nl2sql

# Copy the project here or clone it from your private repository.
git clone YOUR_PRIVATE_REPOSITORY_URL .

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

Do not commit database passwords to Git.

## 2. Configure PostgreSQL

Create a database user with read-only permissions. The application should not
connect as a PostgreSQL superuser.

Example connection configuration on the VM:

```text
/opt/mmm-nl2sql/configuration/environment.env
```

```text
DATABASE_URL=postgresql://nl2sql_reader:URL_ENCODED_PASSWORD@127.0.0.1:5432/company_ai
```

Protect the file:

```bash
chmod 600 /opt/mmm-nl2sql/configuration/environment.env
```

Test the database connection:

```bash
cd /opt/mmm-nl2sql
.venv/bin/python connect_database.py
```

PostgreSQL should listen on localhost or a private network interface only. Do
not open port `5432` to the public internet.

## 3. Change the MCP server to remote HTTP transport

The current `main.py` starts a local stdio server. Public hosting requires a
remote HTTP-compatible MCP transport. The exact FastMCP transport name depends
on the installed version, but the intended configuration is:

```python
import os

from core.server_manifest import mcp


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")

    if transport == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host="127.0.0.1",
            port=int(os.getenv("MCP_PORT", "8000")),
        )
    else:
        mcp.run()
```

The MCP process should bind to `127.0.0.1`. The reverse proxy handles public
HTTPS traffic and forwards it to the local MCP process.

Verify the installed FastMCP version and its HTTP transport documentation
before deploying. Do not expose an unauthenticated MCP endpoint.

## 4. Add authentication

Every public request must be authenticated. Use one of these approaches:

- OAuth for multiple Claude users and account-level access control.
- A unique bearer token per approved user for a small internal group.
- A VPN/private network if all users are inside one organization.

Do not use one shared database password as the only user authentication method.
The database credential should remain on the VM and should not be displayed to
Claude users.

## 5. Configure HTTPS reverse proxy

Point `mcp.example.com` to the VM and allow only ports `80` and `443` through
the firewall. Use Caddy or Nginx to terminate TLS and proxy to the local MCP
port.

Example Caddy shape:

```text
mcp.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Add the authentication middleware before publishing the endpoint. Confirm the
endpoint uses a valid certificate and does not expose PostgreSQL directly.

## 6. Run the MCP server as a service

Example systemd service:

```ini
[Unit]
Description=MMM NL2SQL MCP Server
After=network-online.target postgresql.service

[Service]
Type=simple
User=nl2sql
WorkingDirectory=/opt/mmm-nl2sql
Environment=MCP_TRANSPORT=streamable-http
Environment=MCP_PORT=8000
ExecStart=/opt/mmm-nl2sql/.venv/bin/python /opt/mmm-nl2sql/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Save it as:

```text
/etc/systemd/system/mmm-nl2sql.service
```

Then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mmm-nl2sql
sudo systemctl status mmm-nl2sql
sudo journalctl -u mmm-nl2sql -f
```

## 7. Connect Claude users

For current Claude Desktop versions:

1. Open **Settings → Connectors**.
2. Add the remote MCP server URL supplied by the administrator.
3. Complete OAuth or enter the assigned bearer token if required.
4. Confirm that the `mmm-nl2sql` tools are visible.

Users should see tools such as:

```text
list_database_tables
inspect_table_schema
validate_sql_query
execute_read_only_query
```

The local `.mcpb` package is not used for this remote deployment. It is only
for local Claude Desktop installations.

## 8. Verify end to end

Test in this order:

1. PostgreSQL connection succeeds on the VM.
2. The MCP service is running.
3. The reverse proxy returns HTTPS successfully.
4. Authentication rejects requests without a valid token.
5. An authenticated Claude user can see the MCP tools.
6. `inspect_table_schema` returns the real PostgreSQL schema.
7. `validate_sql_query` detects invalid table or column names.
8. `execute_read_only_query` returns actual rows.
9. Write queries such as `UPDATE` or `DROP` are rejected.

Use a request such as:

```text
Inspect the live PostgreSQL schema, write a query for the top 10 physician
segments by Cardizor TRx, execute it, and return the exact SQL and actual rows.
Do not use assumptions or web search.
```

## 9. Security checklist

- Use a non-superuser, read-only PostgreSQL account.
- Keep PostgreSQL port `5432` private.
- Use HTTPS for all public MCP traffic.
- Require authentication for every MCP request.
- Use unique user tokens or OAuth rather than one shared token.
- Rotate database credentials and MCP tokens.
- Apply request rate limits.
- Keep query timeout and result-row limits enabled.
- Log authenticated requests without logging database passwords.
- Back up PostgreSQL and restrict VM SSH access.
- Review whether physician identifiers are allowed to be returned to every user.

## References

- [Claude Desktop local MCP extensions](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop)
- [Claude MCP transport documentation](https://code.claude.com/docs/en/mcp)
- [MCPB manifest specification](https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md)
