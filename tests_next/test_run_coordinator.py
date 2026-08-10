import asyncio
from datetime import date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from signplus.checkin import (
    CheckInEngine,
    RunResult,
    Step,
    StepKind,
    TaskDefinition,
)
from signplus.scenario import ScenarioTelegramAdapter
from signplus.scheduler import DailySchedule, RunCoordinator
from signplus.store import SignPlusStore


class FixedRandom:
    def __init__(self, value: float):
        self.value = value

    def random(self) -> float:
        return self.value


@pytest.mark.asyncio
async def test_due_task_runs_once_and_survives_coordinator_restart(tmp_path: Path) -> None:
    store = SignPlusStore(tmp_path / "signplus.sqlite")
    store.migrate()
    store.add_account("primary", encrypted_session="encrypted-placeholder")
    store.create_task(
        task_id="task-1",
        definition=TaskDefinition(
            name="daily",
            chat_id=10001,
            steps=(Step(kind=StepKind.SEND_TEXT, value="/checkin"),),
            success_patterns=("success",),
            failure_patterns=("failed",),
        ),
        schedule=DailySchedule.fixed("08:00"),
        account_names=("primary",),
        enabled=True,
    )
    telegram = ScenarioTelegramAdapter(
        {
            "version": 1,
            "initial_messages": [],
            "interactions": [
                {
                    "operation": {"type": "send_text", "value": "/checkin"},
                    "emit": [{"id": 1, "text": "success", "buttons": []}],
                }
            ],
        }
    )
    now = datetime(2026, 8, 11, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))

    first = await RunCoordinator(
        store,
        engine_for_account=lambda _: CheckInEngine(telegram),
    ).tick(now)
    second = await RunCoordinator(
        store,
        engine_for_account=lambda _: CheckInEngine(telegram),
    ).tick(now)

    assert first.dispatched == 1
    assert first.succeeded == 1
    assert second.dispatched == 0
    assert [(run.state, run.code) for run in store.list_runs()] == [
        ("succeeded", "SUCCESS_CONFIRMED")
    ]


@pytest.mark.asyncio
async def test_random_window_time_is_persisted_across_restart(tmp_path: Path) -> None:
    store = SignPlusStore(tmp_path / "window.sqlite")
    store.migrate()
    store.add_account("primary", "encrypted-placeholder")
    store.create_task(
        task_id="task-window",
        definition=TaskDefinition(
            name="window",
            chat_id=10001,
            steps=(Step(kind=StepKind.SEND_TEXT, value="/checkin"),),
            success_patterns=("success",),
            failure_patterns=("failed",),
        ),
        schedule=DailySchedule.window("08:00", "10:00"),
        account_names=("primary",),
        enabled=True,
    )
    telegram = ScenarioTelegramAdapter(
        {
            "version": 1,
            "initial_messages": [],
            "interactions": [
                {
                    "operation": {"type": "send_text", "value": "/checkin"},
                    "emit": [{"id": 1, "text": "success", "buttons": []}],
                }
            ],
        }
    )
    tz = ZoneInfo("Asia/Taipei")

    before = await RunCoordinator(
        store,
        engine_for_account=lambda _: CheckInEngine(telegram),
        random_source=FixedRandom(0.5),
    ).tick(datetime(2026, 8, 11, 7, 0, tzinfo=tz))
    persisted_time = store.list_runs()[0].scheduled_for
    after = await RunCoordinator(
        store,
        engine_for_account=lambda _: CheckInEngine(telegram),
        random_source=FixedRandom(0.9),
    ).tick(datetime(2026, 8, 11, 9, 1, tzinfo=tz))

    assert before.dispatched == 0
    assert persisted_time == "2026-08-11T09:00:00+08:00"
    assert after.succeeded == 1
    assert store.list_runs()[0].scheduled_for == persisted_time


