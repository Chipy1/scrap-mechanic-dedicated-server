from __future__ import annotations

import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings

_LOG_TIME_RE = re.compile(r"^(?P<time>\d{2}:\d{2}:\d{2})")
_VERSION_RE = re.compile(r"Game version\s+([0-9][0-9A-Za-z.\-]*)")
_FETCH_RE = re.compile(r"Fetching game info for:\s*'([^']+)'")
_OPEN_RE = re.compile(r"Opening save file:\s*'([^']+)'")
_HANDLE_RE = re.compile(r"Connection handle:\s*(\d+),\s*user:\s*(\d+)")
_STATE_RE = re.compile(r"State:\s*(.+?)\s*->\s*(.+?)\s*$")
_LOADED_PLAYER_RE = re.compile(r"Loaded player\s+\d+\s+\(for user\s+(\d+)\)")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_windows_path(value: str) -> str:
    return value.replace("\\", "/")


class LogMonitor:
    """Incrementally parse the newest Scrap Mechanic server log."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._current_path: Path | None = None
        self._offset = 0
        self._recent: deque[str] = deque(maxlen=2000)
        self._reset_state_locked()
        self._last_line_epoch: float | None = None
        self._last_line_at: str | None = None

    def _reset_state_locked(self) -> None:
        self.version: str | None = None
        self.active_save_path: str | None = None
        self.server_started = False
        self.server_started_at: str | None = None
        self.errors = 0
        self.warnings = 0
        self._handle_users: dict[str, str] = {}
        self._connected_handles: set[str] = set()
        self._last_handle: str | None = None
        self._known_players: set[str] = set()
        self._last_line_epoch: float | None = None
        self._last_line_at: str | None = None

    def latest_path(self) -> Path | None:
        root = self.settings.logs_dir
        if not root.exists():
            return None
        candidates: list[tuple[float, str, Path]] = []
        try:
            for path in root.glob("game-*.log"):
                if not path.is_file():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                candidates.append((stat.st_mtime, path.name, path))
        except OSError:
            return None
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][2]

    def poll(self) -> list[str]:
        """Read complete new lines, update state, and return the new lines."""
        with self._lock:
            path = self.latest_path()
            if path is None:
                return []
            if self._current_path != path:
                self._current_path = path
                self._offset = 0
                self._recent.clear()
                self._reset_state_locked()
            try:
                size = path.stat().st_size
            except OSError:
                return []
            if size < self._offset:
                self._offset = 0
                self._reset_state_locked()
            if size == self._offset:
                return []
            try:
                with path.open("rb") as handle:
                    handle.seek(self._offset)
                    data = handle.read()
            except OSError:
                return []
            if not data:
                return []
            newline = data.rfind(b"\n")
            if newline < 0:
                # A partially written line: wait for the next poll.
                return []
            complete = data[: newline + 1]
            self._offset += len(complete)
            text = complete.decode("utf-8", errors="replace")
            lines = text.splitlines()
            for line in lines:
                self._parse_line(line)
                self._recent.append(line)
            if lines:
                self._last_line_epoch = time.time()
                self._last_line_at = _now_iso()
            return lines

    def _parse_line(self, line: str) -> None:
        line = line.rstrip("\r")
        if not line:
            return
        match = _VERSION_RE.search(line)
        if match:
            self.version = match.group(1)
        match = _FETCH_RE.search(line)
        if match:
            self.active_save_path = match.group(1)
        match = _OPEN_RE.search(line)
        if match:
            self.active_save_path = match.group(1)
        if "Server started" in line:
            self.server_started = True
            if self.server_started_at is None:
                self.server_started_at = _now_iso()
        if "ERROR:" in line or re.search(r"\bERROR\b", line):
            self.errors += 1
        if "WARNING:" in line or re.search(r"\bWARNING\b", line):
            self.warnings += 1

        match = _HANDLE_RE.search(line)
        if match:
            handle, user = match.groups()
            self._handle_users[handle] = user
            self._last_handle = handle
            return

        match = _STATE_RE.search(line)
        if match and self._last_handle:
            source, destination = match.groups()
            source = source.strip()
            destination = destination.strip()
            handle = self._last_handle
            if destination == "Connected":
                self._connected_handles.add(handle)
            if source == "Connected" and destination != "Connected":
                self._connected_handles.discard(handle)
            if destination == "None":
                self._connected_handles.discard(handle)
            return

        match = _LOADED_PLAYER_RE.search(line)
        if match:
            self._known_players.add(match.group(1))

    def tail(self, count: int = 200) -> list[str]:
        count = max(1, min(int(count), 5000))
        with self._lock:
            path = self._current_path or self.latest_path()
            if path is None:
                return []
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    lines = handle.readlines()
            except OSError:
                return []
            return [line.rstrip("\r\n") for line in lines[-count:]]

    def players(self, exclude_steam_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            seen: set[str] = set()
            result: list[dict[str, Any]] = []
            for handle in sorted(self._connected_handles):
                user = self._handle_users.get(handle)
                key = user or f"handle:{handle}"
                if exclude_steam_id and user == exclude_steam_id:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                result.append({"steam_id": user, "handle": handle})
            return result

    def active_save_filename(self) -> str | None:
        with self._lock:
            if not self.active_save_path:
                return None
            normalized = _norm_windows_path(self.active_save_path)
            return normalized.rsplit("/", 1)[-1]

    def active_save_is_custom(self) -> bool | None:
        with self._lock:
            if not self.active_save_path:
                return None
            normalized = _norm_windows_path(self.active_save_path).lower()
            return "/save/custom/" in normalized

    def save_match(self) -> bool | None:
        filename = self.active_save_filename()
        if filename is None:
            return None
        custom = self.active_save_is_custom()
        return (
            filename.lower() == self.settings.save_filename.lower()
            and custom == self.settings.is_custom_game
        )

    def is_fresh(self) -> bool:
        with self._lock:
            if self._last_line_epoch is None:
                return False
            return (time.time() - self._last_line_epoch) <= self.settings.server_stale_seconds

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "path": str(self._current_path) if self._current_path else None,
                "version": self.version,
                "active_save_path": self.active_save_path,
                "active_save_filename": self.active_save_filename(),
                "active_save_is_custom": self.active_save_is_custom(),
                "game_mode": self.settings.game_mode,
                "save_match": self.save_match(),
                "server_started": self.server_started,
                "server_started_at": self.server_started_at,
                "last_line_at": self._last_line_at,
                "fresh": self.is_fresh(),
                "errors": self.errors,
                "warnings": self.warnings,
                "players_online": len(self.players()),
            }
