from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from .crypto import SessionCipher
from .importer import LegacyImporter
from .store import SignPlusStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Import a read-only TG-SignPulse copy")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--database", type=Path, default=Path("/data/signplus.sqlite"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    master_key = os.environ.get("APP_MASTER_KEY", "")
    if not master_key:
        parser.error("APP_MASTER_KEY is required")
    store = SignPlusStore(args.database)
    store.migrate()
    report = LegacyImporter(
        source=args.source,
        store=store,
        cipher=SessionCipher(master_key),
        success_patterns=(r"(?:签到成功|今日已签到|您今日已签到|已经签到|已签到)",),
        failure_patterns=(r"(?:签到失败|操作失败|验证失败|未成功)",),
    ).run(apply=args.apply)
    print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
