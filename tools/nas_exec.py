#!/usr/bin/env python3
"""Execute a scoped NAS command using a password injected by Bitwarden."""

from __future__ import annotations

import os
import shlex
import sys

import paramiko


def main() -> int:
    command = " ".join(sys.argv[1:])
    if not command:
        raise SystemExit("usage: nas_exec.py <command>")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=os.environ.get("NAS_HOST", "192.168.31.50"),
            port=int(os.environ.get("NAS_PORT", "22")),
            username=os.environ.get("NAS_USER", "jerry74"),
            password=os.environ["NAS_PASSWORD"],
            look_for_keys=False,
            allow_agent=False,
            timeout=15,
        )
        sudo = os.environ.get("NAS_SUDO") == "1"
        remote_command = (
            f"sudo -S -p '' sh -c {shlex.quote(command)}" if sudo else command
        )
        stdin, stdout, stderr = client.exec_command(remote_command, timeout=300)
        if sudo:
            stdin.write(os.environ["NAS_PASSWORD"] + "\n")
            stdin.flush()
        stdin.close()
        output = stdout.read()
        error = stderr.read()
        code = stdout.channel.recv_exit_status()
        if output:
            sys.stdout.buffer.write(output)
        if error:
            sys.stderr.buffer.write(error)
        return code
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
