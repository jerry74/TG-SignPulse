from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .checkin import Step, StepKind, TaskDefinition
from .crypto import SessionCipher
from .scheduler import DailySchedule
from .store import SignPlusStore


@dataclass(frozen=True)
class ImportReport:
    accounts_ready: int
    tasks_ready: int
    accounts_imported: int
    tasks_imported: int
    unsupported: tuple[str, ...]


class LegacyImporter:
    def __init__(
        self,
        *,
        source: Path,
        store: SignPlusStore,
        cipher: SessionCipher,
        success_patterns: tuple[str, ...],
        failure_patterns: tuple[str, ...],
    ) -> None:
        if not success_patterns:
            raise ValueError("at least one success pattern is required")
        self._source = source
        self._store = store
        self._cipher = cipher
        self._success = success_patterns
        self._failure = failure_patterns

    def run(self, *, apply: bool) -> ImportReport:
        sessions = sorted((self._source / "sessions").glob("*.session_string"))
        configs = sorted((self._source / ".signer" / "signs").glob("*/*/config.json"))
        prepared: list[tuple[str, str, TaskDefinition, DailySchedule]] = []
        unsupported: list[str] = []
        for path in configs:
            account_name = path.parent.parent.name
            task_name = path.parent.name
            try:
                definition, schedule = self._convert_task(path, task_name)
                prepared.append((self._task_id(account_name, task_name), account_name, definition, schedule))
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                unsupported.append(f"{account_name}/{task_name}:{exc}")

        accounts_imported = tasks_imported = 0
        if apply:
            for path in sessions:
                name = path.name.removesuffix(".session_string")
                if self._store.account_exists(name):
                    continue
                session_string = path.read_text(encoding="utf-8").strip()
                self._store.add_account(name, self._cipher.encrypt(session_string))
                accounts_imported += 1
            for task_id, account_name, definition, schedule in prepared:
                if not self._store.account_exists(account_name):
                    unsupported.append(f"{account_name}/{definition.name}:SESSION_MISSING")
                    continue
                existed = self._store.task_exists(task_id)
                self._store.create_task(
                    task_id=task_id,
                    definition=definition,
                    schedule=schedule,
                    account_names=(account_name,),
                    enabled=False,
                )
                if not existed:
                    tasks_imported += 1

        return ImportReport(
            accounts_ready=len(sessions),
            tasks_ready=len(prepared),
            accounts_imported=accounts_imported,
            tasks_imported=tasks_imported,
            unsupported=tuple(unsupported),
        )

    def _convert_task(
        self, path: Path, task_name: str
    ) -> tuple[TaskDefinition, DailySchedule]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        chats = payload.get("chats") or []
        if len(chats) != 1:
            raise ValueError("TASK_MUST_HAVE_ONE_CHAT")
        chat = chats[0]
        chat_id = int(chat["chat_id"])
        chat_username = self._chat_username(
            path.parent.parent / "chats_cache.json", chat_id
        )
        steps = tuple(self._convert_action(action) for action in chat.get("actions") or [])
        if not steps:
            raise ValueError("TASK_HAS_NO_STEPS")
        if payload.get("execution_mode") == "range":
            schedule = DailySchedule.window(payload["range_start"], payload["range_end"])
        else:
            schedule = DailySchedule.fixed(payload["sign_at"])
        return (
            TaskDefinition(
                name=task_name,
                chat_id=chat_id,
                chat_username=chat_username,
                thread_id=chat.get("message_thread_id"),
                steps=steps,
                success_patterns=self._success,
                failure_patterns=self._failure,
            ),
            schedule,
        )

    @staticmethod
    def _chat_username(cache_path: Path, chat_id: int) -> str | None:
        if not cache_path.is_file():
            return None
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(cache, list):
            return None
        matches = [
            str(item.get("username") or "").strip()
            for item in cache
            if isinstance(item, dict) and int(item.get("id", 0)) == chat_id
        ]
        usernames = [value for value in matches if value]
        return usernames[0] if len(usernames) == 1 else None

    @staticmethod
    def _convert_action(action: dict[str, Any]) -> Step:
        action_id = int(action["action"])
        if action_id == 1:
            return Step(StepKind.SEND_TEXT, str(action["text"]))
        if action_id == 2:
            return Step(StepKind.SEND_DICE, str(action.get("dice") or "🎲"))
        if action_id == 3:
            return Step(StepKind.CLICK_BUTTON, str(action["text"]), match_mode="exact")
        if action_id == 4:
            return Step(StepKind.SOLVE_CAPTION_ARITHMETIC)
        raise ValueError(f"ACTION_{action_id}_UNSUPPORTED")

    @staticmethod
    def _task_id(account_name: str, task_name: str) -> str:
        digest = hashlib.sha256(f"{account_name}\0{task_name}".encode()).hexdigest()[:20]
        return f"legacy-{digest}"
