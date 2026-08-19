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
async def test_run_records_the_actual_completion_time(tmp_path: Path) -> None:
    store = SignPlusStore(tmp_path / "completion-time.sqlite")
    store.migrate()
    store.add_account("primary", "encrypted-placeholder")
    store.create_task(
        "task-completion-time",
        TaskDefinition(
            "completion-time",
            1,
            (Step(StepKind.SEND_TEXT, "/checkin"),),
            ("success",),
            ("failed",),
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
                    "emit": [{"id": 1, "text": "success"}],
                }
            ],
        }
    )
    started = datetime(2026, 8, 19, 16, 37, 30, tzinfo=ZoneInfo("Asia/Taipei"))
    finished = started + timedelta(seconds=31)
    coordinator = RunCoordinator(
        store,
        lambda _: CheckInEngine(telegram),
        now_source=lambda: finished,
    )

    run = await coordinator.run_now("task-completion-time", "primary", started)

    assert run.started_at == started.isoformat()
    assert run.finished_at == finished.isoformat()


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
async def test_cross_midnight_retry_keeps_the_window_start_occurrence_date(
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
    runs = store.list_runs()
    run = runs[0]
    assert result.failed == 1
    assert run.occurrence_date == "2026-08-11"
    assert run.scheduled_for == "2026-08-12T00:00:00+08:00"
    assert runs[1].trigger == "retry"
    assert runs[1].occurrence_date == "2026-08-11"
    assert notifier.calls == []


def test_running_scheduled_work_retries_after_process_restart(tmp_path: Path) -> None:
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
    restarted_at = now + timedelta(minutes=1)
    coordinator = RunCoordinator(
        store,
        lambda _: CheckInEngine(
            ScenarioTelegramAdapter(
                {"version": 1, "initial_messages": [], "interactions": []}
            )
        ),
        retry_delay=timedelta(minutes=5),
        max_attempts=3,
    )

    assert coordinator.recover_interrupted(restarted_at) == 1
    runs = store.list_runs()
    assert (runs[0].state, runs[0].code) == ("interrupted", "PROCESS_INTERRUPTED")
    assert (runs[1].trigger, runs[1].attempt_number, runs[1].scheduled_for) == (
        "retry",
        2,
        "2026-08-11T08:06:00+08:00",
    )


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
async def test_scheduled_retry_does_not_block_a_manual_run(
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
    coordinator = RunCoordinator(
        store,
        lambda _: CheckInEngine(telegram),
        retry_delay=timedelta(hours=2),
    )
    now = datetime(2026, 8, 11, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))

    first = await coordinator.tick(now)
    second = await coordinator.tick(now + timedelta(hours=1))
    manual = await coordinator.run_now(
        "task-failure", "primary", now + timedelta(hours=2)
    )

    assert (first.failed, second.dispatched) == (1, 0)
    assert manual.trigger == "manual"
    assert manual.state == "succeeded"
    assert sorted(run.trigger for run in store.list_runs()) == [
        "manual",
        "retry",
        "scheduled",
    ]


@pytest.mark.asyncio
async def test_retryable_scheduled_failure_retries_after_delay_and_succeeds(
    tmp_path: Path,
) -> None:
    store = SignPlusStore(tmp_path / "scheduled-retry.sqlite")
    store.migrate()
    store.add_account("primary", "encrypted-placeholder")
    store.create_task(
        "task-retry",
        TaskDefinition(
            "retry",
            1,
            (Step(StepKind.SEND_TEXT, "/checkin"),),
            ("success",),
            ("failed",),
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
                    "emit": [],
                },
                {
                    "operation": {"type": "send_text", "value": "/checkin"},
                    "emit": [{"id": 1, "text": "success"}],
                },
            ],
        }
    )
    tz = ZoneInfo("Asia/Taipei")
    first_at = datetime(2026, 8, 20, 9, 0, tzinfo=tz)
    current = [first_at]
    coordinator = RunCoordinator(
        store,
        lambda _: CheckInEngine(telegram),
        now_source=lambda: current[0],
        retry_delay=timedelta(minutes=5),
        max_attempts=3,
    )

    first = await coordinator.tick(first_at)
    before_retry = await coordinator.tick(first_at + timedelta(minutes=4))
    current[0] = first_at + timedelta(minutes=5)
    restarted = RunCoordinator(
        store,
        lambda _: CheckInEngine(telegram),
        now_source=lambda: current[0],
        retry_delay=timedelta(minutes=5),
        max_attempts=3,
    )
    retry = await restarted.tick(current[0])

    runs = store.list_runs()
    assert (first.failed, before_retry.dispatched, retry.succeeded) == (1, 0, 1)
    assert [(run.trigger, run.state, run.code) for run in runs] == [
        ("scheduled", "failed", "TELEGRAM_TIMEOUT"),
        ("retry", "succeeded", "SUCCESS_CONFIRMED"),
    ]
    assert [run.attempt_number for run in runs] == [1, 2]
    assert runs[1].scheduled_for == "2026-08-20T09:05:00+08:00"
    assert runs[0].events[-1] == {
        "type": "retry_scheduled",
        "attempt_number": 2,
        "scheduled_for": "2026-08-20T09:05:00+08:00",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_code", ["ACCOUNT_UNAUTHORIZED", "FAILURE_CONFIRMED"])
async def test_terminal_scheduled_failure_is_not_retried(
    tmp_path: Path, terminal_code: str
) -> None:
    store = SignPlusStore(tmp_path / f"terminal-{terminal_code}.sqlite")
    store.migrate()
    store.add_account("primary", "encrypted-placeholder")
    store.create_task(
        "task-terminal",
        TaskDefinition("terminal", 1, (), ("success",), ("failed",)),
        DailySchedule.fixed("08:00"),
        ("primary",),
        True,
    )

    class TerminalEngine:
        async def execute(self, command: object) -> RunResult:
            del command
            return RunResult(False, terminal_code, terminal_code, ())

    now = datetime(2026, 8, 20, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    result = await RunCoordinator(
        store,
        lambda _: TerminalEngine(),  # type: ignore[arg-type]
        now_source=lambda: now,
    ).tick(now)

    assert result.failed == 1
    assert [(run.trigger, run.code) for run in store.list_runs()] == [
        ("scheduled", terminal_code)
    ]


@pytest.mark.asyncio
async def test_scheduled_retry_stops_after_the_configured_attempt_limit(
    tmp_path: Path,
) -> None:
    store = SignPlusStore(tmp_path / "retry-limit.sqlite")
    store.migrate()
    store.add_account("primary", "encrypted-placeholder")
    store.create_task(
        "task-limit",
        TaskDefinition("limit", 1, (), ("success",), ("failed",)),
        DailySchedule.fixed("08:00"),
        ("primary",),
        True,
    )

    class TimeoutEngine:
        async def execute(self, command: object) -> RunResult:
            del command
            return RunResult(False, "TELEGRAM_TIMEOUT", "timeout", ())

    class Notifier:
        def __init__(self) -> None:
            self.calls = 0

        async def notify_failure(self, **kwargs: object) -> None:
            del kwargs
            self.calls += 1

    tz = ZoneInfo("Asia/Taipei")
    current = [datetime(2026, 8, 20, 9, 0, tzinfo=tz)]
    notifier = Notifier()
    coordinator = RunCoordinator(
        store,
        lambda _: TimeoutEngine(),  # type: ignore[arg-type]
        failure_notifier=notifier,
        now_source=lambda: current[0],
        retry_delay=timedelta(minutes=5),
        max_attempts=3,
    )

    results = []
    for minutes in (0, 5, 10, 15):
        current[0] = datetime(2026, 8, 20, 9, minutes, tzinfo=tz)
        results.append(await coordinator.tick(current[0]))

    runs = store.list_runs()
    assert [result.dispatched for result in results] == [1, 1, 1, 0]
    assert [run.attempt_number for run in runs] == [1, 2, 3]
    assert all(run.state == "failed" for run in runs)
    assert notifier.calls == 1


@pytest.mark.asyncio
async def test_manual_failure_never_creates_an_automatic_retry(tmp_path: Path) -> None:
    store = SignPlusStore(tmp_path / "manual-no-retry.sqlite")
    store.migrate()
    store.add_account("primary", "encrypted-placeholder")
    store.create_task(
        "task-manual",
        TaskDefinition(
            "manual",
            1,
            (Step(StepKind.SEND_TEXT, "/checkin"),),
            ("success",),
            ("failed",),
        ),
        DailySchedule.fixed("23:00"),
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
                    "emit": [],
                }
            ],
        }
    )
    now = datetime(2026, 8, 20, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))

    run = await RunCoordinator(store, lambda _: CheckInEngine(telegram)).run_now(
        "task-manual", "primary", now
    )

    assert run.code == "TELEGRAM_TIMEOUT"
    assert [(item.trigger, item.attempt_number) for item in store.list_runs()] == [
        ("manual", 1)
    ]


