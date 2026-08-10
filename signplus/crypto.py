from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class SessionCipher:
    def __init__(self, master_key: str):
        if len(master_key) < 32:
            raise ValueError("APP_MASTER_KEY must contain at least 32 characters")
        key = hashlib.sha256(("session:" + master_key).encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def encrypt(self, session_string: str) -> str:
        if not session_string.strip():
            raise ValueError("session string is empty")
        return self._fernet.encrypt(session_string.encode("utf-8")).decode("ascii")

    def decrypt(self, encrypted_session: str) -> str:
        try:
            return self._fernet.decrypt(encrypted_session.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError):
            raise ValueError("SESSION_DECRYPT_FAILED") from None
