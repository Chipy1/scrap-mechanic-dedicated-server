from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings


class SaveError(Exception):
    """Base class for save-related errors."""


class SaveNotFound(SaveError):
    pass


class AmbiguousSave(SaveError):
    def __init__(self, paths: list[Path]) -> None:
        self.paths = paths
        rendered = ", ".join(str(path) for path in paths)
        super().__init__(f"multiple saves match: {rendered}")


class InvalidSave(SaveError):
    pass


class PathSecurityError(SaveError):
    pass


@dataclass(frozen=True)
class SaveValidation:
    valid: bool
    error: str | None = None
    size_bytes: int = 0
    quick_check: str | None = None
    integrity_check: str | None = None
    gametick: int | None = None
    savegameversion: int | None = None
    mods_sha256: str | None = None
    table_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "error": self.error,
            "size_bytes": self.size_bytes,
            "quick_check": self.quick_check,
            "integrity_check": self.integrity_check,
            "gametick": self.gametick,
            "savegameversion": self.savegameversion,
            "mods_sha256": self.mods_sha256,
            "table_count": self.table_count,
        }


@dataclass(frozen=True)
class SavePath:
    path: Path
    user_id: str | None
    is_custom: bool
    save_name: str
    size_bytes: int
    mtime: float

    @property
    def filename(self) -> str:
        return self.path.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "user_id": self.user_id,
            "is_custom": self.is_custom,
            "save_name": self.save_name,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "mtime": self.mtime,
        }


def _readonly_uri(path: Path) -> str:
    # SQLite's URI parser needs the path percent-encoded. Keep slashes readable.
    encoded = urllib.parse.quote(str(path.resolve()), safe="/")
    return f"file:{encoded}?mode=ro"


def _connect_readonly(path: Path, timeout: float = 10.0) -> sqlite3.Connection:
    connection = sqlite3.connect(
        _readonly_uri(path),
        uri=True,
        timeout=timeout,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_save(path: Path, *, integrity: bool = False) -> SaveValidation:
    """Validate a Scrap Mechanic SQLite save without modifying it."""
    try:
        if not path.exists():
            return SaveValidation(valid=False, error=f"save does not exist: {path}")
        if not path.is_file():
            return SaveValidation(valid=False, error=f"save is not a file: {path}")
        size = path.stat().st_size
        if size <= 0:
            return SaveValidation(valid=False, error="save is empty")
    except OSError as exc:
        return SaveValidation(valid=False, error=f"cannot stat save: {exc}")

    connection: sqlite3.Connection | None = None
    try:
        connection = _connect_readonly(path)
        pragma = "integrity_check" if integrity else "quick_check"
        check_rows = [str(row[0]) for row in connection.execute(f"PRAGMA {pragma}")]
        check_value = "; ".join(check_rows) if check_rows else "no result"
        if not check_rows or any(row.lower() != "ok" for row in check_rows):
            return SaveValidation(
                valid=False,
                error=f"{pragma} failed: {check_value}",
                size_bytes=size,
                quick_check=check_value if pragma == "quick_check" else None,
                integrity_check=check_value if pragma == "integrity_check" else None,
            )

        table_rows = list(
            connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND lower(name) = 'game'"
            )
        )
        if not table_rows:
            return SaveValidation(
                valid=False,
                error="SQLite save has no Game table",
                size_bytes=size,
                quick_check=check_value if pragma == "quick_check" else None,
                integrity_check=check_value if pragma == "integrity_check" else None,
            )

        info_rows = list(connection.execute('PRAGMA table_info("Game")'))
        columns = {str(row[1]).lower() for row in info_rows}
        if "gametick" not in columns or "savegameversion" not in columns:
            return SaveValidation(
                valid=False,
                error="Game table lacks gametick/savegameversion columns",
                size_bytes=size,
                quick_check=check_value if pragma == "quick_check" else None,
                integrity_check=check_value if pragma == "integrity_check" else None,
            )

        row = connection.execute('SELECT * FROM "Game" LIMIT 1').fetchone()
        if row is None:
            return SaveValidation(
                valid=False,
                error="Game table is empty",
                size_bytes=size,
                quick_check=check_value if pragma == "quick_check" else None,
                integrity_check=check_value if pragma == "integrity_check" else None,
            )

        gametick = row["gametick"] if "gametick" in row.keys() else None
        savegameversion = (
            row["savegameversion"] if "savegameversion" in row.keys() else None
        )
        mods = row["mods"] if "mods" in row.keys() else None
        if isinstance(mods, memoryview):
            mods = mods.tobytes()
        mods_sha = (
            hashlib.sha256(mods).hexdigest()
            if isinstance(mods, (bytes, bytearray)) and len(mods) > 0
            else None
        )
        table_count = int(
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
            ).fetchone()[0]
        )
        return SaveValidation(
            valid=True,
            size_bytes=size,
            quick_check=check_value if pragma == "quick_check" else None,
            integrity_check=check_value if pragma == "integrity_check" else None,
            gametick=int(gametick) if gametick is not None else None,
            savegameversion=(
                int(savegameversion) if savegameversion is not None else None
            ),
            mods_sha256=mods_sha,
            table_count=table_count,
        )
    except Exception as exc:  # sqlite3.DatabaseError, OSError, ValueError, ...
        return SaveValidation(valid=False, error=f"cannot read SQLite save: {exc}")
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass


