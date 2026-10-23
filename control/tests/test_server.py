from __future__ import annotations

import json
import os
import time
from pathlib import Path

from app.config import Settings
from app.logs import LogMonitor
from app.saves import SaveResolver
from app.server import ServerMonitor
from test_logs import log_text, write_log


def write_marker(settings: Settings, *, age: float = 0.0, save: str = "something-test") -> Path:
    path = settings.saves_dir / ".sm-server-running"
    path.write_text(
        json.dumps(
            {
                "pid": 123,
                "started_at": "2026-01-01T00:00:00+00:00",
                "started_epoch": time.time() - 10,
                "save_name": save,
                "save_path": f"C:/Save/{save}.db",
            }
        ),
        encoding="utf-8",
    )
    if age:
        old = time.time() - age
        os.utime(path, (old, old))
    return path


def make_monitor(settings: Settings) -> ServerMonitor:
    logs = LogMonitor(settings)
    return ServerMonitor(settings, logs, SaveResolver(settings))


def test_running_marker(settings: Settings, save_path: Path) -> None:
    write_log(settings, log_text())
    write_marker(settings)
    monitor = make_monitor(settings)
    monitor.logs.poll()
    status = monitor.status()
    assert status["state"] == "running"
    assert status["up"] is True
    assert status["marker"]["running"] is True
    assert status["save_match"] is True
    assert status["players_online"] == 1
    assert status["uptime_seconds"] >= 0


def test_stale_marker(settings: Settings) -> None:
    write_marker(settings, age=120)
    monitor = make_monitor(settings)
    status = monitor.status()
    assert status["state"] == "stopped"
    assert status["marker"]["stale"] is True


def test_save_mismatch_is_error(settings: Settings, save_path: Path) -> None:
    save = "C:/users/steamuser/AppData/Roaming/Axolot Games/Scrap Mechanic/User/User_1/Save/other.db"
    write_log(settings, log_text(save=save))
    write_marker(settings)
    monitor = make_monitor(settings)
    monitor.logs.poll()
    status = monitor.status()
    assert status["state"] == "error"
    assert status["save_match"] is False


def test_server_account_excluded(settings: Settings, save_path: Path) -> None:
    settings.steam_id = "76561199141049812"
    write_log(settings, log_text())
    write_marker(settings)
    monitor = make_monitor(settings)
    monitor.logs.poll()
    assert monitor.status()["players_online"] == 0
