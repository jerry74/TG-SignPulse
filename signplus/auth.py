from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time


def hash_password(password: str, salt: bytes | None = None) -> str:
    if len(password) < 12:
        raise ValueError("password must contain at least 12 characters")
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
    )
    return "scrypt$" + base64.urlsafe_b64encode(salt + digest).decode("ascii")


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, payload = encoded.split("$", 1)
        raw = base64.urlsafe_b64decode(payload)
    except (ValueError, TypeError):
        return False
    if algorithm != "scrypt" or len(raw) != 48:
        return False
    salt, expected = raw[:16], raw[16:]
    actual = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
    )
    return hmac.compare_digest(actual, expected)


class TokenManager:
    def __init__(self, master_key: str, ttl_seconds: int = 8 * 60 * 60):
        if len(master_key) < 32:
            raise ValueError("APP_MASTER_KEY must contain at least 32 characters")
        self._key = hashlib.sha256(("token:" + master_key).encode()).digest()
        self._ttl = ttl_seconds

    def issue(self, username: str) -> str:
        payload = json.dumps(
            {"sub": username, "exp": int(time.time()) + self._ttl},
            separators=(",", ":"),
        ).encode()
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = hmac.new(self._key, encoded, hashlib.sha256).digest()
        return (encoded + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()

    def verify(self, token: str) -> str | None:
        try:
            payload_part, signature_part = token.encode().split(b".", 1)
            signature = base64.urlsafe_b64decode(signature_part + b"=" * (-len(signature_part) % 4))
            expected = hmac.new(self._key, payload_part, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                return None
            payload = json.loads(
                base64.urlsafe_b64decode(payload_part + b"=" * (-len(payload_part) % 4))
            )
            if int(payload["exp"]) < int(time.time()):
                return None
            return str(payload["sub"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None
