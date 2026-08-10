from __future__ import annotations

from typing import Any, Protocol

import httpx

from .checkin import RunResult


class FailureNotifier(Protocol):
    async def notify_failure(
        self, *, task_name: str, account_name: str, result: RunResult
    ) -> None: ...


class NullFailureNotifier:
    """Default production policy: notifications remain explicitly disabled."""

    async def notify_failure(
        self, *, task_name: str, account_name: str, result: RunResult
    ) -> None:
        return None


class TelegramBotFailureNotifier:
    """Optional failure-only notifier; secrets never enter the message body."""

    def __init__(
        self, *, token: str, chat_id: str, client: Any | None = None
    ) -> None:
        if not token or not chat_id:
            raise ValueError("notification token and chat id are required")
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._client = client

    async def notify_failure(
        self, *, task_name: str, account_name: str, result: RunResult
    ) -> None:
        payload = {
            "chat_id": self._chat_id,
            "text": (
                f"TG-SignPlus failure\nTask: {task_name}\n"
                f"Account: {account_name}\nCode: {result.code}"
            ),
        }
        if self._client is not None:
            response = await self._client.post(self._url, json=payload, timeout=10)
        else:
            async with httpx.AsyncClient() as client:
                response = await client.post(self._url, json=payload, timeout=10)
        response.raise_for_status()
