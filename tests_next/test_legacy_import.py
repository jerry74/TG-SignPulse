import json
from pathlib import Path

from signplus.crypto import SessionCipher
from signplus.importer import LegacyImporter
from signplus.store import SignPlusStore


def test_legacy_import_is_dry_runnable_encrypted_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    session_dir = source / "sessions"
    task_dir = source / ".signer" / "signs" / "primary" / "daily"
    session_dir.mkdir(parents=True)
    task_dir.mkdir(parents=True)
    (session_dir / "primary.session_string").write_text(
        "sensitive-session-string", encoding="utf-8"
    )
    (task_dir / "config.json").write_text(
        json.dumps(
            {
                "_version": 4,
                "execution_mode": "range",
                "range_start": "08:00",
                "range_end": "19:00",
                "sign_at": "08:00",
                "chats": [
                    {
                        "chat_id": 10001,
                        "name": "fixture-bot",
                        "actions": [
                            {"action": 1, "text": "/start"},
                            {"action": 3, "text": "check in"},
                            {"action": 4},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (task_dir.parent / "chats_cache.json").write_text(
        json.dumps(
            [{"id": 10001, "title": "fixture", "username": "fixture_bot", "type": "bot"}]
        ),
        encoding="utf-8",
    )
    store = SignPlusStore(tmp_path / "target" / "signplus.sqlite")
    store.migrate()
    cipher = SessionCipher("test-master-key-that-is-long-enough-123456")
    importer = LegacyImporter(
        source=source,
        store=store,
        cipher=cipher,
        success_patterns=("check-in ok", "already checked in"),
        failure_patterns=("check-in failed",),
    )

    dry_run = importer.run(apply=False)
    assert dry_run.accounts_ready == 1
    assert dry_run.tasks_ready == 1
    assert store.list_accounts() == []

    first = importer.run(apply=True)
    second = importer.run(apply=True)

    assert first.accounts_imported == 1
    assert first.tasks_imported == 1
    assert second.accounts_imported == 0
    assert second.tasks_imported == 0
    encrypted = store.get_encrypted_session("primary")
    assert encrypted != "sensitive-session-string"
    assert cipher.decrypt(encrypted) == "sensitive-session-string"
    assert [step["kind"] for step in store.list_tasks()[0]["steps"]] == [
        "send_text",
        "click_button",
        "solve_caption_arithmetic",
    ]
    assert store.list_tasks()[0]["chat_username"] == "fixture_bot"
