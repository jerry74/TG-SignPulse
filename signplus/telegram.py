from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .crypto import SessionCipher
from .scenario import (
    TelegramFloodWaitError,
    TelegramMessage,
    TelegramTimeout,
    TelegramTransientError,
    TelegramUnauthorizedError,
)
from .store import SignPlusStore


@dataclass(frozen=True)
class SavedMessagesProbeResult:
    sent_message_id: int
    read_back: bool
    deleted: bool


class SavedMessagesProbe:
    def __init__(self, client: Any):
        self._client = client

    async def run(self, marker: str) -> SavedMessagesProbeResult:
        if not marker.startswith("TGSP_TEST:"):
            raise ValueError("probe marker must start with TGSP_TEST:")
        sent = await self._client.send_message("me", marker)
        read = await self._client.get_messages("me", sent.id)
        read_back = bool(read and getattr(read, "text", None) == marker)
        if not read_back:
            raise RuntimeError("SAVED_MESSAGES_READBACK_FAILED")
        await self._client.delete_messages("me", sent.id)
        after_delete = await self._client.get_messages("me", sent.id)
        deleted = not bool(after_delete and getattr(after_delete, "text", None))
        if not deleted:
            raise RuntimeError("SAVED_MESSAGES_DELETE_FAILED")
        return SavedMessagesProbeResult(sent.id, read_back, deleted)


class KurigramTelegramAdapter:
    def __init__(self, client: Any, *, poll_interval: float = 1.0):
        self.client = client
        self._poll_interval = poll_interval

    async def latest_message(
        self, chat_id: int | str, thread_id: int | None = None
    ) -> TelegramMessage | None:
        messages = await self._history(chat_id, limit=10)
        for message in messages:
            if self._matches_thread(message, thread_id):
                return self._convert(message)
        return None

    async def send_text(
        self, chat_id: int | str, value: str, thread_id: int | None = None
    ) -> None:
        kwargs = {"message_thread_id": thread_id} if thread_id is not None else {}
        await self._call(self.client.send_message(chat_id, value, **kwargs))

    async def send_dice(
        self, chat_id: int | str, value: str, thread_id: int | None = None
    ) -> None:
        kwargs = {"message_thread_id": thread_id} if thread_id is not None else {}
        await self._call(self.client.send_dice(chat_id, emoji=value, **kwargs))

    async def click_button(self, chat_id: int | str, message_id: int, value: str) -> None:
        message = await self._call(self.client.get_messages(chat_id, message_id))
        markup = getattr(message, "reply_markup", None)
        rows = getattr(markup, "inline_keyboard", None) or getattr(
            markup, "keyboard", None
        ) or []
        positions = [
            (row_index, column_index)
            for row_index, row in enumerate(rows)
            for column_index, button in enumerate(row)
            if str(getattr(button, "text", "")) == value
        ]
        if len(positions) != 1:
            raise TelegramTransientError("button disappeared before click")
        row, column = positions[0]
        await self._call(message.click(column, row))

    async def wait_for_message(
        self,
        chat_id: int | str,
        after: TelegramMessage | None,
        timeout_seconds: float,
        thread_id: int | None = None,
    ) -> TelegramMessage:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            for raw in await self._history(chat_id, limit=15):
                if not self._matches_thread(raw, thread_id):
                    continue
                message = self._convert(raw)
                if after is None or message.id > after.id or (
                    message.id == after.id and message != after
                ):
                    return message
            await asyncio.sleep(self._poll_interval)
        raise TelegramTimeout("Telegram response timeout")

    async def _history(self, chat_id: int | str, limit: int) -> list[Any]:
        try:
            return [message async for message in self.client.get_chat_history(chat_id, limit=limit)]
        except Exception as exc:  # noqa: BLE001 - external SDK exception taxonomy
            self._raise_translated(exc)
        return []

    async def _call(self, awaitable: Any) -> Any:
        try:
            return await awaitable
        except Exception as exc:  # noqa: BLE001 - external SDK exception taxonomy
            self._raise_translated(exc)

    @staticmethod
    def _raise_translated(exc: Exception) -> None:
        name = type(exc).__name__
        if name in {
            "Unauthorized",
            "AuthKeyUnregistered",
            "SessionRevoked",
            "UserDeactivated",
        }:
            raise TelegramUnauthorizedError(name) from exc
        if name == "FloodWait":
            seconds = int(getattr(exc, "value", getattr(exc, "x", 0)) or 0)
            raise TelegramFloodWaitError(seconds) from exc
        raise TelegramTransientError(name) from exc

    @staticmethod
    def _matches_thread(message: Any, thread_id: int | None) -> bool:
        if thread_id is None:
            return True
        return int(getattr(message, "message_thread_id", 0) or 0) == thread_id

    @staticmethod
    def _convert(message: Any) -> TelegramMessage:
        markup = getattr(message, "reply_markup", None)
        rows = getattr(markup, "inline_keyboard", None) or getattr(
            markup, "keyboard", None
        ) or []
        buttons = tuple(
            str(getattr(button, "text", ""))
            for row in rows
            for button in row
            if getattr(button, "text", None)
        )
        return TelegramMessage(
            id=int(message.id),
            text=str(getattr(message, "text", "") or ""),
            caption=str(getattr(message, "caption", "") or ""),
            buttons=buttons,
        )


