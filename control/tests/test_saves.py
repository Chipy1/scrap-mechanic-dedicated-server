from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, normalize_save_name
from app.saves import (
    AmbiguousSave,
    InvalidSave,
    PathSecurityError,
    SaveNotFound,
    SaveResolver,
    copy_save_snapshot,
    validate_save,
)
from conftest import write_save


def test_validate_save_ok(save_path: Path) -> None:
    result = validate_save(save_path)
    assert result.valid is True
    assert result.error is None
    assert result.gametick == 100
    assert result.savegameversion == 29
    assert result.table_count >= 3


def test_validate_save_integrity_ok(save_path: Path) -> None:
    result = validate_save(save_path, integrity=True)
    assert result.valid is True
    assert result.integrity_check == "ok"


def test_validate_empty_and_non_sqlite(tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    assert validate_save(empty).valid is False

    junk = tmp_path / "junk.db"
    junk.write_text("not sqlite", encoding="utf-8")
    result = validate_save(junk)
    assert result.valid is False
    assert "SQLite" in (result.error or "")


def test_resolver_single(save_path: Path, resolver: SaveResolver) -> None:
    resolved = resolver.resolve()
    assert resolved.path == save_path
    assert resolved.user_id == "123"
    assert resolver.relative_path(resolved.path) == "User_123/Save/Survival/something-test.db"
    status = resolver.status()
    assert status["found"] is True
    assert status["validation"]["valid"] is True


def test_resolver_custom(settings: Settings) -> None:
    settings.game_mode = "custom"
    path = write_save(settings.saves_dir / "User_9" / "Save" / "Custom" / "something-test.db")
    resolver = SaveResolver(settings)
    assert resolver.resolve().path == path


def test_resolver_missing(settings: Settings) -> None:
    resolver = SaveResolver(settings)
    with pytest.raises(SaveNotFound):
        resolver.resolve()


def test_resolver_ambiguous(settings: Settings) -> None:
    write_save(settings.saves_dir / "User_1" / "Save" / "Survival" / "something-test.db")
    write_save(settings.saves_dir / "User_2" / "Save" / "Survival" / "something-test.db")
    resolver = SaveResolver(settings)
    with pytest.raises(AmbiguousSave):
        resolver.resolve()
    status = resolver.status()
    assert status["found"] is True
    assert status["error"]
    assert status["candidate_count"] == 2


def test_status_skips_when_server_marker_is_fresh(
    settings: Settings, save_path: Path, monkeypatch
) -> None:
    marker = settings.saves_dir / ".sm-server-running"
    marker.write_text("{}", encoding="utf-8")

    def fail(*args, **kwargs):
        raise AssertionError("live save validation must not run")

    monkeypatch.setattr("app.saves.validate_save", fail)
    status = SaveResolver(settings).status()
    assert status["validation"] is None
    assert status["validation_skipped"] == "server is running"


def test_snapshot_refuses_active_journal(
    settings: Settings, save_path: Path
) -> None:
    journal = save_path.with_name(save_path.name + "-journal")
    journal.write_text("hot journal", encoding="utf-8")
    destination = settings.backups_dir / "snapshot.db"
    with pytest.raises(InvalidSave):
        copy_save_snapshot(
            save_path, destination, attempts=1, settle_seconds=0
        )


def test_status_skips_live_validation(
    settings: Settings, save_path: Path, monkeypatch
) -> None:
    resolver = SaveResolver(settings)

    def fail(*args, **kwargs):
        raise AssertionError("live save validation must not run")

    monkeypatch.setattr("app.saves.validate_save", fail)
    status = resolver.status(validate=False)
    assert status["validation"] is None
    assert status["valid"] is None
    assert status["validation_skipped"] == "requested"
    assert status["size_bytes"] == save_path.stat().st_size


def test_resolver_creative(settings: Settings) -> None:
    settings.game_mode = "creative"
    path = write_save(settings.saves_dir / "User_123" / "Save" / settings.save_filename)
    resolver = SaveResolver(settings)
    resolved = resolver.resolve()
    assert resolved.path == path
    assert resolved.is_custom is False
    assert resolver.relative_path(resolved.path) == "User_123/Save/something-test.db"


def test_resolver_steam_id(save_path: Path, resolver: SaveResolver) -> None:
    resolver.settings.steam_id = "123"
    assert resolver.resolve().path == save_path
    resolver.settings.steam_id = "999"
    with pytest.raises(SaveNotFound):
        resolver.resolve()


def test_path_from_relative_traversal(settings: Settings, resolver: SaveResolver) -> None:
    with pytest.raises(PathSecurityError):
        resolver.path_from_relative("../outside.db")
    with pytest.raises(PathSecurityError):
        resolver.path_from_relative("/absolute.db")


def test_normalize_save_name() -> None:
    assert normalize_save_name("something-test") == "something-test"
    assert normalize_save_name("something-test.db") == "something-test"
    with pytest.raises(ValueError):
        normalize_save_name("../bad")
    with pytest.raises(ValueError):
        normalize_save_name("")