@pytest.mark.asyncio
async def test_cross_midnight_window_uses_window_start_date_and_notifies_failure(
    tmp_path: Path,
) -> None:
    store = SignPlusStore(tmp_path / "midnight.sqlite")
    store.migrate()
    store.add_account("primary", "encrypted-placeholder")
    store.create_task(
        "task-midnight",
        TaskDefinition(
            name="midnight", chat_id=1,
            steps=(Step(StepKind.SEND_TEXT, "/checkin"),),
            success_patterns=("success",), failure_patterns=("failed",),
        ),
        DailySchedule.window("23:00", "01:00"),
        ("primary",), True,
    )
    telegram = ScenarioTelegramAdapter(
        {"version": 1, "initial_messages": [], "interactions": [{
            "operation": {"type": "send_text", "value": "/checkin"},
            "emit": [{"id": 1, "text": "ambiguous", "buttons": []}],
        }]}
    )

    class Notifier:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def notify_failure(self, *, task_name: str, account_name: str, result: object) -> None:
            self.calls.append(f"{task_name}:{account_name}")

    notifier = Notifier()
    result = await RunCoordinator(
        store, lambda _: CheckInEngine(telegram), FixedRandom(0.5), notifier
    ).tick(datetime(2026, 8, 12, 0, 30, tzinfo=ZoneInfo("Asia/Taipei")))
    run = store.list_runs()[0]
    assert result.failed == 1
    assert run.occurrence_date == "2026-08-11"
    assert run.scheduled_for == "2026-08-12T00:00:00+08:00"
    assert notifier.calls == ["midnight:primary"]


def test_running_work_is_terminally_interrupted_after_process_restart(tmp_path: Path) -> None:
    store = SignPlusStore(tmp_path / "crash.sqlite")
    store.migrate()
    store.add_account("primary", "encrypted-placeholder")
    store.create_task(
        "task-crash",
        TaskDefinition("crash", 1, (Step(StepKind.SEND_TEXT, "/x"),), ("ok",), ("no",)),
        DailySchedule.fixed("08:00"), ("primary",), True,
    )
    now = datetime(2026, 8, 11, 8, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    run = store.ensure_scheduled_run("task-crash", "primary", "2026-08-11", now)
    assert store.claim_run(run.id, now)
    assert store.interrupt_running(now) == 1
    assert store.list_runs()[0].state == "interrupted"


@given(
    day=st.dates(min_value=date(2024, 1, 1), max_value=date(2032, 12, 31)),
    random_value=st.floats(min_value=0, max_value=0.999999, allow_nan=False),
)
@settings(max_examples=25, deadline=None)
def test_cross_midnight_random_time_invariant(day: date, random_value: float) -> None:
    with TemporaryDirectory() as directory:
        store = SignPlusStore(Path(directory) / "property.sqlite")
        store.migrate()
        store.add_account("primary", "encrypted-placeholder")
        store.create_task(
            "property-task",
            TaskDefinition(
                "property", 1, (Step(StepKind.SEND_TEXT, "/x"),), ("ok",), ("no",)
            ),
            DailySchedule.window("23:00", "01:00"),
            ("primary",),
            True,
        )
        coordinator = RunCoordinator(
            store,
            lambda _: CheckInEngine(ScenarioTelegramAdapter(
                {"version": 1, "initial_messages": [], "interactions": []}
            )),
            FixedRandom(random_value),
        )
        now = datetime.combine(day, datetime.min.time(), ZoneInfo("Asia/Taipei")).replace(
            hour=22
        )
        asyncio.run(coordinator.tick(now))
        run = store.list_runs()[0]
        scheduled = datetime.fromisoformat(run.scheduled_for)
        start = now.replace(hour=23)
        assert run.occurrence_date == day.isoformat()
        assert start <= scheduled < start + timedelta(hours=2)


@pytest.mark.asyncio
async def test_final_failure_is_not_retried_but_manual_run_remains_available(
    tmp_path: Path,
) -> None:
    store = SignPlusStore(tmp_path / "failure.sqlite")
    store.migrate()
    store.add_account("primary", "encrypted-placeholder")
    store.create_task(
        "task-failure",
        TaskDefinition(
            "failure", 1, (Step(StepKind.SEND_TEXT, "/checkin"),),
            ("success",), ("failed",),
        ),
        DailySchedule.fixed("08:00"),
        ("primary",),
        True,
    )
    telegram = ScenarioTelegramAdapter(
        {
            "version": 1,
            "initial_messages": [],
            "interactions": [
                {
                    "operation": {"type": "send_text", "value": "/checkin"},
                    "emit": [{"id": 1, "text": "ambiguous"}],
                },
                {
                    "operation": {"type": "send_text", "value": "/checkin"},
                    "emit": [{"id": 2, "text": "success"}],
                },
            ],
        }
    )
    coordinator = RunCoordinator(store, lambda _: CheckInEngine(telegram))
    now = datetime(2026, 8, 11, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))

    first = await coordinator.tick(now)
    second = await coordinator.tick(now + timedelta(hours=1))
    manual = await coordinator.run_now(
        "task-failure", "primary", now + timedelta(hours=2)
    )

    assert (first.failed, second.dispatched) == (1, 0)
    assert manual.trigger == "manual"
    assert manual.state == "succeeded"
    assert [run.trigger for run in store.list_runs()] == ["scheduled", "manual"]