class KurigramClientPool:
    def __init__(
        self,
        *,
        store: SignPlusStore,
        cipher: SessionCipher,
        api_id: int,
        api_hash: str,
        workdir: Path,
        proxy: dict[str, Any] | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._store = store
        self._cipher = cipher
        self._api_id = api_id
        self._api_hash = api_hash
        self._workdir = workdir
        self._proxy = proxy
        self._client_factory = client_factory
        self._clients: dict[str, Any] = {}

    async def adapter(self, account_name: str) -> KurigramTelegramAdapter:
        client = await self.client(account_name)
        return KurigramTelegramAdapter(client)

    async def client(self, account_name: str) -> Any:
        existing = self._clients.get(account_name)
        if existing is not None:
            if getattr(existing, "is_connected", True) is False:
                try:
                    await existing.start()
                except Exception as exc:  # noqa: BLE001 - SDK exception taxonomy
                    self._clients.pop(account_name, None)
                    KurigramTelegramAdapter._raise_translated(exc)
            return existing
        factory = self._client_factory
        if factory is None:
            from pyrogram import Client

            factory = Client

        session_string = self._cipher.decrypt(
            self._store.get_encrypted_session(account_name)
        )
        client = factory(
            name=f"signplus-{account_name}",
            api_id=self._api_id,
            api_hash=self._api_hash,
            session_string=session_string,
            in_memory=True,
            no_updates=True,
            workdir=str(self._workdir),
            proxy=self._proxy,
        )
        try:
            await client.start()
        except Exception as exc:  # noqa: BLE001 - SDK exception taxonomy
            KurigramTelegramAdapter._raise_translated(exc)
        self._clients[account_name] = client
        return client

    async def close(self) -> None:
        for client in list(self._clients.values()):
            try:
                await client.stop()
            except Exception:
                logging.getLogger(__name__).exception("Telegram client stop failed")
        self._clients.clear()


class LazyAccountTelegramAdapter:
    def __init__(self, pool: KurigramClientPool, account_name: str):
        self._pool = pool
        self._account_name = account_name

    async def _adapter(self) -> KurigramTelegramAdapter:
        return await self._pool.adapter(self._account_name)

    async def latest_message(
        self, chat_id: int | str, thread_id: int | None = None
    ) -> TelegramMessage | None:
        return await (await self._adapter()).latest_message(chat_id, thread_id)

    async def send_text(
        self, chat_id: int | str, value: str, thread_id: int | None = None
    ) -> None:
        return await (await self._adapter()).send_text(chat_id, value, thread_id)

    async def send_dice(
        self, chat_id: int | str, value: str, thread_id: int | None = None
    ) -> None:
        return await (await self._adapter()).send_dice(chat_id, value, thread_id)

    async def click_button(
        self, chat_id: int | str, message_id: int, value: str
    ) -> None:
        return await (await self._adapter()).click_button(chat_id, message_id, value)

    async def wait_for_message(
        self,
        chat_id: int | str,
        after: TelegramMessage | None,
        timeout_seconds: float,
        thread_id: int | None = None,
    ) -> TelegramMessage:
        return await (await self._adapter()).wait_for_message(
            chat_id, after, timeout_seconds, thread_id
        )


class KurigramLoginManager:
    def __init__(
        self,
        *,
        store: SignPlusStore,
        cipher: SessionCipher,
        api_id: int,
        api_hash: str,
        workdir: Path,
        proxy: dict[str, Any] | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._store = store
        self._cipher = cipher
        self._api_id = api_id
        self._api_hash = api_hash
        self._workdir = workdir
        self._proxy = proxy
        self._client_factory = client_factory
        self._pending: dict[str, dict[str, Any]] = {}

    async def start(self, account_name: str, phone_number: str) -> dict[str, str]:
        factory = self._client_factory
        if factory is None:
            from pyrogram import Client

            factory = Client

        client = factory(
            name=f"login-{uuid.uuid4().hex}",
            api_id=self._api_id,
            api_hash=self._api_hash,
            in_memory=True,
            no_updates=True,
            workdir=str(self._workdir),
            proxy=self._proxy,
        )
        await client.connect()
        sent = await client.send_code(phone_number)
        login_id = uuid.uuid4().hex
        self._pending[login_id] = {
            "client": client,
            "account_name": account_name,
            "phone_number": phone_number,
            "phone_code_hash": sent.phone_code_hash,
        }
        return {"login_id": login_id, "status": "code_required"}

    async def submit_code(self, login_id: str, code: str) -> dict[str, str]:
        pending = self._require(login_id)
        client = pending["client"]
        try:
            await client.sign_in(
                pending["phone_number"], pending["phone_code_hash"], code
            )
        except Exception as exc:
            if type(exc).__name__ == "SessionPasswordNeeded":
                return {"login_id": login_id, "status": "password_required"}
            raise
        return await self._persist(login_id)

    async def submit_password(self, login_id: str, password: str) -> dict[str, str]:
        pending = self._require(login_id)
        await pending["client"].check_password(password)
        return await self._persist(login_id)

    async def _persist(self, login_id: str) -> dict[str, str]:
        pending = self._pending.pop(login_id)
        client = pending["client"]
        session_string = await client.export_session_string()
        self._store.add_account(
            pending["account_name"], self._cipher.encrypt(session_string), "active"
        )
        await client.disconnect()
        return {"login_id": login_id, "status": "complete"}

    def _require(self, login_id: str) -> dict[str, Any]:
        pending = self._pending.get(login_id)
        if pending is None:
            raise KeyError("LOGIN_NOT_FOUND")
        return pending
