import pytest

from signplus.telegram import SavedMessagesProbe


class FakeSavedMessagesClient:
    def __init__(self) -> None:
        self.messages: dict[int, str] = {}
        self.next_id = 1

    async def send_message(self, chat_id: str, text: str):
        assert chat_id == "me"
        message = type("Message", (), {"id": self.next_id, "text": text})()
        self.messages[self.next_id] = text
        self.next_id += 1
        return message

    async def get_messages(self, chat_id: str, message_id: int):
        assert chat_id == "me"
        text = self.messages.get(message_id)
        return type("Message", (), {"id": message_id, "text": text})()

    async def delete_messages(self, chat_id: str, message_id: int) -> None:
        assert chat_id == "me"
        self.messages.pop(message_id, None)


@pytest.mark.asyncio
async def test_saved_messages_probe_roundtrips_and_cleans_up() -> None:
    client = FakeSavedMessagesClient()

    result = await SavedMessagesProbe(client).run("TGSP_TEST:run-123")

    assert result.sent_message_id == 1
    assert result.read_back is True
    assert result.deleted is True
    assert client.messages == {}
