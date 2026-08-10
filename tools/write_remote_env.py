#!/usr/bin/env python3
"""Write the TG-SignPlus remote .env without exposing secret values."""

from __future__ import annotations

import base64
import os
from io import StringIO

import paramiko

REMOTE_PATH = "/opt/docker/stacks/tg-signplus/.env"


def main() -> None:
    key = paramiko.Ed25519Key.from_private_key(
        StringIO(os.environ["INFRA__PVE__SSH_PRIVATE_KEY"])
    )
    content = base64.b64decode(os.environ["TGSP_ENV_CONTENT_BASE64"])
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=os.environ.get("SSH_TARGET_HOST", "192.168.31.240"),
            username=os.environ.get("SSH_TARGET_USER", "dockeradmin"),
            pkey=key,
            look_for_keys=False,
            allow_agent=False,
            timeout=15,
        )
        sftp = client.open_sftp()
        try:
            with sftp.file(REMOTE_PATH, "wb") as remote:
                remote.write(content)
            sftp.chmod(REMOTE_PATH, 0o600)
        finally:
            sftp.close()
    finally:
        client.close()


if __name__ == "__main__":
    main()
