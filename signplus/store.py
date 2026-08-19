from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .checkin import RunResult, Step, StepKind, TaskDefinition


@dataclass(frozen=True)
class StoredSchedule:
    kind: str
    at: str | None = None
    start: str | None = None
    end: str | None = None


@dataclass(frozen=True)
class TaskAssignment:
    task_id: str
    account_name: str
    definition: TaskDefinition
    schedule: StoredSchedule


@dataclass(frozen=True)
class StoredRun:
    id: str
    task_id: str
    account_name: str
    occurrence_date: str | None
    scheduled_for: str
    trigger: str
    state: str
    code: str | None
    message: str | None
    events: tuple[dict[str, object], ...]
    started_at: str | None
    finished_at: str | None
    parent_run_id: str | None
    attempt_number: int


class SignPlusStore:
    """SQLite implementation behind the storage seam."""

    def __init__(self, path: Path):
        self.path = path

    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    name TEXT PRIMARY KEY,
                    encrypted_session TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    chat_id INTEGER NOT NULL,
                    chat_username TEXT,
                    thread_id INTEGER,
                    steps_json TEXT NOT NULL,
                    success_json TEXT NOT NULL,
                    failure_json TEXT NOT NULL,
                    schedule_kind TEXT NOT NULL,
                    schedule_at TEXT,
                    schedule_start TEXT,
                    schedule_end TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS task_accounts (
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    account_name TEXT NOT NULL REFERENCES accounts(name) ON DELETE CASCADE,
                    PRIMARY KEY (task_id, account_name)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    account_name TEXT NOT NULL REFERENCES accounts(name) ON DELETE CASCADE,
                    occurrence_date TEXT,
                    scheduled_for TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    state TEXT NOT NULL,
                    code TEXT,
                    message TEXT,
                    events_json TEXT NOT NULL DEFAULT '[]',
                    started_at TEXT,
                    finished_at TEXT,
                    parent_run_id TEXT,
                    attempt_number INTEGER NOT NULL DEFAULT 1
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_scheduled_occurrence
                ON runs(task_id, account_name, occurrence_date)
                WHERE trigger = 'scheduled';
                CREATE INDEX IF NOT EXISTS ix_runs_state_time
                ON runs(state, scheduled_for);
                CREATE TABLE IF NOT EXISTS run_events (
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                PRAGMA user_version = 2;
                """
            )
            columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "chat_username" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN chat_username TEXT")
            run_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "parent_run_id" not in run_columns:
                db.execute("ALTER TABLE runs ADD COLUMN parent_run_id TEXT")
            if "attempt_number" not in run_columns:
                db.execute(
                    "ALTER TABLE runs ADD COLUMN attempt_number INTEGER NOT NULL DEFAULT 1"
                )
            db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS uq_retry_attempt
                   ON runs(parent_run_id, attempt_number)
                   WHERE trigger='retry'"""
            )
            db.execute("PRAGMA user_version = 4")

    def add_account(self, name: str, encrypted_session: str, status: str = "active") -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO accounts(name, encrypted_session, status)
                   VALUES (?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     encrypted_session=excluded.encrypted_session,
                     status=excluded.status""",
                (name, encrypted_session, status),
            )

    def list_accounts(self) -> list[dict[str, str]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT name, status, created_at FROM accounts ORDER BY name"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_encrypted_session(self, name: str) -> str:
        with self._connect() as db:
            row = db.execute(
                "SELECT encrypted_session FROM accounts WHERE name=?", (name,)
            ).fetchone()
        if row is None:
            raise KeyError(name)
        return str(row["encrypted_session"])

    def account_exists(self, name: str) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT 1 FROM accounts WHERE name=?", (name,)).fetchone()
        return row is not None

    def set_account_status(self, name: str, status: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE accounts SET status=? WHERE name=?", (status, name))

    def delete_account(self, name: str) -> bool:
        with self._connect() as db:
            result = db.execute("DELETE FROM accounts WHERE name=?", (name,))
        return result.rowcount == 1

    def task_exists(self, task_id: str) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone()
        return row is not None

    def ensure_user(self, username: str, password_hash: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO users(username, password_hash) VALUES (?, ?)",
                (username, password_hash),
            )

    def get_password_hash(self, username: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT password_hash FROM users WHERE username=?", (username,)
            ).fetchone()
        return str(row["password_hash"]) if row else None

    def create_task(
        self,
        task_id: str,
        definition: TaskDefinition,
        schedule: Any,
        account_names: tuple[str, ...],
        enabled: bool,
    ) -> None:
        schedule_kind = str(schedule.kind)
        schedule_at = getattr(schedule, "at", None)
        schedule_start = getattr(schedule, "start", None)
        schedule_end = getattr(schedule, "end", None)
        steps = [
            {
                **asdict(step),
                "kind": step.kind.value,
            }
            for step in definition.steps
        ]
        with self._connect() as db:
            db.execute(
                """INSERT INTO tasks(
                       id, name, chat_id, chat_username, thread_id, steps_json,
                       success_json, failure_json, schedule_kind,
                       schedule_at, schedule_start, schedule_end, enabled
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       name=excluded.name, chat_id=excluded.chat_id,
                       chat_username=excluded.chat_username,
                       thread_id=excluded.thread_id, steps_json=excluded.steps_json,
                       success_json=excluded.success_json,
                       failure_json=excluded.failure_json,
                       schedule_kind=excluded.schedule_kind,
                       schedule_at=excluded.schedule_at,
                       schedule_start=excluded.schedule_start,
                       schedule_end=excluded.schedule_end,
                       enabled=excluded.enabled""",
                (
                    task_id,
                    definition.name,
                    definition.chat_id,
                    definition.chat_username,
                    definition.thread_id,
                    json.dumps(steps, ensure_ascii=False),
                    json.dumps(definition.success_patterns, ensure_ascii=False),
                    json.dumps(definition.failure_patterns, ensure_ascii=False),
                    schedule_kind,
                    schedule_at,
                    schedule_start,
                    schedule_end,
                    int(enabled),
                ),
            )
            db.execute("DELETE FROM task_accounts WHERE task_id = ?", (task_id,))
            db.executemany(
                "INSERT INTO task_accounts(task_id, account_name) VALUES (?, ?)",
                ((task_id, account_name) for account_name in account_names),
            )

    def list_enabled_assignments(self) -> list[TaskAssignment]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT t.*, ta.account_name
                   FROM tasks t JOIN task_accounts ta ON ta.task_id=t.id
                   WHERE t.enabled=1 ORDER BY t.id, ta.account_name"""
            ).fetchall()
        return [self._assignment(row) for row in rows]

    def get_assignment(self, task_id: str, account_name: str) -> TaskAssignment | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT t.*, ta.account_name
                   FROM tasks t JOIN task_accounts ta ON ta.task_id=t.id
                   WHERE t.id=? AND ta.account_name=?""",
                (task_id, account_name),
            ).fetchone()
        return self._assignment(row) if row else None

    def list_tasks(self) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT t.*, group_concat(ta.account_name) AS account_names
                   FROM tasks t LEFT JOIN task_accounts ta ON ta.task_id=t.id
                   GROUP BY t.id ORDER BY t.name"""
            ).fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "chat_id": row["chat_id"],
                "chat_username": row["chat_username"],
                "thread_id": row["thread_id"],
                "steps": json.loads(row["steps_json"]),
                "success_patterns": json.loads(row["success_json"]),
                "failure_patterns": json.loads(row["failure_json"]),
                "schedule": {
                    "kind": row["schedule_kind"],
                    "at": row["schedule_at"],
                    "start": row["schedule_start"],
                    "end": row["schedule_end"],
                },
                "account_names": str(row["account_names"] or "").split(","),
                "enabled": bool(row["enabled"]),
            }
            for row in rows
        ]

    def delete_task(self, task_id: str) -> bool:
        with self._connect() as db:
            result = db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        return result.rowcount == 1

    def ensure_scheduled_run(
        self,
        task_id: str,
        account_name: str,
        occurrence_date: str,
        scheduled_for: datetime,
    ) -> StoredRun:
        run_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO runs(
                       id, task_id, account_name, occurrence_date,
                       scheduled_for, trigger, state
                   ) VALUES (?, ?, ?, ?, ?, 'scheduled', 'pending')""",
                (run_id, task_id, account_name, occurrence_date, scheduled_for.isoformat()),
            )
            row = db.execute(
                """SELECT * FROM runs
                   WHERE task_id=? AND account_name=? AND occurrence_date=?
                     AND trigger='scheduled'""",
                (task_id, account_name, occurrence_date),
            ).fetchone()
        return self._run(row)

    def get_due_automatic_run(
        self,
        task_id: str,
        account_name: str,
        occurrence_date: str,
        now: datetime,
    ) -> StoredRun | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT * FROM runs
                   WHERE task_id=? AND account_name=? AND occurrence_date=?
                     AND trigger IN ('scheduled', 'retry')
                     AND state='pending' AND scheduled_for<=?
                   ORDER BY scheduled_for, attempt_number, id LIMIT 1""",
                (task_id, account_name, occurrence_date, now.isoformat()),
            ).fetchone()
        return self._run(row) if row else None

    def expire_retries_before(
        self,
        task_id: str,
        account_name: str,
        occurrence_date: str,
        now: datetime,
    ) -> int:
        with self._connect() as db:
            result = db.execute(
                """UPDATE runs
                   SET state='expired', code='RETRY_EXPIRED',
                       message='retry expired at occurrence boundary', finished_at=?
                   WHERE task_id=? AND account_name=? AND trigger='retry'
                     AND state='pending' AND occurrence_date<?""",
                (now.isoformat(), task_id, account_name, occurrence_date),
            )
        return result.rowcount

    def create_manual_run(
        self, task_id: str, account_name: str, scheduled_for: datetime
    ) -> StoredRun:
        run_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            busy = db.execute(
                "SELECT 1 FROM runs WHERE account_name=? AND state='running'",
                (account_name,),
            ).fetchone()
            if busy is not None:
                raise RuntimeError("ACCOUNT_BUSY")
            db.execute(
                """INSERT INTO runs(
                       id, task_id, account_name, occurrence_date,
                       scheduled_for, trigger, state
                   ) VALUES (?, ?, ?, NULL, ?, 'manual', 'pending')""",
                (run_id, task_id, account_name, scheduled_for.isoformat()),
            )
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._run(row)

    def get_run(self, run_id: str) -> StoredRun:
        with self._connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            events = db.execute(
                "SELECT event_json FROM run_events WHERE run_id=? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        return self._run(
            row,
            tuple(json.loads(event["event_json"]) for event in events),
        )

    def claim_run(self, run_id: str, now: datetime) -> bool:
        with self._connect() as db:
            result = db.execute(
                """UPDATE runs SET state='running', started_at=?
                   WHERE id=? AND state='pending'""",
                (now.isoformat(), run_id),
            )
        return result.rowcount == 1

    def finish_run(
        self,
        run_id: str,
        result: RunResult,
        now: datetime,
        *,
        retry_at: datetime | None = None,
        max_attempts: int = 1,
    ) -> bool:
        events = list(result.events)
        retry_scheduled = False
        with self._connect() as db:
            if retry_at is not None:
                source = db.execute(
                    "SELECT * FROM runs WHERE id=?", (run_id,)
                ).fetchone()
                if source is not None:
                    next_attempt = int(source["attempt_number"]) + 1
                    if next_attempt <= max_attempts:
                        root_id = str(source["parent_run_id"] or source["id"])
                        inserted = db.execute(
                            """INSERT OR IGNORE INTO runs(
                                   id, task_id, account_name, occurrence_date,
                                   scheduled_for, trigger, state,
                                   parent_run_id, attempt_number
                               ) VALUES (?, ?, ?, ?, ?, 'retry', 'pending', ?, ?)""",
                            (
                                str(uuid.uuid4()),
                                source["task_id"],
                                source["account_name"],
                                source["occurrence_date"],
                                retry_at.isoformat(),
                                root_id,
                                next_attempt,
                            ),
                        )
                        retry_scheduled = inserted.rowcount == 1
                        if retry_scheduled:
                            events.append(
                                {
                                    "type": "retry_scheduled",
                                    "attempt_number": next_attempt,
                                    "scheduled_for": retry_at.isoformat(),
                                }
                            )
            db.execute(
                """UPDATE runs SET state=?, code=?, message=?, events_json=?, finished_at=?
                   WHERE id=?""",
                (
                    "succeeded" if result.success else "failed",
                    result.code,
                    result.message,
                    json.dumps(events, ensure_ascii=False),
                    now.isoformat(),
                    run_id,
                ),
            )
            db.execute("DELETE FROM run_events WHERE run_id=?", (run_id,))
            db.executemany(
                "INSERT INTO run_events(run_id, sequence, event_json) VALUES (?, ?, ?)",
                (
                    (run_id, sequence, json.dumps(event, ensure_ascii=False))
                    for sequence, event in enumerate(events, start=1)
                ),
            )
        return retry_scheduled

    def interrupt_running(
        self,
        now: datetime,
        *,
        retry_at: datetime | None = None,
        max_attempts: int = 1,
    ) -> int:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM runs WHERE state='running'").fetchall()
            for row in rows:
                events: list[dict[str, object]] = [
                    {"type": "process_interrupted"}
                ]
                if (
                    retry_at is not None
                    and row["trigger"] in {"scheduled", "retry"}
                    and int(row["attempt_number"]) < max_attempts
                ):
                    next_attempt = int(row["attempt_number"]) + 1
                    root_id = str(row["parent_run_id"] or row["id"])
                    inserted = db.execute(
                        """INSERT OR IGNORE INTO runs(
                               id, task_id, account_name, occurrence_date,
                               scheduled_for, trigger, state,
                               parent_run_id, attempt_number
                           ) VALUES (?, ?, ?, ?, ?, 'retry', 'pending', ?, ?)""",
                        (
                            str(uuid.uuid4()),
                            row["task_id"],
                            row["account_name"],
                            row["occurrence_date"],
                            retry_at.isoformat(),
                            root_id,
                            next_attempt,
                        ),
                    )
                    if inserted.rowcount == 1:
                        events.append(
                            {
                                "type": "retry_scheduled",
                                "attempt_number": next_attempt,
                                "scheduled_for": retry_at.isoformat(),
                            }
                        )
                db.execute(
                    """UPDATE runs SET state='interrupted', code='PROCESS_INTERRUPTED',
                       message='process interrupted', events_json=?, finished_at=?
                       WHERE id=?""",
                    (json.dumps(events), now.isoformat(), row["id"]),
                )
                db.execute("DELETE FROM run_events WHERE run_id=?", (row["id"],))
                db.executemany(
                    "INSERT INTO run_events(run_id, sequence, event_json) VALUES (?, ?, ?)",
                    (
                        (row["id"], index, json.dumps(event))
                        for index, event in enumerate(events, start=1)
                    ),
                )
        return len(rows)

    def list_runs(self) -> list[StoredRun]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM runs ORDER BY scheduled_for, id").fetchall()
            events = db.execute(
                "SELECT run_id, event_json FROM run_events ORDER BY run_id, sequence"
            ).fetchall()
        by_run: dict[str, list[dict[str, object]]] = {}
        for event in events:
            by_run.setdefault(str(event["run_id"]), []).append(
                json.loads(event["event_json"])
            )
        return [self._run(row, tuple(by_run.get(str(row["id"]), []))) for row in rows]

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=30000")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _assignment(row: sqlite3.Row) -> TaskAssignment:
        steps = tuple(
            Step(
                kind=StepKind(item["kind"]),
                value=item.get("value", ""),
                match_mode=item.get("match_mode", "exact"),
                timeout_seconds=float(item.get("timeout_seconds", 30)),
            )
            for item in json.loads(row["steps_json"])
        )
        return TaskAssignment(
            task_id=row["id"],
            account_name=row["account_name"],
            definition=TaskDefinition(
                name=row["name"],
                chat_id=int(row["chat_id"]),
                thread_id=row["thread_id"],
                steps=steps,
                success_patterns=tuple(json.loads(row["success_json"])),
                failure_patterns=tuple(json.loads(row["failure_json"])),
                chat_username=row["chat_username"],
            ),
            schedule=StoredSchedule(
                kind=row["schedule_kind"],
                at=row["schedule_at"],
                start=row["schedule_start"],
                end=row["schedule_end"],
            ),
        )

    @staticmethod
    def _run(
        row: sqlite3.Row, events: tuple[dict[str, object], ...] = ()
    ) -> StoredRun:
        return StoredRun(
            id=row["id"],
            task_id=row["task_id"],
            account_name=row["account_name"],
            occurrence_date=row["occurrence_date"],
            scheduled_for=row["scheduled_for"],
            trigger=row["trigger"],
            state=row["state"],
            code=row["code"],
            message=row["message"],
            events=events,
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            parent_run_id=row["parent_run_id"],
            attempt_number=int(row["attempt_number"]),
        )
