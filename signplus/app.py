from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from .auth import TokenManager, hash_password, verify_password
from .checkin import CheckInEngine, Step, StepKind, TaskDefinition
from .notifier import FailureNotifier
from .scheduler import DailySchedule, RunCoordinator
from .store import SignPlusStore


class LoginBody(BaseModel):
    username: str
    password: str


class TelegramLoginStartBody(BaseModel):
    account_name: str
    phone_number: str


class TelegramLoginCodeBody(BaseModel):
    code: str


class TelegramLoginPasswordBody(BaseModel):
    password: str


class ScheduleBody(BaseModel):
    kind: str
    at: str | None = None
    start: str | None = None
    end: str | None = None


class StepBody(BaseModel):
    kind: StepKind
    value: str = ""
    match_mode: str = "exact"
    timeout_seconds: float = Field(30, ge=1, le=180)


class TaskBody(BaseModel):
    id: str
    name: str
    account_names: list[str]
    chat_id: int
    thread_id: int | None = None
    schedule: ScheduleBody
    steps: list[StepBody]
    success_patterns: list[str]
    failure_patterns: list[str]
    enabled: bool = True


def create_app(
    *,
    data_dir: Path,
    master_key: str,
    bootstrap_username: str,
    bootstrap_password: str,
    engine_for_account: Callable[[str], CheckInEngine],
    initial_accounts: dict[str, str] | None = None,
    telegram_login_manager: Any | None = None,
    saved_messages_probe: Callable[[str, str], Awaitable[object]] | None = None,
    failure_notifier: FailureNotifier | None = None,
    telegram_preflight: Callable[
        [str, TaskDefinition], Awaitable[dict[str, object]]
    ]
    | None = None,
) -> FastAPI:
    store = SignPlusStore(data_dir / "signplus.sqlite")
    store.migrate()
    store.ensure_user(bootstrap_username, hash_password(bootstrap_password))
    for name, encrypted_session in (initial_accounts or {}).items():
        store.add_account(name, encrypted_session)
    tokens = TokenManager(master_key)
    coordinator = RunCoordinator(
        store, engine_for_account, failure_notifier=failure_notifier
    )
    app = FastAPI(title="TG-SignPlus", version="3.0.0")
    app.state.ready = True
    app.state.store = store
    app.state.coordinator = coordinator

    def current_user(authorization: str | None = Header(default=None)) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="AUTH_REQUIRED")
        username = tokens.verify(authorization.removeprefix("Bearer ").strip())
        if username is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="TOKEN_INVALID")
        return username

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def ready() -> dict[str, str]:
        if not app.state.ready:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="NOT_READY")
        return {"status": "ready"}

    @app.post("/api/v1/auth/login")
    def login(body: LoginBody) -> dict[str, str]:
        encoded = store.get_password_hash(body.username)
        if encoded is None or not verify_password(body.password, encoded):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="LOGIN_FAILED")
        return {"access_token": tokens.issue(body.username), "token_type": "bearer"}

    @app.get("/api/v1/accounts")
    def accounts(_: str = Depends(current_user)) -> list[dict[str, str]]:
        return store.list_accounts()

    @app.delete(
        "/api/v1/accounts/{account_name}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        response_model=None,
    )
    def delete_account(account_name: str, _: str = Depends(current_user)) -> Response:
        if not store.delete_account(account_name):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="ACCOUNT_NOT_FOUND")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/v1/accounts/login/start")
    async def start_telegram_login(
        body: TelegramLoginStartBody, _: str = Depends(current_user)
    ) -> dict[str, str]:
        if telegram_login_manager is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="LOGIN_DISABLED")
        return await telegram_login_manager.start(body.account_name, body.phone_number)

    @app.post("/api/v1/accounts/login/{login_id}/code")
    async def submit_telegram_code(
        login_id: str,
        body: TelegramLoginCodeBody,
        _: str = Depends(current_user),
    ) -> dict[str, str]:
        if telegram_login_manager is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="LOGIN_DISABLED")
        try:
            return await telegram_login_manager.submit_code(login_id, body.code)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LOGIN_NOT_FOUND") from None

    @app.post("/api/v1/accounts/login/{login_id}/password")
    async def submit_telegram_password(
        login_id: str,
        body: TelegramLoginPasswordBody,
        _: str = Depends(current_user),
    ) -> dict[str, str]:
        if telegram_login_manager is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="LOGIN_DISABLED")
        try:
            return await telegram_login_manager.submit_password(login_id, body.password)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LOGIN_NOT_FOUND") from None

    @app.post("/api/v1/accounts/{account_name}/probe-saved-messages")
    async def probe_saved_messages(
        account_name: str, _: str = Depends(current_user)
    ) -> dict[str, object]:
        if saved_messages_probe is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="PROBE_DISABLED")
        marker = f"TGSP_TEST:{account_name}:{int(datetime.now(UTC).timestamp())}"
        result = await saved_messages_probe(account_name, marker)
        return result.__dict__

    @app.get("/api/v1/tasks")
    def tasks(_: str = Depends(current_user)) -> list[dict[str, object]]:
        return store.list_tasks()

    @app.post("/api/v1/tasks", status_code=status.HTTP_201_CREATED)
    def create_task(body: TaskBody, _: str = Depends(current_user)) -> dict[str, object]:
        if not body.success_patterns:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="SUCCESS_RULE_REQUIRED")
        if any(not store.account_exists(name) for name in body.account_names):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="ACCOUNT_NOT_FOUND")
        try:
            if body.schedule.kind == "fixed":
                schedule = DailySchedule.fixed(body.schedule.at or "")
            elif body.schedule.kind == "window":
                schedule = DailySchedule.window(
                    body.schedule.start or "", body.schedule.end or ""
                )
            else:
                raise ValueError("unsupported schedule")
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, detail="SCHEDULE_INVALID"
            ) from None
        store.create_task(
            task_id=body.id,
            definition=TaskDefinition(
                name=body.name,
                chat_id=body.chat_id,
                thread_id=body.thread_id,
                steps=tuple(
                    Step(
                        kind=step.kind,
                        value=step.value,
                        match_mode=step.match_mode,
                        timeout_seconds=step.timeout_seconds,
                    )
                    for step in body.steps
                ),
                success_patterns=tuple(body.success_patterns),
                failure_patterns=tuple(body.failure_patterns),
            ),
            schedule=schedule,
            account_names=tuple(body.account_names),
            enabled=body.enabled,
        )
        return next(task for task in store.list_tasks() if task["id"] == body.id)

    @app.put("/api/v1/tasks/{task_id}")
    def update_task(
        task_id: str, body: TaskBody, _: str = Depends(current_user)
    ) -> dict[str, object]:
        if task_id != body.id or not store.task_exists(task_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="TASK_NOT_FOUND")
        return create_task(body, _)

    @app.delete(
        "/api/v1/tasks/{task_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        response_model=None,
    )
    def delete_task(task_id: str, _: str = Depends(current_user)) -> Response:
        if not store.delete_task(task_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="TASK_NOT_FOUND")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/v1/tasks/{task_id}/run")
    async def run_task(
        task_id: str,
        account_name: str = Query(...),
        _: str = Depends(current_user),
    ) -> dict[str, object]:
        try:
            run = await coordinator.run_now(
                task_id,
                account_name,
                datetime.now(ZoneInfo("Asia/Taipei")),
            )
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="TASK_NOT_FOUND") from None
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from None
        return run.__dict__

    @app.get("/api/v1/runs")
    def runs(_: str = Depends(current_user)) -> list[dict[str, object]]:
        return [run.__dict__ for run in store.list_runs()]

    @app.post("/api/v1/tasks/{task_id}/preflight")
    async def preflight_task(
        task_id: str,
        account_name: str = Query(...),
        _: str = Depends(current_user),
    ) -> dict[str, object]:
        if telegram_preflight is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail="PREFLIGHT_DISABLED"
            )
        assignment = store.get_assignment(task_id, account_name)
        if assignment is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="TASK_NOT_FOUND")
        return await telegram_preflight(account_name, assignment.definition)

    return app
