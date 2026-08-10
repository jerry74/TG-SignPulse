#!/usr/bin/env python3
"""Stream a NAS migration archive directly to the Docker VM using SFTP."""

from __future__ import annotations

import os
import shlex
from io import StringIO

import paramiko


def main() -> None:
    source = os.environ["NAS_ARCHIVE_PATH"]
    target = os.environ["VM_ARCHIVE_PATH"]
    if not source.startswith("/volume2/docker/tg-signpulse/backups/"):
        raise ValueError("NAS archive path is outside the approved backup directory")
    nas = paramiko.SSHClient()
    vm = paramiko.SSHClient()
    nas.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    vm.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        nas.connect(
            hostname="192.168.31.50",
            username="jerry74",
            password=os.environ["NAS_PASSWORD"],
            look_for_keys=False,
            allow_agent=False,
        )
        key = paramiko.Ed25519Key.from_private_key(
            StringIO(os.environ["INFRA__PVE__SSH_PRIVATE_KEY"])
        )
        vm.connect(
            hostname="192.168.31.240",
            username="dockeradmin",
            pkey=key,
            look_for_keys=False,
            allow_agent=False,
        )
        vm_sftp = vm.open_sftp()
        try:
            stdin, stdout, stderr = nas.exec_command(
                f"sudo -S -p '' cat {shlex.quote(source)}", timeout=300
            )
            stdin.write(os.environ["NAS_PASSWORD"] + "\n")
            stdin.flush()
            stdin.close()
            with vm_sftp.file(target, "wb") as writer:
                while chunk := stdout.read(1024 * 1024):
                    writer.write(chunk)
            code = stdout.channel.recv_exit_status()
            if code:
                raise RuntimeError(stderr.read().decode("utf-8", errors="replace"))
            vm_sftp.chmod(target, 0o600)
        finally:
            vm_sftp.close()
    finally:
        nas.close()
        vm.close()


if __name__ == "__main__":
    main()