@pytest.mark.asyncio
async def test_unauthorized_run_marks_account_for_reauthentication(
    tmp_path: Path,
) -> None:
    store = SignPlusStore(tmp_path / "unauthorized.sqlite")
    store.migrate()
    store.add_account("primary", "encrypted-placeholder")
    store.create_task(
        "task-unauthorized",
        TaskDefinition(
            "unauthorized", 1, (Step(StepKind.SEND_TEXT, "/checkin"),),
            ("success",), ("failed",),
        ),
        DailySchedule.fixed("08:00"),
        ("primary",),
        True,
    )
    telegram = ScenarioTelegramAdapter(
        {
            "version": 1,
            "initial_messages": [],
            "interactions": [{
                "operation": {"type": "send_text", "value": "/checkin"},
                "fault": {"type": "unauthorized"},
            }],
        }
    )

    result = await RunCoordinator(
        store, lambda _: CheckInEngine(telegram)
    ).tick(datetime(2026, 8, 11, 9, 0, tzinfo=ZoneInfo("Asia/Taipei")))

    assert result.failed == 1
    assert store.list_accounts()[0]["status"] == "reauth_required"
    assert store.list_runs()[0].code == "ACCOUNT_UNAUTHORIZED"


@pytest.mark.asyncio
async def test_global_default_dispatch_is_sequential_across_accounts(
    tmp_path: Path,
) -> None:
    store = SignPlusStore(tmp_path / "sequential.sqlite")
    store.migrate()
    for account in ("first", "second"):
        store.add_account(account, "encrypted-placeholder")
    store.create_task(
        "task-sequential",
        TaskDefinition(
            "sequential", 1, (Step(StepKind.SEND_TEXT, "/checkin"),),
            ("success",), ("failed",),
        ),
        DailySchedule.fixed("08:00"),
        ("first", "second"),
        True,
    )

    class TrackingEngine:
        active = 0
        maximum = 0

        async def execute(self, command: object) -> RunResult:
            del command
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            await asyncio.sleep(0)
            self.active -= 1
            return RunResult(True, "SUCCESS_CONFIRMED", "ok", ())

    engine = TrackingEngine()
    result = await RunCoordinator(store, lambda _: engine).tick(
        datetime(2026, 8, 11, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    )

    assert result.succeeded == 2
    assert engine.maximum == 1


@given(day=st.dates(min_value=date(2024, 1, 1), max_value=date(2032, 12, 31)))
@settings(max_examples=25, deadline=None)
def test_fixed_schedule_stays_on_the_local_calendar_date(day: date) -> None:
    with TemporaryDirectory() as directory:
        store = SignPlusStore(Path(directory) / "fixed-property.sqlite")
        store.migrate()
        store.add_account("primary", "encrypted-placeholder")
        store.create_task(
            "fixed-property",
            TaskDefinition(
                "fixed", 1, (Step(StepKind.SEND_TEXT, "/x"),), ("ok",), ("no",)
            ),
            DailySchedule.fixed("23:59"),
            ("primary",),
            True,
        )
        now = datetime.combine(
            day, time(hour=0, minute=1), ZoneInfo("Asia/Taipei")
        )
        asyncio.run(
            RunCoordinator(
                store,
                lambda _: CheckInEngine(
                    ScenarioTelegramAdapter(
                        {"version": 1, "initial_messages": [], "interactions": []}
                    )
                ),
            ).tick(now)
        )
        run = store.list_runs()[0]
        assert run.occurrence_date == day.isoformat()
        assert datetime.fromisoformat(run.scheduled_for).date() == day
