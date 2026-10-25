from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

CONTROL_ROOT = Path(__file__).resolve().parents[1]
if str(CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_ROOT))

from app.config import Settings  # noqa: E402
from app.saves import SaveResolver  # noqa: E402


def write_save(
    path: Path,
    *,
    gametick: int = 100,
    savegameversion: int = 29,
    mods: bytes = b"\x00\x00\x00\x00",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE Game (
                savegameversion INTEGER,
                flags INTEGER,
                seed INTEGER,
                gametick INTEGER,
                mods BLOB,
                uniqueIds BLOB
            )
            """
        )
        connection.execute(
            "INSERT INTO Game VALUES (?, 7, 123, ?, ?, ?)",
            (savegameversion, gametick, mods, b"\x00\x01"),
        )
        connection.execute("CREATE TABLE RigidBody (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE VoxelTerrain (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()
    return path


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    saves = tmp_path / "saves"
    logs = tmp_path / "logs"
    backups = tmp_path / "backups"
    state = tmp_path / "state"
    for directory in (saves, logs, backups, state):
        directory.mkdir(parents=True, exist_ok=True)
    return Settings(
        saves_dir=saves,
        logs_dir=logs,
        backups_dir=backups,
        state_dir=state,
        save_name="something-test",
        game_mode="survival",
        api_token="test-token",
        backup_auto=False,
        log_poll_seconds=0.1,
        server_stale_seconds=10,
    )


@pytest.fixture()
def save_path(settings: Settings) -> Path:
    return write_save(
        settings.saves_dir / "User_123" / "Save" / "Survival" / settings.save_filename
    )


@pytest.fixture()
def resolver(settings: Settings) -> SaveResolver:
    return SaveResolver(settings)
