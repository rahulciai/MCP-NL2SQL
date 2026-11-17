from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import agent_chat
from api.app import create_app
from core.database_service import _validate_read_only_sql, execute_read_only_query_result
from core.server_manifest import mcp
from core.session_store import (
    DeliveryTokenError,
    RequestStateError,
    SessionStore,
)
from core.settings import AppSettings
from core.workspace_agent_client import RunStatus, TriggerResult, WorkspaceAgentClient


def make_settings(
    path: Path,
    *,
    timeout: float = 1.0,
    callback_grace: float = 0.05,
    tokens: tuple[tuple[str, str], ...] = (("alice", "alice-token"), ("bob", "bob-token")),
) -> AppSettings:
    return AppSettings(
        agent_access_token="agent-secret",
        workspace_agent_trigger_url=(
            "https://api.chatgpt.com/v1/workspace_agents/"
            "agtch_test_trigger/trigger"
        ),
        chat_api_token="",
        chat_api_tokens=tokens,
        database_schema="mmm",
        session_db_path=path,
        request_timeout_seconds=timeout,
        callback_token_ttl_seconds=900,
        callback_grace_seconds=callback_grace,
        agent_http_timeout_seconds=0.2,
        agent_poll_interval_seconds=0.01,
        agent_poll_timeout_seconds=2,
        max_message_chars=8_000,
        max_answer_chars=20_000,
        max_sql_chars=100_000,
        max_data_bytes=1_000_000,
        mcp_host="127.0.0.1",
        mcp_port=8000,
    )


def parse_trigger_input(input_text: str) -> tuple[str, str]:
    request_id = re.search(r"^Request ID: (.+)$", input_text, re.MULTILINE)
    delivery_token = re.search(r"^Delivery token: (.+)$", input_text, re.MULTILINE)
    assert request_id is not None
    assert delivery_token is not None
    return request_id.group(1), delivery_token.group(1)


class ManualAgentClient:
    def __init__(self, status: str = "in_progress") -> None:
        self.status = status
        self.calls: list[dict] = []
        self.status_calls: list[str] = []

    async def trigger(self, **kwargs):
        self.calls.append(kwargs)
        return TriggerResult(
            run_id="apirun_test",
            status="queued",
            conversation_url="https://chatgpt.com/c/test",
        )

    async def get_run_status(self, run_id: str) -> RunStatus:
        self.status_calls.append(run_id)
        return RunStatus(
            status=self.status,
            error_code="dispatch_failed" if self.status == "failed" else None,
            error_message="The agent run failed." if self.status == "failed" else None,
        )


class CallbackBeforeResponseAgentClient(ManualAgentClient):
    async def trigger(self, **kwargs):
        self.calls.append(kwargs)
        request_id, delivery_token = parse_trigger_input(kwargs["input_text"])
        result = await mcp.call_tool(
            "submit_answer",
            {
                "request_id": request_id,
                "delivery_token": delivery_token,
                "answer": "callback won the race",
                "data": [{"columns": ["value"], "rows": [[1]]}],
                "sql": "SELECT 1 AS value",
            },
        )
        assert '"accepted": true' in result.content[0].text
        return TriggerResult(
            run_id="apirun_race",
            status="queued",
            conversation_url="https://chatgpt.com/c/race",
        )


class SlowAgentClient(ManualAgentClient):
    async def trigger(self, **kwargs):
        self.calls.append(kwargs)
        await asyncio.sleep(1)
        return TriggerResult(run_id="apirun_late", status="queued")


class FakeHTTPResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self.payload = payload

    def json(self):
        return self.payload


