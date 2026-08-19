from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from random import Random
from typing import Protocol

from .checkin import CheckInEngine, RunCommand, RunResult
from .notifier import FailureNotifier, NullFailureNotifier
from .store import SignPlusStore, StoredSchedule


@dataclass(frozen=True)
class DailySchedule:
    kind: str
    at: str | None = None
    start: str | None = None
    end: str | None = None

    @classmethod
    def fixed(cls, at: str) -> DailySchedule:
        _parse_time(at)
        return cls(kind="fixed", at=at)

    @classmethod
    def window(cls, start: str, end: str) -> DailySchedule:
        _parse_time(start)
        _parse_time(end)
        return cls(kind="window", start=start, end=end)


@dataclass(frozen=True)
class DispatchResult:
    dispatched: int
    succeeded: int
    failed: int


class RandomSource(Protocol):
    def random(self) -> float: ...


class RunCoordinator:
    def __init__(
        self,
        store: SignPlusStore,
        engine_for_account: Callable[[str], CheckInEngine],
        random_source: RandomSource | None = None,
        failure_notifier: FailureNotifier | None = None,
        now_source: Callable[[], datetime] | None = None,
        retry_delay: timedelta = timedelta(minutes=5),
        max_attempts: int = 3,
    ) -> None:
        if retry_delay.total_seconds() < 0:
            raise ValueError("retry_delay must not be negative")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        self._store = store
        self._engine_for_account = engine_for_account
        self._random = random_source or Random()
        self._notifier = failure_notifier or NullFailureNotifier()
        self._now_source = now_source
        self._retry_delay = retry_delay
        self._max_attempts = max_attempts

    async def tick(self, now: datetime) -> DispatchResult:
        if now.tzinfo is None:
            raise ValueError("tick requires a timezone-aware datetime")
        dispatched = succeeded = failed = 0
        for assignment in self._store.list_enabled_assignments():
            occurrence_date, scheduled_for = _scheduled_occurrence(
                assignment.schedule, now, self._random
            )
            self._store.expire_retries_before(
                assignment.task_id,
                assignment.account_name,
                occurrence_date.isoformat(),
                now,
            )
            self._store.ensure_scheduled_run(
                assignment.task_id,
                assignment.account_name,
                occurrence_date.isoformat(),
                scheduled_for,
            )
            run = self._store.get_due_automatic_run(
                assignment.task_id,
                assignment.account_name,
                occurrence_date.isoformat(),
                now,
            )
            if run is None:
                continue
            if not self._store.claim_run(run.id, now):
                continue
            dispatched += 1
            result = await self._engine_for_account(assignment.account_name).execute(
                RunCommand(
                    run_id=run.id,
                    account_name=assignment.account_name,
                    task=assignment.definition,
                )
            )
            finished_at = self._finished_at(now)
            retry_at = (
                finished_at + self._retry_delay
                if self._is_retryable(result)
                else None
            )
            retry_scheduled = self._store.finish_run(
                run.id,
                result,
                finished_at,
                retry_at=retry_at,
                max_attempts=self._max_attempts,
            )
            if result.code == "ACCOUNT_UNAUTHORIZED":
                self._store.set_account_status(assignment.account_name, "reauth_required")
            if result.success:
                succeeded += 1
            else:
                failed += 1
                if not retry_scheduled:
                    await self._notify_failure(
                        task_name=assignment.definition.name,
                        account_name=assignment.account_name,
                        result=result,
                    )
        return DispatchResult(dispatched, succeeded, failed)

    async def run_now(self, task_id: str, account_name: str, now: datetime) -> object:
        assignment = self._store.get_assignment(task_id, account_name)
        if assignment is None:
            raise KeyError("TASK_ASSIGNMENT_NOT_FOUND")
        run = self._store.create_manual_run(task_id, account_name, now)
        if not self._store.claim_run(run.id, now):
            raise RuntimeError("RUN_NOT_CLAIMED")
        result = await self._engine_for_account(account_name).execute(
            RunCommand(run.id, account_name, assignment.definition)
        )
        self._store.finish_run(run.id, result, self._finished_at(now))
        if result.code == "ACCOUNT_UNAUTHORIZED":
            self._store.set_account_status(account_name, "reauth_required")
        if not result.success:
            await self._notify_failure(
                task_name=assignment.definition.name,
                account_name=account_name,
                result=result,
            )
        return self._store.get_run(run.id)

    def _finished_at(self, started_at: datetime) -> datetime:
        if self._now_source is not None:
            value = self._now_source()
            if value.tzinfo is None:
                raise ValueError("now_source must return a timezone-aware datetime")
            return value
        return datetime.now(started_at.tzinfo)

    def recover_interrupted(self, now: datetime) -> int:
        if now.tzinfo is None:
            raise ValueError("recovery requires a timezone-aware datetime")
        return self._store.interrupt_running(
            now,
            retry_at=now + self._retry_delay,
            max_attempts=self._max_attempts,
        )

    @staticmethod
    def _is_retryable(result: RunResult) -> bool:
        return not result.success and result.code not in {
            "ACCOUNT_UNAUTHORIZED",
            "FAILURE_CONFIRMED",
        }

    async def _notify_failure(
        self, *, task_name: str, account_name: str, result: RunResult
    ) -> None:
        try:
            await self._notifier.notify_failure(
                task_name=task_name,
                account_name=account_name,
                result=result,
            )
        except Exception:
            logging.getLogger(__name__).exception("failure notification failed")


def _scheduled_occurrence(
    schedule: StoredSchedule, now: datetime, random_source: RandomSource
) -> tuple[date, datetime]:
    if schedule.kind == "fixed" and schedule.at:
        value = _parse_time(schedule.at)
        return now.date(), datetime.combine(now.date(), value, tzinfo=now.tzinfo)
    if schedule.kind == "window" and schedule.start and schedule.end:
        start_time = _parse_time(schedule.start)
        end_time = _parse_time(schedule.end)
        occurrence_date = now.date()
        if end_time <= start_time and now.timetz().replace(tzinfo=None) < end_time:
            occurrence_date -= timedelta(days=1)
        start = datetime.combine(occurrence_date, start_time, tzinfo=now.tzinfo)
        end = datetime.combine(occurrence_date, end_time, tzinfo=now.tzinfo)
        if end <= start:
            end += timedelta(days=1)
        offset = (end - start).total_seconds() * random_source.random()
        return occurrence_date, start + timedelta(seconds=offset)
    raise ValueError(f"unsupported schedule: {schedule.kind}")


def _parse_time(value: str) -> time:
    return time.fromisoformat(value)