def copy_save_snapshot(
    source: Path,
    destination: Path,
    *,
    attempts: int = 8,
    settle_seconds: float = 0.25,
) -> None:
    """Copy a consistent save snapshot without taking a SQLite lock.

    Scrap Mechanic has no busy timeout: any read transaction held by a control
    process can make its next commit fail with "database is locked" and crash
    the server. Instead of the Online Backup API, copy the main database while
    it is quiescent and retry if a rollback journal, WAL, or mtime change shows
    that the game wrote during the copy. The destination is validated by the
    caller after it is detached from the live file.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    sidecars = [
        source.with_name(source.name + suffix)
        for suffix in ("-journal", "-wal", "-shm")
    ]
    for attempt in range(attempts):
        if any(path.exists() for path in sidecars):
            if attempt + 1 < attempts:
                time.sleep(settle_seconds)
                continue
            raise InvalidSave("save is being written; retry backup")
        before = source.stat()
        shutil.copyfile(source, destination)
        after = source.stat()
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or any(path.exists() for path in sidecars)
        ):
            if attempt + 1 < attempts:
                time.sleep(settle_seconds)
                continue
            raise InvalidSave("save changed during copy; retry backup")
        with destination.open("rb") as handle:
            os.fsync(handle.fileno())
        return
    raise InvalidSave("save is being written; retry backup")


class SaveResolver:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def save_filename(self) -> str:
        return self.settings.save_filename

    def _path_for_user(self, user_id: str) -> Path:
        return (
            self.settings.saves_dir
            / f"User_{user_id}"
            / self.settings.save_relative_dir
            / self.save_filename
        )

    def find_all(self) -> list[SavePath]:
        found: list[SavePath] = []
        root = self.settings.saves_dir
        if not root.exists():
            return found
        try:
            user_dirs = sorted(root.glob("User_*"))
        except OSError:
            return found
        for user_dir in user_dirs:
            if not user_dir.is_dir():
                continue
            path = user_dir / self.settings.save_relative_dir / self.save_filename
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            found.append(
                SavePath(
                    path=path,
                    user_id=user_dir.name.removeprefix("User_"),
                    is_custom=self.settings.is_custom_game,
                    save_name=self.settings.save_name,
                    size_bytes=stat.st_size,
                    mtime=stat.st_mtime,
                )
            )
        found.sort(key=lambda item: item.mtime, reverse=True)
        return found

    def resolve(self) -> SavePath:
        if self.settings.steam_id:
            path = self._path_for_user(self.settings.steam_id)
            if not path.is_file():
                raise SaveNotFound(
                    f"save not found for SteamID {self.settings.steam_id}: {path}"
                )
            stat = path.stat()
            return SavePath(
                path=path,
                user_id=self.settings.steam_id,
                is_custom=self.settings.is_custom_game,
                save_name=self.settings.save_name,
                size_bytes=stat.st_size,
                mtime=stat.st_mtime,
            )
        found = self.find_all()
        if not found:
            raise SaveNotFound(f"save not found: {self.save_filename}")
        if len(found) > 1:
            raise AmbiguousSave([item.path for item in found])
        return found[0]

    def user_id(self) -> str | None:
        if self.settings.steam_id:
            return self.settings.steam_id
        try:
            return self.resolve().user_id
        except SaveError:
            return None

    def relative_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.settings.saves_dir.resolve()).as_posix()
        except (OSError, ValueError) as exc:
            raise PathSecurityError(f"path is outside the saves volume: {path}") from exc

    def path_from_relative(self, relative: str) -> Path:
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise PathSecurityError("backup path must be relative and stay inside saves")
        target = (self.settings.saves_dir / candidate).resolve()
        root = self.settings.saves_dir.resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise PathSecurityError("backup path is outside the saves volume") from exc
        return target

    def _server_marker_active(self) -> bool:
        marker = self.settings.saves_dir / ".sm-server-running"
        try:
            age = time.time() - marker.stat().st_mtime
        except OSError:
            return False
        return age <= self.settings.server_stale_seconds

    def status(self, *, validate: bool = True) -> dict[str, Any]:
        all_saves = self.find_all()
        result: dict[str, Any] = {
            "save_name": self.settings.save_name,
            "filename": self.save_filename,
            "game_mode": self.settings.game_mode,
            "is_custom_game": self.settings.is_custom_game,
            "steam_id": self.settings.steam_id,
            "candidate_count": len(all_saves),
            "candidates": [item.to_dict() for item in all_saves],
            "found": bool(all_saves),
            "path": None,
            "relative_path": None,
            "validation": None,
            "validation_skipped": None,
            "valid": None,
            "error": None,
        }
        if validate and self._server_marker_active():
            validate = False
            result["validation_skipped"] = "server is running"
        try:
            save = self.resolve()
        except SaveError as exc:
            result["error"] = str(exc)
            if isinstance(exc, AmbiguousSave):
                result["path"] = str(exc.paths[0])
                result["relative_path"] = self.relative_path(exc.paths[0])
                if validate:
                    result["validation"] = validate_save(
                        exc.paths[0], integrity=False
                    ).to_dict()
            return result
        result.update(
            {
                "path": str(save.path),
                "relative_path": self.relative_path(save.path),
                "user_id": save.user_id,
                "size_bytes": save.size_bytes,
                "mtime": save.mtime,
            }
        )
        if not validate:
            if result["validation_skipped"] is None:
                result["validation_skipped"] = "requested"
            return result
        validation = validate_save(save.path, integrity=False)
        result["validation"] = validation.to_dict()
        result["valid"] = validation.valid
        return result
