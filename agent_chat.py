#!/usr/bin/env python3
"""Terminal client for the authenticated MMM NL2SQL chat API."""

from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


from core.settings import load_environment_file


class APIClientError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


TERMINAL_REQUEST_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "callback_missing",
        "callback_expired",
        "trigger_unknown",
    }
)


class Loader:
    def __init__(self, message: str = "Waiting for the Workspace Agent") -> None:
        self.message = message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        def run() -> None:
            for character in itertools.cycle("|/-\\"):
                if self._stop.is_set():
                    return
                print(f"\r{self.message} {character}", end="", flush=True)
                time.sleep(0.12)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        print("\r" + (" " * (len(self.message) + 2)) + "\r", end="", flush=True)


class ChatAPIClient:
    def __init__(self, base_url: str, token: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(2_000_000)
                decoded = json.loads(raw.decode("utf-8")) if raw else {}
                return decoded if isinstance(decoded, dict) else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read(256_000)
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                message = str(error.get("message") or f"HTTP {exc.code}")
            else:
                message = f"HTTP {exc.code} from the chat server."
            raise APIClientError(message, status_code=exc.code, payload=payload) from exc
        except urllib.error.URLError as exc:
            raise APIClientError(
                "Could not reach the chat server. Check the URL, server process, and tunnel."
            ) from exc
        except TimeoutError as exc:
            raise APIClientError(
                "The chat request timed out. It was not automatically resent."
            ) from exc

    def chat(self, message: str, session_id: str | None) -> dict[str, Any]:
        return self._request(
            "POST",
            "/chat",
            {"message": message, "session_id": session_id},
        )

    def get_request(self, request_id: str) -> dict[str, Any]:
        return self._request("GET", f"/chat/requests/{request_id}")

    def wait_for_request(
        self,
        request_id: str,
        *,
        wait_timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + wait_timeout_seconds
        while True:
            payload = self.get_request(request_id)
            status = payload.get("status")
            if status == "completed":
                return payload
            if status in TERMINAL_REQUEST_STATUSES - {"completed"}:
                error = payload.get("error")
                message = (
                    str(error.get("message"))
                    if isinstance(error, dict) and error.get("message")
                    else f"Request ended with status {status}."
                )
                raise APIClientError(
                    message,
                    status_code=502,
                    payload=payload,
                )
            if time.monotonic() >= deadline:
                raise APIClientError(
                    "Timed out waiting for the callback. The request was not resent.",
                    status_code=504,
                    payload={
                        "error": {
                            "code": "client_wait_timeout",
                            "message": "The callback has not arrived yet.",
                            "request_id": request_id,
                        }
                    },
                )
            time.sleep(poll_interval_seconds)



def _state_path() -> Path:
    return Path(
        os.getenv(
            "AGENT_CHAT_STATE_PATH",
            "~/.mmm_nl2sql_sessions.json",
        )
    ).expanduser()


def load_state(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"sessions": [], "requests": [], "active_session": None}
    if not isinstance(parsed, dict):
        return {"sessions": [], "requests": [], "active_session": None}
    sessions = parsed.get("sessions")
    if not isinstance(sessions, list):
        sessions = []
    requests = parsed.get("requests")
    if not isinstance(requests, list):
        requests = []
    return {
        "sessions": [
            item
            for item in sessions
            if isinstance(item, dict) and isinstance(item.get("session_id"), str)
        ],
        "requests": [
            item
            for item in requests
            if isinstance(item, dict) and isinstance(item.get("request_id"), str)
        ],
        "active_session": (
            parsed.get("active_session")
            if isinstance(parsed.get("active_session"), str)
            else None
        ),
    }


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def remember_session(state: dict[str, Any], session_id: str) -> None:
    sessions = state["sessions"]
    if not any(item.get("session_id") == session_id for item in sessions):
        sessions.append(
            {
                "label": f"Session {len(sessions) + 1}",
                "session_id": session_id,
            }
        )
    state["active_session"] = session_id


def remember_request(state: dict[str, Any], payload: dict[str, Any]) -> None:
    """Persist request/result metadata locally, never credentials."""
    request_id = payload.get("request_id")
    if not isinstance(request_id, str):
        return
    requests = state.setdefault("requests", [])
    existing = next(
        (item for item in requests if item.get("request_id") == request_id),
        None,
    )
    if existing is None:
        existing = {"request_id": request_id}
        requests.append(existing)
    for key in (
        "session_id",
        "status",
        "agent_status",
        "callback_status",
        "answer",
        "data",
        "sql",
        "error",
        "created_at",
        "updated_at",
    ):
        if key in payload:
            existing[key] = payload[key]
    session_id = payload.get("session_id")
    if isinstance(session_id, str):
        remember_session(state, session_id)


def print_tables(data: Any) -> None:
    if not isinstance(data, list) or not data:
        return
    for index, table in enumerate(data, start=1):
        if not isinstance(table, dict):
            continue
        columns = table.get("columns") or []
        rows = table.get("rows") or []
        print(f"\nTable {index}:")
        if columns:
            print(" | ".join(str(column) for column in columns))
            print("-+-".join("-" * max(3, len(str(column))) for column in columns))
        for row in rows:
            if isinstance(row, (list, tuple)):
                print(" | ".join(str(value) for value in row))
            else:
                print(str(row))


def answer_contains_markdown_table(answer: str) -> bool:
    """Return whether answer already renders a Markdown table.

    Structured ``data`` is normally the single source used for terminal table
    output. This compatibility check prevents older agent instructions (or a
    model that includes a table despite the instructions) from displaying the
    same rows twice.
    """
    lines = answer.splitlines()
    separator = re.compile(
        r"^[ ]*[|]?[ ]*:?-{3,}:?[ ]*(?:[|][ ]*:?-{3,}:?[ ]*)*[|]?[ ]*$"
    )
    return any(
        "|" in line and separator.match(next_line or "")
        for line, next_line in zip(lines, lines[1:])
    )


def print_help() -> None:
    print(
        "Commands:\n"
        "  /new       start a new application session\n"
        "  /list      list locally saved session references\n"
        "  /use N     continue session number N\n"
        "  /help      show this help\n"
        "  /quit      exit\n"
        "\n"
        "Session references are saved locally without the API token."
    )


def run() -> int:
    # Load configuration/environment.env before resolving CLI defaults.
    # Existing shell variables remain authoritative because the shared loader
    # uses os.environ.setdefault(...).
    load_environment_file()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.getenv("CHAT_API_URL", "http://127.0.0.1:8000"),
        help="Base URL of the FastAPI server.",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("CHAT_API_TOKEN", ""),
        help="Bearer token; it is never written to the local session file.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("CHAT_CLIENT_TIMEOUT_SECONDS", "150")),
        help="HTTP timeout in seconds.",
    )
    parser.add_argument(
        "--wait-timeout",
        type=float,
        default=float(os.getenv("CHAT_CLIENT_WAIT_TIMEOUT_SECONDS", "900")),
        help="Maximum time to poll one accepted request.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.getenv("CHAT_CLIENT_POLL_INTERVAL_SECONDS", "1")),
        help="Seconds between result requests.",
    )
    args = parser.parse_args()

    if not args.token:
        print(
            "No chat token supplied. Set CHAT_API_TOKEN or pass --token.",
            file=sys.stderr,
        )
        return 2

    state_path = _state_path()
    state = load_state(state_path)
    client = ChatAPIClient(args.url, args.token, args.timeout)
    print("MMM NL2SQL chat client. Type /help for commands.")

    while True:
        try:
            message = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not message:
            continue

        if message == "/quit":
            return 0
        if message == "/help":
            print_help()
            continue
        if message == "/new":
            state["active_session"] = None
            save_state(state_path, state)
            print("The next message will create a new session.")
            continue
        if message == "/list":
            sessions = state["sessions"]
            if not sessions:
                print("No saved sessions.")
                continue
            for index, item in enumerate(sessions, start=1):
                marker = "*" if item["session_id"] == state.get("active_session") else " "
                print(f"{marker} {index}: {item.get('label', 'Session')} ({item['session_id']})")
            continue
        if message.startswith("/use "):
            try:
                index = int(message.split(maxsplit=1)[1]) - 1
                item = state["sessions"][index]
            except (ValueError, IndexError):
                print("Usage: /use NUMBER, where NUMBER is shown by /list.")
                continue
            state["active_session"] = item["session_id"]
            save_state(state_path, state)
            print(f"Using {item['session_id']}.")
            continue

        loader = Loader()
        loader.start()
        request_id: str | None = None
        try:
            accepted = client.chat(message, state.get("active_session"))
            remember_request(state, accepted)
            save_state(state_path, state)
            request_id = accepted.get("request_id")
            if not isinstance(request_id, str):
                raise APIClientError(
                    "The server accepted the request without returning a request_id."
                )
            response = client.wait_for_request(
                request_id,
                wait_timeout_seconds=args.wait_timeout,
                poll_interval_seconds=max(0.1, args.poll_interval),
            )
            remember_request(state, response)
            save_state(state_path, state)
        except APIClientError as exc:
            print(f"Request failed: {exc}", file=sys.stderr)
            payload = exc.payload if isinstance(exc.payload, dict) else {}
            payload_request_id = payload.get("request_id")
            if isinstance(payload_request_id, str):
                request_id = payload_request_id
            terminal_status = payload.get("status")
            if terminal_status in TERMINAL_REQUEST_STATUSES - {"completed"}:
                # Replace the locally queued record with the server's terminal
                # state. This clears the client's unresolved/pending view while
                # retaining the request ID and structured error for diagnostics.
                remember_request(state, payload)
                save_state(state_path, state)
                error = payload.get("error")
                error_code = (
                    error.get("code")
                    if isinstance(error, dict) and error.get("code")
                    else terminal_status
                )
                print(
                    f"Terminal request status: {terminal_status} ({error_code}). "
                    "No further polling or automatic resend will be attempted.",
                    file=sys.stderr,
                )
                if isinstance(request_id, str):
                    print(
                        f"Request ID: {request_id}. The terminal failure was saved "
                        "locally for diagnostics.",
                        file=sys.stderr,
                    )
                continue
            if exc.status_code == 401:
                print(
                    "Authentication failed. Check CHAT_API_TOKEN or the --token value.",
                    file=sys.stderr,
                )
            elif exc.status_code == 504:
                print(
                    "The request wait timed out. It was not automatically resent.",
                    file=sys.stderr,
                )
                error = exc.payload.get("error") if isinstance(exc.payload, dict) else None
                request_id = error.get("request_id") if isinstance(error, dict) else None
                if isinstance(request_id, str):
                    print(
                        f"Request ID: {request_id}. Retrieve it later with GET /chat/requests/{request_id}.",
                        file=sys.stderr,
                    )
            elif exc.status_code == 409:
                print(
                    "The Workspace Agent is suspended or another request is still active; do not resend automatically.",
                    file=sys.stderr,
                )
            continue
        finally:
            loader.stop()

        session_id = response.get("session_id")
        if isinstance(session_id, str):
            remember_session(state, session_id)
        save_state(state_path, state)
        answer = str(response.get("answer") or "")
        print(f"\nAssistant: {answer}")
        # The callback data is the canonical table representation. Older
        # agent runs may still include a Markdown table in ``answer``; avoid
        # rendering the structured rows a second time for those responses.
        if not answer_contains_markdown_table(answer):
            print_tables(response.get("data"))




if __name__ == "__main__":
    raise SystemExit(run())
