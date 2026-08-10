from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from signplus.app import create_app
from signplus.auth import TokenManager
from signplus.checkin import CheckInEngine
from signplus.scenario import ScenarioTelegramAdapter
from signplus.telegram import KurigramTaskPreflight


def test_admin_can_create_and_run_a_task_through_http(tmp_path: Path) -> None:
    telegram = ScenarioTelegramAdapter(
        {
            "version": 1,
            "initial_messages": [],
            "interactions": [
                {
                    "operation": {"type": "send_text", "value": "/checkin"},
                    "emit": [{"id": 1, "text": "check-in ok", "buttons": []}],
                }
            ],
        }
    )
    preflight_calls: list[str] = []

    async def preflight(account_name: str, task: object) -> dict[str, object]:
        preflight_calls.append(account_name)
        return {"session_authorized": True, "chat_accessible": True}

    app = create_app(
        data_dir=tmp_path,
        master_key="test-master-key-that-is-long-enough-123456",
        bootstrap_username="admin",
        bootstrap_password="correct-horse-battery-staple",
        engine_for_account=lambda _: CheckInEngine(telegram),
        initial_accounts={"primary": "test-placeholder"},
        telegram_preflight=preflight,
    )

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "correct-horse-battery-staple"},
        )
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        created = client.post(
            "/api/v1/tasks",
            headers=headers,
            json={
                "id": "task-http",
                "name": "daily",
                "account_names": ["primary"],
                "chat_id": 10001,
                "schedule": {"kind": "fixed", "at": "08:00"},
                "steps": [{"kind": "send_text", "value": "/checkin"}],
                "success_patterns": ["check-in ok"],
                "failure_patterns": ["check-in failed"],
                "enabled": False,
            },
        )
        assert created.status_code == 201

        run = client.post(
            "/api/v1/tasks/task-http/run?account_name=primary",
            headers=headers,
        )
        assert run.status_code == 200
        assert run.json()["state"] == "succeeded"
        assert run.json()["code"] == "SUCCESS_CONFIRMED"
        stored_run = client.get("/api/v1/runs", headers=headers).json()[0]
        assert stored_run["state"] == "succeeded"
        assert [event["type"] for event in stored_run["events"]] == [
            "run_started",
            "text_sent",
        ]
        preflight_response = client.post(
            "/api/v1/tasks/task-http/preflight?account_name=primary", headers=headers
        )
        assert preflight_response.json() == {
            "session_authorized": True,
            "chat_accessible": True,
        }
        assert preflight_calls == ["primary"]

        now = datetime(2026, 8, 11, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        busy_run = app.state.store.create_manual_run("task-http", "primary", now)
        assert app.state.store.claim_run(busy_run.id, now)
        busy = client.post(
            "/api/v1/tasks/task-http/run?account_name=primary", headers=headers
        )
        assert busy.status_code == 409
        assert busy.json()["detail"] == "ACCOUNT_BUSY"

        updated = client.put(
            "/api/v1/tasks/task-http",
            headers=headers,
            json={**created.json(), "enabled": False},
        )
        assert updated.status_code == 200
        assert updated.json()["enabled"] is False
        assert client.delete("/api/v1/tasks/task-http", headers=headers).status_code == 204
        assert client.get("/api/v1/tasks", headers=headers).json() == []


def test_http_auth_and_task_validation_fail_closed(tmp_path: Path) -> None:
    app = create_app(
        data_dir=tmp_path,
        master_key="test-master-key-that-is-long-enough-123456",
        bootstrap_username="admin",
        bootstrap_password="correct-horse-battery-staple",
        engine_for_account=lambda _: CheckInEngine(
            ScenarioTelegramAdapter(
                {"version": 1, "initial_messages": [], "interactions": []}
            )
        ),
        initial_accounts={"primary": "test-placeholder"},
    )

    with TestClient(app) as client:
        assert client.get("/api/v1/tasks").status_code == 401
        assert client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "incorrect-password"},
        ).status_code == 401
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "correct-horse-battery-staple"},
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        invalid = client.post(
            "/api/v1/tasks",
            headers=headers,
            json={
                "id": "invalid-regex",
                "name": "invalid-regex",
                "account_names": ["primary"],
                "chat_id": 1,
                "schedule": {"kind": "fixed", "at": "08:00"},
                "steps": [{"kind": "send_text", "value": "/start"}],
                "success_patterns": ["("],
                "failure_patterns": [],
                "enabled": False,
            },
        )
        assert invalid.status_code == 422
        assert invalid.json()["detail"] == "RULE_REGEX_INVALID"


