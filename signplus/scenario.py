from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ScenarioMismatch(AssertionError):
    pass


class TelegramTimeout(TimeoutError):
    pass


class TelegramTransientError(ConnectionError):
    pass


class TelegramUnauthorizedError(PermissionError):
    pass


class TelegramFloodWaitError(RuntimeError):
    def __init__(self, seconds: int):
        super().__init__(f"flood wait {seconds}s")
        self.seconds = seconds


class VirtualClock:
    def __init__(self) -> None:
        self._seconds = 0.0

    def monotonic(self) -> float:
        return self._seconds

    def advance(self, seconds: float) -> None:
        self._seconds += max(0.0, seconds)

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)


@dataclass(frozen=True)
class TelegramMessage:
    id: int
    text: str = ""
    caption: str = ""
    buttons: tuple[str, ...] = ()
    thread_id: int | None = None
    outgoing: bool = False


class ScenarioTelegramAdapter:
    """Deterministic Telegram adapter used only by tests and the test CLI."""

    def __init__(self, scenario: dict[str, Any]):
        if scenario.get("version") != 1:
            raise ValueError("unsupported scenario version")
        self._reject_secrets(scenario)
        self._initial = tuple(self._message(item) for item in scenario.get("initial_messages", []))
        self._interactions = deque(scenario.get("interactions", []))
        self._pending: deque[tuple[float, TelegramMessage]] = deque()
        self.operations: list[tuple[str, str]] = []
        self.clock = VirtualClock()

    @classmethod
    def from_path(cls, path: Path) -> ScenarioTelegramAdapter:
        return cls(json.loads(path.read_text(encoding="utf-8")))

    async def latest_message(
        self, chat_id: int | str, thread_id: int | None = None
    ) -> TelegramMessage | None:
        del chat_id
        eligible = (
            message
            for message in self._initial
            if not message.outgoing
            and (thread_id is None or message.thread_id == thread_id)
        )
        return max(eligible, key=lambda message: message.id, default=None)

    async def send_text(self, chat_id: int | str, value: str, thread_id: int | None = None) -> None:
        del chat_id, thread_id
        self._consume("send_text", value)

    async def send_dice(self, chat_id: int | str, value: str, thread_id: int | None = None) -> None:
        del chat_id, thread_id
        self._consume("send_dice", value)

    async def click_button(self, chat_id: int | str, message_id: int, value: str) -> None:
        del chat_id, message_id
        self._consume("click_button", value)

    async def wait_for_message(
        self,
        chat_id: int | str,
        after: TelegramMessage | None,
        timeout_seconds: float,
        thread_id: int | None = None,
    ) -> TelegramMessage:
        del chat_id
        while self._pending:
            delay_seconds, message = self._pending.popleft()
            if delay_seconds > timeout_seconds:
                self.clock.advance(timeout_seconds)
                raise TelegramTimeout("scenario message arrived after timeout")
            self.clock.advance(delay_seconds)
            if thread_id is not None and message.thread_id != thread_id:
                continue
            if message.outgoing:
                continue
            if after is None or message.id > after.id or (message.id == after.id and message != after):
                return message
        raise TelegramTimeout("scenario emitted no new message")

    def assert_complete(self) -> None:
        if self._interactions:
            raise ScenarioMismatch(f"{len(self._interactions)} interaction(s) were not consumed")
        if self._pending:
            raise ScenarioMismatch(f"{len(self._pending)} emitted message(s) were not consumed")

    def _consume(self, kind: str, value: str) -> None:
        actual = (kind, value)
        self.operations.append(actual)
        if not self._interactions:
            raise ScenarioMismatch(f"unexpected operation {actual!r}")
        interaction = self._interactions.popleft()
        expected = interaction.get("operation") or {}
        expected_operation = (expected.get("type"), str(expected.get("value", "")))
        if actual != expected_operation:
            raise ScenarioMismatch(f"expected {expected_operation!r}, got {actual!r}")
        fault = interaction.get("fault") or {}
        fault_type = fault.get("type")
        if fault_type in {"transient", "disconnect"}:
            raise TelegramTransientError("scenario transient failure")
        if fault_type == "timeout":
            raise TelegramTimeout("scenario operation timed out")
        if fault_type == "unauthorized":
            raise TelegramUnauthorizedError("scenario unauthorized")
        if fault_type == "flood_wait":
            raise TelegramFloodWaitError(int(fault.get("seconds", 1)))
        if fault_type:
            raise ScenarioMismatch(f"unsupported fault type {fault_type!r}")
        self._pending.extend(
            (float(item.get("delay_seconds", 0)), self._message(item))
            for item in interaction.get("emit", [])
        )

    @staticmethod
    def _message(item: dict[str, Any]) -> TelegramMessage:
        return TelegramMessage(
            id=int(item["id"]),
            text=str(item.get("text") or ""),
            caption=str(item.get("caption") or ""),
            buttons=tuple(str(value) for value in item.get("buttons", [])),
            thread_id=(int(item["thread_id"]) if item.get("thread_id") is not None else None),
            outgoing=bool(item.get("outgoing", False)),
        )

    @staticmethod
    def _reject_secrets(value: object, path: str = "scenario") -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized = str(key).lower()
                if normalized in {"session", "session_string", "token", "api_hash", "phone_number"}:
                    raise ValueError(f"secret-like field is forbidden: {path}.{key}")
                ScenarioTelegramAdapter._reject_secrets(nested, f"{path}.{key}")
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                ScenarioTelegramAdapter._reject_secrets(nested, f"{path}[{index}]")
