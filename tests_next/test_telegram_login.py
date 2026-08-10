from pathlib import Path

import pytest

from signplus.crypto import SessionCipher
from signplus.store import SignPlusStore
from signplus.telegram import KurigramLoginManager


class SessionPasswordNeeded(Exception):
    pass


class FakeLoginClient:
    def __init__(self, **_: object) -> None:
        self.disconnected = False

    async def connect(self) -> None:
        return None

    async def send_code(self, phone: str) -> object:
        assert phone.startswith("+")
        return type("Sent", (), {"phone_code_hash": "hash"})()

    async def sign_in(self, phone: str, code_hash: str, code: str) -> None:
        assert (phone, code_hash, code) == ("+886900000000", "hash", "12345")
        raise SessionPasswordNeeded()

    async def check_password(self, password: str) -> None:
        assert password == "2fa-secret"

    async def export_session_string(self) -> str:
        return "exported-session"

    async def disconnect(self) -> None:
        self.disconnected = True


@pytest.mark.asyncio
async def test_phone_code_and_2fa_login_persists_only_encrypted_session(tmp_path: Path) -> None:
    store = SignPlusStore(tmp_path / "login.sqlite")
    store.migrate()
    cipher = SessionCipher("test-master-key-that-is-long-enough-123456")
    manager = KurigramLoginManager(
        store=store, cipher=cipher, api_id=1, api_hash="hash", workdir=tmp_path,
        client_factory=FakeLoginClient,
    )
    started = await manager.start("primary", "+886900000000")
    code = await manager.submit_code(started["login_id"], "12345")
    assert code["status"] == "password_required"
    completed = await manager.submit_password(started["login_id"], "2fa-secret")
    assert completed["status"] == "complete"
    encrypted = store.get_encrypted_session("primary")
    assert encrypted != "exported-session"
    assert cipher.decrypt(encrypted) == "exported-session"
