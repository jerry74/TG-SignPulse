from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from functools import partial
from typing import Protocol, TypeVar

from .challenge import CaptionArithmeticSolver, ChallengeError
from .scenario import (
    TelegramFloodWaitError,
    TelegramMessage,
    TelegramTimeout,
    TelegramTransientError,
    TelegramUnauthorizedError,
)


class StepKind(str, Enum):
    SEND_TEXT = "send_text"
    CLICK_BUTTON = "click_button"
    SEND_DICE = "send_dice"
    SOLVE_CAPTION_ARITHMETIC = "solve_caption_arithmetic"


@dataclass(frozen=True)
class Step:
    kind: StepKind
    value: str = ""
    match_mode: str = "exact"
    timeout_seconds: float = 30.0


@dataclass(frozen=True)
class TaskDefinition:
    name: str
    chat_id: int
    steps: tuple[Step, ...]
    success_patterns: tuple[str, ...]
    failure_patterns: tuple[str, ...]
    thread_id: int | None = None
    chat_username: str | None = None

    @property
    def telegram_target(self) -> int | str:
        return self.chat_username or self.chat_id


@dataclass(frozen=True)
class RunCommand:
    run_id: str
    account_name: str
    task: TaskDefinition


@dataclass(frozen=True)
class RunResult:
    success: bool
    code: str
    message: str
    events: tuple[dict[str, object], ...]


class TelegramPort(Protocol):
    async def latest_message(
        self, chat_id: int | str, thread_id: int | None = None
    ) -> TelegramMessage | None: ...

    async def send_text(self, chat_id: int | str, value: str, thread_id: int | None = None) -> None: ...

    async def send_dice(self, chat_id: int | str, value: str, thread_id: int | None = None) -> None: ...

    async def click_button(self, chat_id: int | str, message_id: int, value: str) -> None: ...

    async def wait_for_message(
        self,
        chat_id: int | str,
        after: TelegramMessage | None,
        timeout_seconds: float,
        thread_id: int | None = None,
    ) -> TelegramMessage: ...


_T = TypeVar("_T")