def test_expired_admin_token_is_rejected() -> None:
    tokens = TokenManager(
        "test-master-key-that-is-long-enough-123456", ttl_seconds=-1
    )
    assert tokens.verify(tokens.issue("admin")) is None


def test_phone_code_and_2fa_are_exposed_through_authenticated_http(
    tmp_path: Path,
) -> None:
    class LoginManager:
        async def start(self, account_name: str, phone_number: str):
            assert (account_name, phone_number) == ("primary", "+886900000000")
            return {"login_id": "login-1", "status": "code_required"}

        async def submit_code(self, login_id: str, code: str):
            if login_id != "login-1":
                raise KeyError(login_id)
            assert code == "12345"
            return {"login_id": login_id, "status": "password_required"}

        async def submit_password(self, login_id: str, password: str):
            assert (login_id, password) == ("login-1", "2fa-secret")
            return {"login_id": login_id, "status": "complete"}

    app = create_app(
        data_dir=tmp_path,
        master_key="test-master-key-that-is-long-enough-123456",
        bootstrap_username="admin",
        bootstrap_password="correct-horse-battery-staple",
        engine_for_account=lambda _: CheckInEngine(
            ScenarioTelegramAdapter(
                {"version": 1, "initial_messages": [], "interactions": []}
            )
        ),
        telegram_login_manager=LoginManager(),
    )
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "correct-horse-battery-staple"},
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        started = client.post(
            "/api/v1/accounts/login/start",
            headers=headers,
            json={"account_name": "primary", "phone_number": "+886900000000"},
        )
        assert started.json()["status"] == "code_required"
        code = client.post(
            "/api/v1/accounts/login/login-1/code",
            headers=headers,
            json={"code": "12345"},
        )
        assert code.json()["status"] == "password_required"
        password = client.post(
            "/api/v1/accounts/login/login-1/password",
            headers=headers,
            json={"password": "2fa-secret"},
        )
        assert password.json()["status"] == "complete"
        assert client.post(
            "/api/v1/accounts/login/missing/code",
            headers=headers,
            json={"code": "12345"},
        ).status_code == 404


def test_preflight_proves_each_button_rule_matches_latest_keyboard_once(
    tmp_path: Path,
) -> None:
    button = type("Button", (), {"text": "✅ 簽到"})()
    markup = type("Markup", (), {"inline_keyboard": [[button]]})()
    message = type(
        "Message",
        (),
        {
            "id": 10,
            "text": "choose",
            "caption": "",
            "reply_markup": markup,
            "message_thread_id": None,
        },
    )()

    class Client:
        async def get_me(self):
            return type("Me", (), {"id": 99})()

        async def get_chat(self, target: int | str):
            del target
            return type("Chat", (), {"id": 10001})()

        async def get_chat_history(self, target: int | str, limit: int):
            del target, limit
            yield message

    async def client_for_account(account_name: str):
        assert account_name == "primary"
        return Client()

    app = create_app(
        data_dir=tmp_path,
        master_key="test-master-key-that-is-long-enough-123456",
        bootstrap_username="admin",
        bootstrap_password="correct-horse-battery-staple",
        engine_for_account=lambda _: CheckInEngine(
            ScenarioTelegramAdapter(
                {"version": 1, "initial_messages": [], "interactions": []}
            )
        ),
        initial_accounts={"primary": "test-placeholder"},
        telegram_preflight=KurigramTaskPreflight(client_for_account).inspect,
    )
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "correct-horse-battery-staple"},
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        payload = {
            "id": "preflight-task",
            "name": "preflight-task",
            "account_names": ["primary"],
            "chat_id": 10001,
            "schedule": {"kind": "fixed", "at": "08:00"},
            "steps": [{
                "kind": "click_button",
                "value": "签到",
                "match_mode": "exact",
            }],
            "success_patterns": ["success"],
            "failure_patterns": ["failed"],
            "enabled": False,
        }
        assert client.post(
            "/api/v1/tasks", headers=headers, json=payload
        ).status_code == 201
        exact = client.post(
            "/api/v1/tasks/preflight-task/preflight?account_name=primary",
            headers=headers,
        )
        assert exact.json()["button_match_counts"] == [0]
        assert exact.json()["button_rules_ready"] is False

        payload["steps"][0]["value"] = "签到|簽到"
        payload["steps"][0]["match_mode"] = "regex"
        assert client.put(
            "/api/v1/tasks/preflight-task", headers=headers, json=payload
        ).status_code == 200
        regex = client.post(
            "/api/v1/tasks/preflight-task/preflight?account_name=primary",
            headers=headers,
        )
        assert regex.json()["button_match_counts"] == [1]
        assert regex.json()["button_rules_ready"] is True
