from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.backups import (
    BackupBusy,
    BackupInvalid,
    BackupManager,
    RestoreBlocked,
    RestoreConfirmationRequired,
)
from app.saves import InvalidSave, SaveResolver
from app.config import Settings
from conftest import write_save


class RunningMonitor:
    def is_running(self) -> bool:
        return True


class StoppedMonitor:
    def is_running(self) -> bool:
        return False


def read_gametick(path: Path) -> int:
    import sqlite3

    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("SELECT gametick FROM Game").fetchone()[0])
    finally:
        connection.close()


def test_create_and_verify_backup(settings: Settings, save_path: Path) -> None:
    manager = BackupManager(settings, SaveResolver(settings), StoppedMonitor())
    manifest = manager.create_backup("manual")
    assert manifest is not None
    assert manifest["id"] == manager.list_backups()[0]["id"]
    assert manifest["filename"] == "something-test.db"
    assert manifest["gametick"] == 100
    assert manifest["sha256"] == hashlib.sha256(
        manager.backup_file_path(manifest["id"]).read_bytes()
    ).hexdigest()
    verified = manager.verify_backup(manifest["id"])
    assert verified["checksum_ok"] is True
    assert verified["validation"]["valid"] is True
    assert manager.stats()["count"] == 1
    assert manager.stats()["backup_total"] == 1


def test_backup_requires_valid_save(settings: Settings) -> None:
    bad = settings.saves_dir / "User_1" / "Save" / "something-test.db"
    bad.parent.mkdir(parents=True)
    bad.write_text("junk", encoding="utf-8")
    manager = BackupManager(settings, SaveResolver(settings), StoppedMonitor())
    with pytest.raises(InvalidSave):
        manager.create_backup()
    assert manager.stats()["backup_failures"] == 1


def test_only_if_changed(settings: Settings, save_path: Path) -> None:
    manager = BackupManager(settings, SaveResolver(settings), StoppedMonitor())
    first = manager.create_backup("manual", only_if_changed=True)
    assert first is not None
    assert manager.create_backup("manual", only_if_changed=True) is None
    write_save(save_path, gametick=101)
    second = manager.create_backup("manual", only_if_changed=True)
    assert second is not None
    assert second["gametick"] == 101


def test_restore_and_safety_backup(settings: Settings, save_path: Path) -> None:
    manager = BackupManager(settings, SaveResolver(settings), StoppedMonitor())
    backup = manager.create_backup("manual")
    assert backup is not None
    write_save(save_path, gametick=999)
    result = manager.restore(
        backup["id"], confirm_server_stopped=True
    )
    assert result["restored"] is True
    assert read_gametick(save_path) == 100
    assert result["safety_backup"] is not None
    assert result["safety_backup"]["gametick"] == 999
    assert result["validation"]["valid"] is True


def test_restore_requires_confirmation(settings: Settings, save_path: Path) -> None:
    manager = BackupManager(settings, SaveResolver(settings), StoppedMonitor())
    backup = manager.create_backup("manual")
    assert backup is not None
    with pytest.raises(RestoreConfirmationRequired):
        manager.restore(backup["id"])


def test_restore_blocked_while_running(settings: Settings, save_path: Path) -> None:
    manager = BackupManager(settings, SaveResolver(settings), RunningMonitor())
    backup = manager.create_backup("manual")
    assert backup is not None
    with pytest.raises(RestoreBlocked):
        manager.restore(backup["id"], confirm_server_stopped=True)


def test_verify_detects_corruption(settings: Settings, save_path: Path) -> None:
    manager = BackupManager(settings, SaveResolver(settings), StoppedMonitor())
    backup = manager.create_backup("manual")
    assert backup is not None
    path = manager.backup_file_path(backup["id"])
    path.write_bytes(path.read_bytes() + b"corrupt")
    with pytest.raises(BackupInvalid):
        manager.verify_backup(backup["id"])


def test_retention_keeps_latest(settings: Settings, save_path: Path) -> None:
    settings.backup_retention_days = 1
    settings.backup_retention_weeks = 1
    manager = BackupManager(settings, SaveResolver(settings), StoppedMonitor())
    first = manager.create_backup("manual")
    write_save(save_path, gametick=101)
    second = manager.create_backup("manual")
    write_save(save_path, gametick=102)
    third = manager.create_backup("manual")
    assert first and second and third
    backups = manager.list_backups()
    assert len(backups) == 1
    assert backups[0]["id"] == third["id"]


def test_delete_backup(settings: Settings, save_path: Path) -> None:
    manager = BackupManager(settings, SaveResolver(settings), StoppedMonitor())
    backup = manager.create_backup("manual")
    assert backup is not None
    manager.delete_backup(backup["id"])
    assert manager.list_backups() == []