class FakeHTTPClient:
    def __init__(self, response: FakeHTTPResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response


class ChatBackendTests(unittest.TestCase):
    def test_post_returns_202_and_persists_trigger_before_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = SessionStore(path)
            fake = ManualAgentClient()
            client = TestClient(
                create_app(
                    settings=make_settings(path),
                    store=store,
                    agent_client=fake,
                    mount_mcp=False,
                )
            )

            response = client.post(
                "/chat",
                headers={"Authorization": "Bearer alice-token"},
                json={"message": "Find the top segment.", "session_id": None},
            )
            self.assertEqual(response.status_code, 202)
            payload = response.json()
            self.assertTrue(payload["request_id"].startswith("req_"))
            self.assertTrue(payload["session_id"].startswith("sess_"))
            self.assertEqual(payload["status"], "queued")
            self.assertEqual(len(fake.calls), 1)
            call = fake.calls[0]
            self.assertTrue(call["conversation_key"].startswith("mmm_"))
            self.assertTrue(call["idempotency_key"].startswith("mmm-chat-req_"))
            request_id, delivery_token = parse_trigger_input(call["input_text"])
            self.assertEqual(request_id, payload["request_id"])
            self.assertGreater(len(delivery_token), 20)
            self.assertIn("Find the top segment.", call["input_text"])
            self.assertIn("mandatory API-delivery run", call["input_text"])
            self.assertIn("Do not finish with only a normal", call["input_text"])
            self.assertIn("Wait for a successful\nsubmit_answer acknowledgement", call["input_text"])
            self.assertNotIn("agent-secret", call["input_text"])
            record = store.get_request(request_id)
            self.assertEqual(record.status, "queued")
            self.assertEqual(record.callback_status, "pending")
            self.assertIsNone(record.answer)

    def test_callback_result_is_retrieved_as_table_without_sql_in_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = SessionStore(path)
            fake = ManualAgentClient()
            client = TestClient(
                create_app(
                    settings=make_settings(path),
                    store=store,
                    agent_client=fake,
                    mount_mcp=False,
                )
            )
            accepted = client.post(
                "/chat",
                headers={"Authorization": "Bearer alice-token"},
                json={"message": "Find the top segment."},
            ).json()
            request_id, delivery_token = parse_trigger_input(fake.calls[0]["input_text"])
            self.assertEqual(request_id, accepted["request_id"])
            store.submit_answer(
                request_id=request_id,
                delivery_token=delivery_token,
                answer="Top segment is A.",
                data=[{"columns": ["segment"], "rows": [["A"]]}],
                sql="SELECT segment FROM physician_segments LIMIT 1",
            )

            result = client.get(
                f"/chat/requests/{request_id}",
                headers={"Authorization": "Bearer alice-token"},
            )
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["status"], "completed")
            answer = result.json()["answer"]
            self.assertEqual(answer, "Top segment is A.")
            self.assertNotIn("SELECT segment FROM physician_segments LIMIT 1", answer)
            self.assertEqual(result.json()["sql"], "SELECT segment FROM physician_segments LIMIT 1")
            self.assertEqual(result.json()["data"][0]["rows"], [["A"]])

    def test_follow_up_reuses_conversation_key_and_overlapping_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = SessionStore(path)
            fake = ManualAgentClient()
            client = TestClient(
                create_app(
                    settings=make_settings(path),
                    store=store,
                    agent_client=fake,
                    mount_mcp=False,
                )
            )
            headers = {"Authorization": "Bearer alice-token"}
            first = client.post("/chat", headers=headers, json={"message": "First"})
            session_id = first.json()["session_id"]
            overlapping = client.post(
                "/chat",
                headers=headers,
                json={"message": "Overlap", "session_id": session_id},
            )
            self.assertEqual(overlapping.status_code, 409)
            first_request_id, first_token = parse_trigger_input(fake.calls[0]["input_text"])
            store.submit_answer(
                request_id=first_request_id,
                delivery_token=first_token,
                answer="First answer",
                data=[],
            )
            second = client.post(
                "/chat",
                headers=headers,
                json={"message": "Follow up", "session_id": session_id},
            )
            self.assertEqual(second.status_code, 202)
            self.assertEqual(
                fake.calls[0]["conversation_key"], fake.calls[1]["conversation_key"]
            )

    def test_callback_token_validation_duplicate_and_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "state.sqlite3")
            session = store.create_session("alice")
            pending = store.create_request(
                caller_id="alice",
                session_id=session.session_id,
                conversation_key=session.conversation_key or "",
                message="question",
                callback_token_ttl_seconds=900,
            )
            first = store.submit_answer(
                request_id=pending.record.request_id,
                delivery_token=pending.delivery_token,
                answer="done",
                data=[],
                sql="SELECT 1",
            )
            duplicate = store.submit_answer(
                request_id=pending.record.request_id,
                delivery_token=pending.delivery_token,
                answer="done",
                data=[],
                sql="SELECT 1",
            )
            self.assertFalse(first.duplicate)
            self.assertTrue(duplicate.duplicate)
            with self.assertRaises(DeliveryTokenError):
                store.submit_answer(
                    request_id=pending.record.request_id,
                    delivery_token="wrong-token",
                    answer="bad",
                    data=[],
                )
            with self.assertRaises(RequestStateError):
                store.submit_answer(
                    request_id=pending.record.request_id,
                    delivery_token=pending.delivery_token,
                    answer="different",
                    data=[],
                    sql="SELECT 2",
                )
            expiring = store.create_request(
                caller_id="alice",
                session_id=session.session_id,
                conversation_key=session.conversation_key or "",
                message="expiring",
                callback_token_ttl_seconds=0.01,
            )
            time.sleep(0.03)
            with self.assertRaises(DeliveryTokenError):
                store.submit_answer(
                    request_id=expiring.record.request_id,
                    delivery_token=expiring.delivery_token,
                    answer="too late",
                    data=[],
                )

    def test_callback_can_win_before_trigger_metadata_and_status_cannot_overwrite_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = SessionStore(path)
            fake = CallbackBeforeResponseAgentClient()
            client = TestClient(
                create_app(
                    settings=make_settings(path),
                    store=store,
                    agent_client=fake,
                    mount_mcp=False,
                )
            )
            response = client.post(
                "/chat",
                headers={"Authorization": "Bearer alice-token"},
                json={"message": "Race"},
            )
            request_id = response.json()["request_id"]
            record = store.get_request(request_id)
            self.assertEqual(record.status, "completed")
            self.assertEqual(record.callback_status, "completed")
            self.assertEqual(record.trigger_run_id, "apirun_race")
            self.assertEqual(record.conversation_url, "https://chatgpt.com/c/race")

    def test_restart_persistence_caller_isolation_missing_and_listing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = SessionStore(path)
            session = store.create_session("alice")
            pending = store.create_request(
                caller_id="alice",
                session_id=session.session_id,
                conversation_key=session.conversation_key or "",
                message="question",
                callback_token_ttl_seconds=900,
            )
            store.submit_answer(
                request_id=pending.record.request_id,
                delivery_token=pending.delivery_token,
                answer="persisted answer",
                data=[{"columns": ["x"], "rows": [[1]]}],
                sql="SELECT 1",
            )
            restarted_store = SessionStore(path)
            client = TestClient(
                create_app(
                    settings=make_settings(path),
                    store=restarted_store,
                    agent_client=ManualAgentClient(),
                    mount_mcp=False,
                )
            )
            headers = {"Authorization": "Bearer alice-token"}
            retrieved = client.get(
                f"/chat/requests/{pending.record.request_id}", headers=headers
            )
            self.assertEqual(retrieved.status_code, 200)
            self.assertEqual(retrieved.json()["answer"], "persisted answer")
            self.assertEqual(retrieved.json()["sql"], "SELECT 1")
            listed = client.get(
                f"/chat/sessions/{session.session_id}/requests?limit=1&offset=0",
                headers=headers,
            )
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.json()["items"][0]["request_id"], pending.record.request_id)
            forbidden = client.get(
                f"/chat/requests/{pending.record.request_id}",
                headers={"Authorization": "Bearer bob-token"},
            )
            self.assertEqual(forbidden.status_code, 404)
            self.assertEqual(forbidden.json()["error"]["code"], "request_not_found")
            missing = client.get(
                "/chat/requests/req_missing", headers=headers
            )
            self.assertEqual(missing.status_code, 404)
            self.assertEqual(missing.json()["error"]["code"], "request_not_found")

    def test_failed_and_suspended_run_statuses_are_reported(self) -> None:
        for status, expected_status, expected_code in (
            ("failed", "failed", "dispatch_failed"),
            ("suspended", "suspended", "agent_suspended"),
        ):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.sqlite3"
                fake = ManualAgentClient(status)
                client = TestClient(
                    create_app(
                        settings=make_settings(path),
                        store=SessionStore(path),
                        agent_client=fake,
                        mount_mcp=False,
                    )
                )
                accepted = client.post(
                    "/chat",
                    headers={"Authorization": "Bearer alice-token"},
                    json={"message": "run"},
                )
                request_id = accepted.json()["request_id"]
                response = client.get(
                    f"/chat/requests/{request_id}",
                    headers={"Authorization": "Bearer alice-token"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], expected_status)
                self.assertEqual(response.json()["error"]["code"], expected_code)

    def test_callback_missing_after_completed_run_grace_period(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            fake = ManualAgentClient("completed")
            client = TestClient(
                create_app(
                    settings=make_settings(path, timeout=1, callback_grace=0.01),
                    store=SessionStore(path),
                    agent_client=fake,
                    mount_mcp=False,
                )
            )
            accepted = client.post(
                "/chat",
                headers={"Authorization": "Bearer alice-token"},
                json={"message": "no callback"},
            )
            request_id = accepted.json()["request_id"]
            first = client.get(
                f"/chat/requests/{request_id}",
                headers={"Authorization": "Bearer alice-token"},
            )
            self.assertEqual(first.json()["agent_status"], "completed")
            time.sleep(0.03)
            second = client.get(
                f"/chat/requests/{request_id}",
                headers={"Authorization": "Bearer alice-token"},
            )
            self.assertEqual(second.json()["status"], "callback_missing")
            self.assertEqual(second.json()["error"]["code"], "callback_missing")

    def test_uncertain_trigger_is_not_resent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            settings = make_settings(path)
            settings = AppSettings(
                **{**settings.__dict__, "agent_http_timeout_seconds": 0.01}
            )
            fake = SlowAgentClient()
            client = TestClient(
                create_app(
                    settings=settings,
                    store=SessionStore(path),
                    agent_client=fake,
                    mount_mcp=False,
                )
            )
            response = client.post(
                "/chat",
                headers={"Authorization": "Bearer alice-token"},
                json={"message": "uncertain"},
            )
            self.assertEqual(response.status_code, 504)
            self.assertEqual(len(fake.calls), 1)
            request_id = response.json()["error"]["request_id"]
            self.assertEqual(SessionStore(path).get_request(request_id).status, "trigger_unknown")

    def test_workspace_agent_http_adapter_uses_full_trigger_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            http_client = FakeHTTPClient(
                FakeHTTPResponse(
                    202,
                    {
                        "agent_trigger_run_id": "apirun_123",
                        "conversation_url": "https://chatgpt.com/c/123",
                    },
                )
            )
            adapter = WorkspaceAgentClient(make_settings(path), client=http_client)
            result = asyncio.run(
                adapter.trigger(
                    conversation_key="mmm_session",
                    input_text="input",
                    idempotency_key="idem-123",
                )
            )
            self.assertEqual(result.run_id, "apirun_123")
            self.assertEqual(result.conversation_url, "https://chatgpt.com/c/123")
            request = http_client.calls[0]
            self.assertEqual(
                request["url"],
                "https://api.chatgpt.com/v1/workspace_agents/agtch_test_trigger/trigger",
            )
            self.assertEqual(request["json"]["conversation_key"], "mmm_session")
            self.assertEqual(request["headers"]["Idempotency-Key"], "idem-123")
            self.assertEqual(request["headers"]["Authorization"], "Bearer agent-secret")
            self.assertNotIn("agent-secret", json.dumps(request["json"]))


class DatabaseAndMCPTests(unittest.TestCase):
    def test_read_only_sql_validation_and_shared_execution(self) -> None:
        self.assertEqual(_validate_read_only_sql("SELECT 1;"), "SELECT 1")
        with self.assertRaises(ValueError):
            _validate_read_only_sql("UPDATE mmm.table SET value = 1")

        import core.database_service as database_service

        original_fetch = database_service.fetch_all
        try:
            database_service.fetch_all = lambda _sql, _params=None: [{"value": 1}]
            payload = execute_read_only_query_result("SELECT 1 AS value")
            self.assertEqual(payload["rows"], [{"value": 1}])
            self.assertTrue(payload["verified_by_postgresql_execution"])
        finally:
            database_service.fetch_all = original_fetch

    def test_existing_mcp_tools_and_callback_schema_are_registered(self) -> None:
        async def get_tools():
            return await mcp.list_tools()

        tools = {item.name: item for item in asyncio.run(get_tools())}
        self.assertIn("list_database_tables", tools)
        self.assertIn("inspect_table_schema", tools)
        self.assertIn("validate_sql_query", tools)
        self.assertIn("execute_read_only_query", tools)
        self.assertIn("submit_answer", tools)
        self.assertFalse(tools["submit_answer"].annotations.read_only_hint)
        self.assertIn("sql", tools["submit_answer"].parameters["properties"])

    def test_sqlite_migration_adds_callback_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE requests (request_id TEXT PRIMARY KEY, caller_id TEXT NOT NULL, session_id TEXT NOT NULL, conversation_key TEXT NOT NULL, message TEXT NOT NULL, delivery_token_hash TEXT NOT NULL, delivery_token_expires_at REAL NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL, trigger_run_id TEXT, agent_status TEXT, answer TEXT, data_json TEXT NOT NULL DEFAULT '[]', result_fingerprint TEXT, error_code TEXT, error_message TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT)"
                )
                connection.execute(
                    "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, caller_id TEXT NOT NULL, conversation_key TEXT NOT NULL UNIQUE, model_response_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
            SessionStore(path)
            with sqlite3.connect(path) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(requests)")
                }
            self.assertTrue({"callback_status", "callback_deadline_at", "sql_text"} <= columns)

    def test_streamable_http_mcp_mount_remains_operational(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with TestClient(
                create_app(
                    settings=make_settings(path),
                    store=SessionStore(path),
                    agent_client=ManualAgentClient(),
                    mount_mcp=True,
                )
            ) as client:
                response = client.get("/mcp")
                self.assertNotEqual(response.status_code, 404)

    def test_terminal_prints_actual_answer_and_tables_without_sql(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "sessions.json"
            accepted = {
                "request_id": "req_terminal",
                "session_id": "sess_terminal",
                "status": "queued",
            }
            completed = {
                **accepted,
                "status": "completed",
                "answer": "Actual answer",
                "sql": "SELECT 1 AS value",
                "data": [{"columns": ["value"], "rows": [[1]]}],
            }
            output = StringIO()
            inputs = iter(["question", "/quit"])
            with (
                patch.object(agent_chat.ChatAPIClient, "chat", return_value=accepted),
                patch.object(
                    agent_chat.ChatAPIClient,
                    "wait_for_request",
                    return_value=completed,
                ),
                patch("builtins.input", side_effect=lambda _prompt: next(inputs)),
                patch.dict(os.environ, {"AGENT_CHAT_STATE_PATH": str(state_path)}),
                patch("sys.argv", ["agent_chat.py", "--token", "client-token"]),
                redirect_stdout(output),
            ):
                self.assertEqual(agent_chat.run(), 0)
            rendered = output.getvalue()
            self.assertIn("Actual answer", rendered)
            self.assertNotIn("SELECT 1 AS value", rendered)
            self.assertIn("value", rendered)
            self.assertIn("1", rendered)
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["requests"][0]["sql"], "SELECT 1 AS value")

    def test_markdown_table_in_answer_is_not_rendered_again(self) -> None:
        answer = "Summary\n\n| value |\n|---:|\n| 1 |"
        self.assertTrue(agent_chat.answer_contains_markdown_table(answer))
        self.assertFalse(agent_chat.answer_contains_markdown_table("Summary"))

    def test_terminal_callback_failure_is_saved_without_later_result_suggestion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "sessions.json"
            accepted = {
                "request_id": "req_callback_missing",
                "session_id": "sess_callback_missing",
                "status": "queued",
            }
            terminal = {
                **accepted,
                "status": "callback_missing",
                "callback_status": "missing",
                "error": {
                    "code": "callback_missing",
                    "message": "The agent completed without delivering submit_answer.",
                },
                "data": [],
            }
            output = StringIO()
            errors = StringIO()
            inputs = iter(["question", "/quit"])

            def raise_terminal(*_args, **_kwargs):
                raise agent_chat.APIClientError(
                    terminal["error"]["message"],
                    status_code=502,
                    payload=terminal,
                )

            with (
                patch.object(agent_chat.ChatAPIClient, "chat", return_value=accepted),
                patch.object(
                    agent_chat.ChatAPIClient,
                    "wait_for_request",
                    side_effect=raise_terminal,
                ),
                patch("builtins.input", side_effect=lambda _prompt: next(inputs)),
                patch.dict(os.environ, {"AGENT_CHAT_STATE_PATH": str(state_path)}),
                patch("sys.argv", ["agent_chat.py", "--token", "client-token"]),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                self.assertEqual(agent_chat.run(), 0)

            rendered = output.getvalue() + errors.getvalue()
            self.assertIn("Terminal request status: callback_missing", rendered)
            self.assertIn("req_callback_missing", rendered)
            self.assertNotIn("Retrieve it later", rendered)
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["requests"][0]["status"], "callback_missing")
            self.assertEqual(saved["requests"][0]["error"]["code"], "callback_missing")


if __name__ == "__main__":
    unittest.main()
