from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BUILD_VERSION = "0.1.0"
DEFAULT_SAVE_NAME = "something-test"
DEFAULT_GAME_MODE = "survival"
GAME_MODES = ("custom", "survival", "creative")


def _first_env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip() != "":
            return value
    return default


def _bool_env(*names: str, default: bool) -> bool:
    value = _first_env(*names)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def _int_env(*names: str, default: int, minimum: int | None = None) -> int:
    value = _first_env(*names)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"invalid integer for {names[0]}: {value!r}") from exc
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{names[0]} must be >= {minimum}")
    return parsed


def _float_env(*names: str, default: float, minimum: float | None = None) -> float:
    value = _first_env(*names)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"invalid number for {names[0]}: {value!r}") from exc
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{names[0]} must be >= {minimum}")
    return parsed


def normalize_game_mode(value: str) -> str:
    mode = (value or "").strip().lower()
    if mode not in GAME_MODES:
        choices = "|".join(GAME_MODES)
        raise ValueError(f"GAME_MODE must be one of {choices}")
    return mode


def normalize_save_name(value: str) -> str:
    """Return the save stem without the trailing .db suffix."""
    name = (value or "").strip()
    if name.lower().endswith(".db"):
        name = name[:-3]
    if not name or name in {".", ".."}:
        raise ValueError("SAVE_NAME must not be empty")
    if any(ch in name for ch in ("/", "\\", "\x00")):
        raise ValueError("SAVE_NAME must not contain path separators")
    if name != name.strip():
        raise ValueError("SAVE_NAME must not start or end with whitespace")
    return name


@dataclass
class Settings:
    saves_dir: Path
    logs_dir: Path
    backups_dir: Path
    state_dir: Path
    save_name: str = DEFAULT_SAVE_NAME
    game_mode: str = DEFAULT_GAME_MODE
    api_token: str = ""
    steam_id: str | None = None
    backup_interval_minutes: int = 15
    backup_retention_days: int = 7
    backup_retention_weeks: int = 4
    backup_auto: bool = True
    require_server_stopped: bool = True
    log_poll_seconds: float = 1.0
    server_stale_seconds: int = 90
    build_version: str = BUILD_VERSION

    @classmethod
    def from_env(cls) -> "Settings":
        save_name = normalize_save_name(
            _first_env("SM_SAVE_NAME", "SAVE_NAME", default=DEFAULT_SAVE_NAME)
            or DEFAULT_SAVE_NAME
        )
        steam_id = _first_env("SM_STEAM_ID", "STEAM_ID")
        if steam_id is not None:
            steam_id = steam_id.strip()
            if steam_id and not steam_id.isdigit():
                raise ValueError("SM_STEAM_ID must contain only digits")

        settings = cls(
            saves_dir=Path(_first_env("SM_SAVES_DIR", default="/saves") or "/saves"),
            logs_dir=Path(_first_env("SM_LOGS_DIR", default="/logs") or "/logs"),
            backups_dir=Path(_first_env("SM_BACKUPS_DIR", default="/backups") or "/backups"),
            state_dir=Path(
                _first_env("SM_STATE_DIR", default="/var/lib/sm-control")
                or "/var/lib/sm-control"
            ),
            save_name=save_name,
            game_mode=normalize_game_mode(
                _first_env("SM_GAME_MODE", "GAME_MODE")
                or ("custom" if _bool_env("SM_IS_CUSTOM_GAME", "IS_CUSTOM_GAME", default=False) else "survival")
            ),
            api_token=_first_env("SM_API_TOKEN", default="") or "",
            steam_id=steam_id or None,
            backup_interval_minutes=_int_env(
                "SM_BACKUP_INTERVAL_MINUTES",
                "BACKUP_INTERVAL_MINUTES",
                default=15,
                minimum=1,
            ),
            backup_retention_days=_int_env(
                "SM_BACKUP_RETENTION_DAYS",
                "BACKUP_RETENTION_DAYS",
                default=7,
                minimum=0,
            ),
            backup_retention_weeks=_int_env(
                "SM_BACKUP_RETENTION_WEEKS",
                "BACKUP_RETENTION_WEEKS",
                default=4,
                minimum=0,
            ),
            backup_auto=_bool_env("SM_BACKUP_AUTO", default=True),
            require_server_stopped=_bool_env(
                "SM_REQUIRE_SERVER_STOPPED", default=True
            ),
            log_poll_seconds=_float_env(
                "SM_LOG_POLL_SECONDS", default=1.0, minimum=0.1
            ),
            server_stale_seconds=_int_env(
                "SM_SERVER_STALE_SECONDS", default=90, minimum=10
            ),
        )
        settings.validate()
        return settings

    @property
    def save_filename(self) -> str:
        return f"{self.save_name}.db"

    @property
    def is_custom_game(self) -> bool:
        # Kept as a compatibility alias for older API clients. Creative worlds
        # are not custom games and live directly in Save.
        return self.game_mode == "custom"

    @property
    def save_relative_dir(self) -> Path:
        # Scrap Mechanic layout:
        #   survival -> Save/Survival/<name>.db
        #   custom   -> Save/Custom/<name>.db
        #   creative -> Save/<name>.db
        if self.game_mode == "survival":
            return Path("Save") / "Survival"
        if self.game_mode == "custom":
            return Path("Save") / "Custom"
        return Path("Save")

    def validate(self) -> None:
        self.save_name = normalize_save_name(self.save_name)
        self.game_mode = normalize_game_mode(self.game_mode)
        if self.backup_interval_minutes < 1:
            raise ValueError("backup interval must be >= 1 minute")
        if self.backup_retention_days < 0 or self.backup_retention_weeks < 0:
            raise ValueError("backup retention must be >= 0")
        if self.log_poll_seconds < 0.1:
            raise ValueError("log poll interval must be >= 0.1 seconds")
        if self.server_stale_seconds < 10:
            raise ValueError("server stale timeout must be >= 10 seconds")
        if self.steam_id is not None and self.steam_id and not self.steam_id.isdigit():
            raise ValueError("steam_id must contain only digits")

    def ensure_dirs(self) -> None:
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        for directory in (self.saves_dir, self.logs_dir):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                # A missing/unmounted logs or saves volume should not prevent the
                # control-plane from starting and exposing a useful status.
                pass

    def public_dict(self) -> dict[str, Any]:
        return {
            "build_version": self.build_version,
            "save_name": self.save_name,
            "save_filename": self.save_filename,
            "game_mode": self.game_mode,
            "is_custom_game": self.is_custom_game,
            "steam_id": self.steam_id,
            "paths": {
                "saves": str(self.saves_dir),
                "logs": str(self.logs_dir),
                "backups": str(self.backups_dir),
                "state": str(self.state_dir),
            },
            "backup": {
                "auto": self.backup_auto,
                "interval_minutes": self.backup_interval_minutes,
                "retention_days": self.backup_retention_days,
                "retention_weeks": self.backup_retention_weeks,
            },
            "require_server_stopped": self.require_server_stopped,
            "api_token_set": bool(self.api_token),
        }


