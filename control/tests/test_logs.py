from __future__ import annotations

import os
from pathlib import Path

from app.config import Settings
from app.logs import LogMonitor


def log_text(
    *,
    version: str = "1.0.6.889",
    save: str = "C:/users/steamuser/AppData/Roaming/Axolot Games/Scrap Mechanic/User/User_1/Save/something-test.db",
    connected: bool = True,
) -> str:
    lines = [
        "21:52:46 (1/0) [Main:344] WARNING: Console.cpp:78 Unable to allocate debug console",
        f"21:52:46 (1/0) [Main:344] [Default] Game version {version}",
        f"21:52:53 (1/0) [Main:344] [Default] Fetching game info for: '{save}'",
        f"21:52:54 (1/0) [Main:344] [Default] Opening save file: '{save}'",
        "21:52:54 (1/0) [Main:344] [Default] Server started",
        "21:52:54 (1/0) [Main:344] ERROR: ParticleManager.cpp:1514 Failed to load particle texture",
        "22:40:00 (1/0) [Main:344] [Network] Connection Status Changed",
        "22:40:00 (1/0) [Main:344] [Network] \tConnection handle: 123, user: 76561199141049812",
        "22:40:00 (1/0) [Main:344] [Network] \tState: None -> Connecting",
        "22:40:01 (1/0) [Main:344] [Network] Connection Status Changed",
        "22:40:01 (1/0) [Main:344] [Network] \tConnection handle: 123, user: 76561199141049812",
        "22:40:01 (1/0) [Main:344] [Network] \tState: Finding Route -> Connected",
    ]
    if not connected:
        lines += [
            "22:41:00 (1/0) [Main:344] [Network] Connection Status Changed",
            "22:41:00 (1/0) [Main:344] [Network] \tConnection handle: 123, user: 76561199141049812",
            "22:41:00 (1/0) [Main:344] [Network] \tState: Connected -> Closed By Peer",
            "22:41:00 (1/0) [Main:344] [Network] Connection Status Changed",
            "22:41:00 (1/0) [Main:344] [Network] \tConnection handle: 123, user: 76561199141049812",
            "22:41:00 (1/0) [Main:344] [Network] \tState: Closed By Peer -> None",
        ]
    return "\n".join(lines) + "\n"


def write_log(settings: Settings, text: str, name: str = "game-20260101-000000.log") -> Path:
    path = settings.logs_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_log(settings: Settings) -> None:
    write_log(settings, log_text())
    monitor = LogMonitor(settings)
    new_lines = monitor.poll()
    assert len(new_lines) >= 5
    assert monitor.version == "1.0.6.889"
    assert monitor.active_save_filename() == "something-test.db"
    assert monitor.active_save_is_custom() is False
    assert monitor.save_match() is True
    assert monitor.server_started is True
    assert monitor.errors == 1
    assert monitor.warnings == 1
    players = monitor.players()
    assert len(players) == 1
    assert players[0]["steam_id"] == "76561199141049812"
    assert monitor.tail(2)[-1].endswith("Connected")


def test_player_disconnect(settings: Settings) -> None:
    write_log(settings, log_text(connected=False))
    monitor = LogMonitor(settings)
    monitor.poll()
    assert monitor.players() == []


def test_save_match_false_for_custom(settings: Settings) -> None:
    save = "C:/users/steamuser/AppData/Roaming/Axolot Games/Scrap Mechanic/User/User_1/Save/Custom/something-test.db"
    write_log(settings, log_text(save=save))
    monitor = LogMonitor(settings)
    monitor.poll()
    assert monitor.active_save_is_custom() is True
    assert monitor.save_match() is False


def test_log_rotation_resets_state(settings: Settings) -> None:
    first = write_log(settings, log_text(version="1.0.0.1"), "game-20260101-000000.log")
    monitor = LogMonitor(settings)
    monitor.poll()
    assert monitor.version == "1.0.0.1"

    second = write_log(settings, log_text(version="1.0.0.2"), "game-20260101-000001.log")
    os.utime(first, (1, 1))
    os.utime(second, (2, 2))
    monitor.poll()
    assert monitor.version == "1.0.0.2"
    assert monitor.server_started is True


def test_partial_line_waits_for_newline(settings: Settings) -> None:
    path = settings.logs_dir / "game-20260101-000000.log"
    path.write_text("21:52:46 (1/0) [Main:344] [Default] Game version 1.0.0.1", encoding="utf-8")
    monitor = LogMonitor(settings)
    assert monitor.poll() == []
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert monitor.poll()
    assert monitor.version == "1.0.0.1"


def test_tail_empty(settings: Settings) -> None:
    monitor = LogMonitor(settings)
    assert monitor.tail(10) == []
