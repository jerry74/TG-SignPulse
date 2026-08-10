from pathlib import Path

from fastapi.testclient import TestClient

from signplus.app import create_app
from signplus.checkin import CheckInEngine
from signplus.scenario import ScenarioTelegramAdapter


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
    app = create_app(
        data_dir=tmp_path,
        master_key="test-master-key-that-is-long-enough-123456",
        bootstrap_username="admin",
        bootstrap_password="correct-horse-battery-staple",
        engine_for_account=lambda _: CheckInEngine(telegram),
        initial_accounts={"primary": "test-placeholder"},
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
        assert client.get("/api/v1/runs", headers=headers).json()[0]["state"] == "succeeded"