RUNTIME_FIELDS = {
    "save_name",
    "game_mode",
    "backup_interval_minutes",
    "backup_retention_days",
    "backup_retention_weeks",
    "backup_auto",
    "require_server_stopped",
}


class RuntimeConfig:
    """Runtime-editable, non-secret settings persisted as JSON."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.state_dir / "config.json"
        self._lock = threading.RLock()
        self.updated_at: str | None = None

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            if not isinstance(data, dict):
                return
            for key in RUNTIME_FIELDS:
                if key not in data:
                    continue
                try:
                    setattr(self.settings, key, data[key])
                except (TypeError, ValueError):
                    pass
            try:
                self.settings.validate()
            except ValueError:
                # Ignore a corrupt override file rather than preventing startup.
                pass
            self.updated_at = data.get("updated_at")

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        unknown = set(patch) - RUNTIME_FIELDS
        if unknown:
            raise ValueError(f"unknown config keys: {', '.join(sorted(unknown))}")
        with self._lock:
            old = {key: getattr(self.settings, key) for key in RUNTIME_FIELDS}
            try:
                for key, value in patch.items():
                    if key == "save_name":
                        value = normalize_save_name(str(value))
                    elif key == "game_mode":
                        value = normalize_game_mode(str(value))
                    elif key in {
                        "backup_interval_minutes",
                        "backup_retention_days",
                        "backup_retention_weeks",
                    }:
                        value = int(value)
                    elif key in {"backup_auto", "require_server_stopped"}:
                        value = bool(value)
                    setattr(self.settings, key, value)
                self.settings.validate()
                self.updated_at = datetime.now(timezone.utc).isoformat()
                self._save_locked()
            except Exception:
                for key, value in old.items():
                    setattr(self.settings, key, value)
                raise
            return self.public_dict()

    def public_dict(self) -> dict[str, Any]:
        data = self.settings.public_dict()
        data["updated_at"] = self.updated_at
        return data

    def _save_locked(self) -> None:
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": self.updated_at,
            **{key: getattr(self.settings, key) for key in RUNTIME_FIELDS},
        }
        tmp = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)


def settings_from_public(data: dict[str, Any]) -> dict[str, Any]:
    """Small helper used by tests and scripts; intentionally redacts paths."""
    allowed = {
        "save_name",
        "save_filename",
        "game_mode",
        "is_custom_game",
        "steam_id",
        "backup",
        "require_server_stopped",
    }
    return {key: data[key] for key in allowed if key in data}
