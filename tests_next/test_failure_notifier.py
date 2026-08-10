import pytest

from signplus.checkin import RunResult
from signplus.notifier import TelegramBotFailureNotifier


class Response:
    def raise_for_status(self) -> None:
        return None


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def post(self, url: str, *, json: dict[str, object], timeout: int) -> Response:
        assert timeout == 10
        self.calls.append((url, json))
        return Response()


@pytest.mark.asyncio
async def test_failure_notifier_uses_adapter_without_exposing_token_in_body() -> None:
    client = FakeClient()
    notifier = TelegramBotFailureNotifier(
        token="test-notification-token", chat_id="123", client=client
    )
    await notifier.notify_failure(
        task_name="daily",
        account_name="primary",
        result=RunResult(False, "FAILED", "failed", ()),
    )
    url, payload = client.calls[0]
    assert url.endswith("test-notification-token/sendMessage")
    assert "test-notification-token" not in str(payload)
    assert payload["chat_id"] == "123"
