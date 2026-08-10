from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .app import create_app
from .checkin import CheckInEngine, TaskDefinition
from .crypto import SessionCipher
from .notifier import FailureNotifier, NullFailureNotifier, TelegramBotFailureNotifier
from .store import SignPlusStore
from .telegram import (
    KurigramClientPool,
    KurigramLoginManager,
    KurigramTelegramAdapter,
    LazyAccountTelegramAdapter,
    SavedMessagesProbe,
)


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _proxy() -> dict[str, object] | None:
    raw = os.getenv("TG_PROXY", "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    return {
        "scheme": parsed.scheme,
        "hostname": parsed.hostname,
        "port": parsed.port,
        "username": parsed.username,
        "password": parsed.password,
    }


data_dir = Path(os.getenv("APP_DATA_DIR", "/data"))
data_dir.mkdir(parents=True, exist_ok=True)
master_key = _required("APP_MASTER_KEY")
api_id = int(_required("TG_API_ID"))
api_hash = _required("TG_API_HASH")
cipher = SessionCipher(master_key)
runtime_store = SignPlusStore(data_dir / "signplus.sqlite")
runtime_store.migrate()
pool = KurigramClientPool(
    store=runtime_store,
    cipher=cipher,
    api_id=api_id,
    api_hash=api_hash,
    workdir=data_dir / "runtime",
    proxy=_proxy(),
)
login_manager = KurigramLoginManager(
    store=runtime_store,
    cipher=cipher,
    api_id=api_id,
    api_hash=api_hash,
    workdir=data_dir / "runtime",
    proxy=_proxy(),
)


async def _probe(account_name: str, marker: str):
    try:
        return await SavedMessagesProbe(await pool.client(account_name)).run(marker)
    except KeyError:
        raise HTTPException(404, detail="ACCOUNT_NOT_FOUND") from None


def _engine(account_name: str) -> CheckInEngine:
    return CheckInEngine(LazyAccountTelegramAdapter(pool, account_name))


async def _preflight(
    account_name: str, task: TaskDefinition
) -> dict[str, object]:
    for pattern in (*task.success_patterns, *task.failure_patterns):
        re.compile(pattern)
    for step in task.steps:
        if step.kind.value == "click_button" and step.match_mode == "regex":
            re.compile(step.value)
    client = await pool.client(account_name)
    me = await client.get_me()
    chat = await client.get_chat(task.chat_id)
    latest = await KurigramTelegramAdapter(client).latest_message(
        task.chat_id, task.thread_id
    )
    return {
        "session_authorized": bool(getattr(me, "id", None)),
        "chat_accessible": int(chat.id) == task.chat_id,
        "thread_id": task.thread_id,
        "latest_message_id": latest.id if latest else None,
        "success_rule_count": len(task.success_patterns),
        "failure_rule_count": len(task.failure_patterns),
        "button_rule_count": sum(
            step.kind.value == "click_button" for step in task.steps
        ),
    }


def _failure_notifier() -> FailureNotifier:
    if os.getenv("ENABLE_FAILURE_NOTIFIER", "false").lower() != "true":
        return NullFailureNotifier()
    return TelegramBotFailureNotifier(
        token=_required("NOTIFY_BOT_TOKEN"),
        chat_id=_required("NOTIFY_CHAT_ID"),
    )


app = create_app(
    data_dir=data_dir,
    master_key=master_key,
    bootstrap_username=os.getenv("ADMIN_USERNAME", "admin"),
    bootstrap_password=_required("ADMIN_PASSWORD"),
    engine_for_account=_engine,
    telegram_login_manager=login_manager,
    saved_messages_probe=_probe,
    failure_notifier=_failure_notifier(),
    telegram_preflight=_preflight,
)
store = app.state.store


# The production-only dependencies are attached to the already-created routes.
# Route closures read these values from app state through lightweight proxy objects.
app.state.telegram_pool = pool
app.state.login_manager = login_manager
app.state.ready = False


async def _scheduler_loop() -> None:
    interval = max(5, int(os.getenv("SCHEDULER_POLL_SECONDS", "15")))
    while True:
        try:
            await app.state.coordinator.tick(datetime.now(ZoneInfo("Asia/Taipei")))
            app.state.scheduler_heartbeat = datetime.now(ZoneInfo("Asia/Taipei")).isoformat()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger("signplus.scheduler").exception("scheduler tick failed")
        await asyncio.sleep(interval)


@app.on_event("startup")
async def _startup() -> None:
    (data_dir / "runtime").mkdir(parents=True, exist_ok=True)
    store.interrupt_running(datetime.now(ZoneInfo("Asia/Taipei")))
    app.state.scheduler_task = asyncio.create_task(_scheduler_loop())
    app.state.ready = True


@app.on_event("shutdown")
async def _shutdown() -> None:
    app.state.ready = False
    task = getattr(app.state, "scheduler_task", None)
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await pool.close()


web_dir = Path(os.getenv("APP_WEB_DIR", "/web"))
assets_dir = web_dir / "assets"
if assets_dir.is_dir():
    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")


@app.get("/{path:path}", include_in_schema=False)
async def _spa(path: str):
    candidate = web_dir / path
    if candidate.is_file():
        return FileResponse(candidate)
    index = web_dir / "index.html"
    if index.is_file():
        return FileResponse(index)
    raise HTTPException(404, detail="WEB_NOT_BUILT")
