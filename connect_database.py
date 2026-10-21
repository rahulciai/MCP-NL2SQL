"""Small local diagnostic for the same connection used by the MCP server."""

from core.connection_engine import fetch_all


if __name__ == "__main__":
    try:
        rows = fetch_all(
            "SELECT current_database() AS database_name, current_user AS user_name"
        )
        row = rows[0]
        print("PostgreSQL connection successful.")
        print(f"Database: {row['database_name']}")
        print(f"User: {row['user_name']}")
    except Exception as exc:
        print("PostgreSQL connection failed.")
        print(f"Error: {exc}")