class Clock(Protocol):
    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class CheckInEngine:
    def __init__(
        self,
        telegram: TelegramPort,
        solver: CaptionArithmeticSolver | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._telegram = telegram
        self._solver = solver or CaptionArithmeticSolver()
        self._clock = clock or SystemClock()

    async def execute(self, command: RunCommand) -> RunResult:
        task = command.task
        target = task.telegram_target
        events: list[dict[str, object]] = [
            {"type": "run_started", "run_id": command.run_id}
        ]

        try:
            cursor = await self._retry_operation(
                partial(self._telegram.latest_message, target, task.thread_id),
                events,
            )
            current: TelegramMessage | None = None
            for step in task.steps:
                if step.kind is StepKind.SEND_TEXT:
                    value = step.value
                    await self._retry_operation(
                        partial(
                            self._telegram.send_text,
                            target,
                            value,
                            task.thread_id,
                        ),
                        events,
                    )
                    events.append({"type": "text_sent"})
                    current = await self._wait(task, target, cursor, step.timeout_seconds)
                elif step.kind is StepKind.SEND_DICE:
                    value = step.value or "🎲"
                    await self._retry_operation(
                        partial(
                            self._telegram.send_dice,
                            target,
                            value,
                            task.thread_id,
                        ),
                        events,
                    )
                    events.append({"type": "dice_sent"})
                    current = await self._wait(task, target, cursor, step.timeout_seconds)
                elif step.kind is StepKind.CLICK_BUTTON:
                    current = current or await self._wait(
                        task, target, cursor, step.timeout_seconds
                    )
                    button = self._find_button(current.buttons, step.value, step.match_mode)
                    if button is None:
                        return self._failure("BUTTON_NOT_FOUND", events)
                    message_id = current.id
                    await self._retry_operation(
                        partial(
                            self._telegram.click_button,
                            target,
                            message_id,
                            button,
                        ),
                        events,
                    )
                    events.append({"type": "button_clicked", "button": button})
                    current = await self._wait(task, target, cursor, step.timeout_seconds)
                elif step.kind is StepKind.SOLVE_CAPTION_ARITHMETIC:
                    current = current or await self._wait(
                        task, target, cursor, step.timeout_seconds
                    )
                    deadline = self._clock.monotonic() + step.timeout_seconds
                    while True:
                        terminal = self._terminal_result(task, current, events)
                        if terminal is not None:
                            return terminal
                        try:
                            answer = self._solver.solve(current.caption, current.buttons)
                            break
                        except ChallengeError as exc:
                            error_code = str(exc)
                            if error_code not in {
                                "CHALLENGE_EXPRESSION_NOT_FOUND",
                                "CHALLENGE_ANSWER_NOT_UNIQUE",
                            }:
                                return self._challenge_failure(
                                    error_code, current, events
                                )
                            remaining = deadline - self._clock.monotonic()
                            if remaining <= 0:
                                return self._challenge_failure(
                                    error_code, current, events
                                )
                            try:
                                current = await self._wait(
                                    task, target, current, remaining
                                )
                            except TelegramTimeout:
                                return self._challenge_failure(
                                    error_code, current, events
                                )
                    message_id = current.id
                    answer_button = answer.button
                    await self._retry_operation(
                        partial(
                            self._telegram.click_button,
                            target,
                            message_id,
                            answer_button,
                        ),
                        events,
                    )
                    events.append(
                        {
                            "type": "challenge_solved",
                            "expression": answer.expression,
                            "value": answer.value,
                            "selected": answer.button,
                            "candidate_count": len(current.buttons),
                            "candidate_summary": self._button_summary(
                                current.buttons
                            ),
                        }
                    )
                    current = await self._wait(
                        task, target, current, step.timeout_seconds
                    )

                if current is not None:
                    cursor = current
                    terminal = self._terminal_result(task, current, events)
                    if terminal is not None:
                        return terminal
        except TelegramTimeout:
            return self._failure("TELEGRAM_TIMEOUT", events)
        except TelegramUnauthorizedError:
            return self._failure("ACCOUNT_UNAUTHORIZED", events)
        except TelegramFloodWaitError:
            return self._failure("FLOOD_WAIT_TOO_LONG", events)
        except TelegramTransientError:
            return self._failure("TELEGRAM_TRANSIENT_FAILURE", events)

        return self._failure("SUCCESS_NOT_CONFIRMED", events)

    async def _retry_operation(
        self,
        operation: Callable[[], Awaitable[_T]],
        events: list[dict[str, object]],
    ) -> _T:
        for attempt in range(1, 4):
            try:
                return await operation()
            except TelegramFloodWaitError as exc:
                if exc.seconds > 120 or attempt == 3:
                    raise
                events.append(
                    {"type": "retry", "reason": "flood_wait", "attempt": attempt}
                )
                await self._clock.sleep(exc.seconds)
            except TelegramTransientError:
                if attempt == 3:
                    raise
                events.append(
                    {"type": "retry", "reason": "transient", "attempt": attempt}
                )
        raise AssertionError("retry loop exhausted")

    async def _wait(
        self,
        task: TaskDefinition,
        target: int | str,
        cursor: TelegramMessage | None,
        timeout_seconds: float,
    ) -> TelegramMessage:
        return await self._telegram.wait_for_message(
            target,
            cursor,
            timeout_seconds,
            task.thread_id,
        )

    @staticmethod
    def _find_button(buttons: tuple[str, ...], pattern: str, mode: str) -> str | None:
        if mode == "exact":
            matches = [button for button in buttons if button == pattern]
        elif mode == "regex":
            matches = [button for button in buttons if re.search(pattern, button)]
        else:
            return None
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _terminal_result(
        task: TaskDefinition,
        message: TelegramMessage,
        events: list[dict[str, object]],
    ) -> RunResult | None:
        content = "\n".join(value for value in (message.text, message.caption) if value)
        if any(re.search(pattern, content) for pattern in task.failure_patterns):
            return CheckInEngine._failure("FAILURE_CONFIRMED", events)
        if any(re.search(pattern, content) for pattern in task.success_patterns):
            return RunResult(True, "SUCCESS_CONFIRMED", "success rule matched", tuple(events))
        return None

    @staticmethod
    def _failure(code: str, events: list[dict[str, object]]) -> RunResult:
        return RunResult(False, code, code.replace("_", " ").lower(), tuple(events))

    @staticmethod
    def _challenge_failure(
        code: str,
        message: TelegramMessage,
        events: list[dict[str, object]],
    ) -> RunResult:
        events.append(
            {
                "type": "challenge_rejected",
                "error_code": code,
                "candidate_count": len(message.buttons),
                "candidate_summary": CheckInEngine._button_summary(
                    message.buttons
                ),
            }
        )
        return CheckInEngine._failure(code, events)

    @staticmethod
    def _button_summary(buttons: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            hashlib.sha256(button.strip().encode("utf-8")).hexdigest()[:10]
            for button in buttons
        )