@pytest.mark.asyncio
async def test_retry_does_not_cross_into_the_next_occurrence_date(tmp_path: Path) -> None:
    store = SignPlusStore(tmp_path / "retry-expiry.sqlite")
    store.migrate()
    store.add_account("primary", "encrypted-placeholder")
    store.create_task(
        "task-expiry",
        TaskDefinition("expiry", 1, (), ("success",), ("failed",)),
        DailySchedule.fixed("23:59"),
        ("primary",),
        True,
    )

    class TimeoutEngine:
        async def execute(self, command: object) -> RunResult:
            del command
            return RunResult(False, "TELEGRAM_TIMEOUT", "timeout", ())

    tz = ZoneInfo("Asia/Taipei")
    first = datetime(2026, 8, 20, 23, 59, tzinfo=tz)
    current = [first]
    coordinator = RunCoordinator(
        store,
        lambda _: TimeoutEngine(),  # type: ignore[arg-type]
        now_source=lambda: current[0],
        retry_delay=timedelta(minutes=5),
    )
    await coordinator.tick(first)
    current[0] = datetime(2026, 8, 21, 0, 5, tzinfo=tz)

    after_midnight = await coordinator.tick(current[0])

    expired = next(run for run in store.list_runs() if run.trigger == "retry")
    assert after_midnight.dispatched == 0
    assert (expired.state, expired.code) == ("expired", "RETRY_EXPIRED")


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
