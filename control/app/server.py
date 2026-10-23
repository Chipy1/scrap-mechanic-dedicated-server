from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .logs import LogMonitor
from .saves import SaveResolver


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class MarkerInfo:
    present: bool
    running: bool
    stale: bool
    age_seconds: float | None = None
    pid: int | None = None
    started_at: str | None = None
    started_epoch: float | None = None
    save_name: str | None = None
    save_path: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "present": self.present,
            "running": self.running,
            "stale": self.stale,
            "age_seconds": round(self.age_seconds, 3) if self.age_seconds is not None else None,
            "pid": self.pid,
            "started_at": self.started_at,
            "save_name": self.save_name,
            "save_path": self.save_path,
            "error": self.error,
        }


class ServerMonitor:
    """Combine the entrypoint heartbeat with log-derived server state."""

    def __init__(
        self, settings: Settings, logs: LogMonitor, resolver: SaveResolver
    ) -> None:
        self.settings = settings
        self.logs = logs
        self.resolver = resolver

    @property
    def marker_path(self) -> Path:
        return self.settings.saves_dir / ".sm-server-running"

    def read_marker(self) -> MarkerInfo:
        path = self.marker_path
        if not path.exists():
            return MarkerInfo(present=False, running=False, stale=False)
        try:
            stat = path.stat()
            age = max(0.0, time.time() - stat.st_mtime)
        except OSError as exc:
            return MarkerInfo(
                present=False,
                running=False,
                stale=False,
                error=f"cannot stat marker: {exc}",
            )
        running = age <= self.settings.server_stale_seconds
        data: dict[str, Any] = {}
        error: str | None = None
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                data = parsed
            else:
                error = "marker is not a JSON object"
        except (OSError, ValueError) as exc:
            error = f"cannot read marker: {exc}"
        return MarkerInfo(
            present=True,
            running=running,
            stale=not running,
            age_seconds=age,
            pid=int(data["pid"]) if isinstance(data.get("pid"), int) else None,
            started_at=data.get("started_at"),
            started_epoch=(
                float(data["started_epoch"])
                if isinstance(data.get("started_epoch"), (int, float))
                else None
            ),
            save_name=data.get("save_name"),
            save_path=data.get("save_path"),
            error=error,
        )

    def _uptime_seconds(self, marker: MarkerInfo) -> float:
        if marker.started_epoch is not None and marker.running:
            return max(0.0, time.time() - marker.started_epoch)
        started = _parse_iso(marker.started_at)
        if started is not None and marker.running:
            return max(0.0, (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds())
        if self.logs.server_started and self.logs.is_fresh():
            started = _parse_iso(self.logs.server_started_at)
            if started is not None:
                return max(
                    0.0,
                    (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds(),
                )
        return 0.0

    def status(self) -> dict[str, Any]:
        marker = self.read_marker()
        log_status = self.logs.to_dict()
        save_match = self.logs.save_match()

        # The marker heartbeat is authoritative. A fresh old log must not make
        # a stopped server look alive after the marker has been removed.
        if marker.running:
            state = "running" if self.logs.server_started else "starting"
        else:
            state = "stopped"

        if state in {"running", "starting"} and save_match is False:
            state = "error"

        up = state in {"running", "starting"}
        exclude = self.settings.steam_id or self.resolver.user_id()
        players = self.logs.players(exclude_steam_id=exclude) if up else []
        return {
            "state": state,
            "up": up,
            "uptime_seconds": round(self._uptime_seconds(marker), 3) if up else 0.0,
            "version": self.logs.version,
            "started_at": marker.started_at or self.logs.server_started_at,
            "active_save": self.logs.active_save_filename(),
            "save_match": save_match,
            "players_online": len(players),
            "players": players,
            "marker": marker.to_dict(),
            "log": log_status,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    def is_running(self) -> bool:
        return self.status()["up"]
