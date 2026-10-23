from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import Settings
from .saves import (
    InvalidSave,
    SaveNotFound,
    SaveResolver,
    copy_sqlite_database,
    sha256_file,
    validate_save,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - Linux is the deployment target
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

BACKUP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,160}$")


class BackupError(Exception):
    pass


class BackupNotFound(BackupError):
    pass


class BackupBusy(BackupError):
    pass


class BackupInvalid(BackupError):
    pass


class RestoreBlocked(BackupError):
    pass


class RestoreConfirmationRequired(BackupError):
    pass


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return (slug or "save")[:48]


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def validate_backup_id(backup_id: str) -> str:
    if not BACKUP_ID_RE.fullmatch(backup_id) or ".." in backup_id:
        raise BackupNotFound("invalid backup id")
    return backup_id


class BackupManager:
    def __init__(
        self,
        settings: Settings,
        resolver: SaveResolver,
        monitor: Any | None = None,
    ) -> None:
        self.settings = settings
        self.resolver = resolver
        self.monitor = monitor
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.Lock()
        self._last_signature: tuple[int, int] | None = None
        self.backup_total = 0
        self.backup_failures = 0
        self.restore_total = 0
        self.last_success_epoch: float | None = None
        self.last_duration_seconds: float | None = None
        self.last_restore_at: str | None = None

    @property
    def backups_dir(self) -> Path:
        return self.settings.backups_dir

    @contextmanager
    def lock(self, *, blocking: bool = False) -> Iterator[None]:
        if not self._thread_lock.acquire(blocking=blocking):
            raise BackupBusy("another backup or restore is in progress")
        lock_fd: int | None = None
        try:
            self.backups_dir.mkdir(parents=True, exist_ok=True)
            lock_path = self.backups_dir / ".lock"
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            if fcntl is not None:
                flags = fcntl.LOCK_EX
                if not blocking:
                    flags |= fcntl.LOCK_NB
                try:
                    fcntl.flock(lock_fd, flags)
                except BlockingIOError as exc:
                    raise BackupBusy("another process is backing up the save") from exc
            yield
        finally:
            if lock_fd is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
            self._thread_lock.release()

    def create_backup(
        self, trigger: str = "manual", *, only_if_changed: bool = False
    ) -> dict[str, Any] | None:
        with self.lock(blocking=False):
            return self._create_backup_locked(trigger, only_if_changed=only_if_changed)

    def _create_backup_locked(
        self,
        trigger: str,
        *,
        only_if_changed: bool = False,
        apply_retention: bool = True,
    ) -> dict[str, Any] | None:
        started = time.monotonic()
        source = self.resolver.resolve()
        try:
            stat = source.path.stat()
        except OSError as exc:
            raise InvalidSave(f"cannot stat save: {exc}") from exc
        signature = (stat.st_mtime_ns, stat.st_size)
        if only_if_changed and self._last_signature == signature:
            return None

        validation = validate_save(source.path, integrity=False)
        if not validation.valid:
            self.backup_failures += 1
            raise InvalidSave(validation.error or "save validation failed")

        now = datetime.now(timezone.utc)
        backup_id = (
            f"{now.strftime('%Y%m%dT%H%M%SZ')}_"
            f"{_safe_slug(self.settings.save_name)}_"
            f"{uuid.uuid4().hex[:6]}"
        )
        temp_dir = Path(
            tempfile.mkdtemp(prefix=".tmp-", dir=str(self.backups_dir))
        )
        final_dir = self.backups_dir / backup_id
        try:
            backup_file = temp_dir / source.filename
            copy_sqlite_database(source.path, backup_file)
            copied_validation = validate_save(backup_file, integrity=True)
            if not copied_validation.valid:
                raise BackupInvalid(
                    copied_validation.error or "backup validation failed"
                )
            checksum = sha256_file(backup_file)
            duration = round(time.monotonic() - started, 3)
            manifest: dict[str, Any] = {
                "schema": 1,
                "id": backup_id,
                "created_at": now.isoformat().replace("+00:00", "Z"),
                "trigger": trigger,
                "save_name": self.settings.save_name,
                "filename": source.filename,
                "is_custom_game": self.settings.is_custom_game,
                "source_relative_path": self.resolver.relative_path(source.path),
                "source_size_bytes": stat.st_size,
                "backup_size_bytes": backup_file.stat().st_size,
                "sha256": checksum,
                "gametick": copied_validation.gametick,
                "savegameversion": copied_validation.savegameversion,
                "mods_sha256": copied_validation.mods_sha256,
                "table_count": copied_validation.table_count,
                "duration_seconds": duration,
            }
            manifest_path = temp_dir / "manifest.json"
            with manifest_path.open("w", encoding="utf-8") as handle:
                json.dump(manifest, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_dir(temp_dir)
            if final_dir.exists():
                raise BackupInvalid(f"backup id collision: {backup_id}")
            os.replace(temp_dir, final_dir)
            _fsync_dir(self.backups_dir)
            self.backup_total += 1
            self.last_success_epoch = time.time()
            self.last_duration_seconds = duration
            self._last_signature = signature
            logger.info(
                "created backup %s (%s bytes, %s)",
                backup_id,
                manifest["backup_size_bytes"],
                trigger,
            )
            if apply_retention:
                self._apply_retention_locked()
            return manifest
        except Exception:
            self.backup_failures += 1
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    def _manifest_for_dir(self, directory: Path) -> dict[str, Any] | None:
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            return None
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("id") != directory.name:
            return None
        if not isinstance(data.get("filename"), str):
            return None
        return data

    def list_backups(self) -> list[dict[str, Any]]:
        if not self.backups_dir.exists():
            return []
        result: list[dict[str, Any]] = []
        try:
            directories = [p for p in self.backups_dir.iterdir() if p.is_dir()]
        except OSError:
            return []
        for directory in directories:
            if directory.name.startswith("."):
                continue
            try:
                validate_backup_id(directory.name)
            except BackupNotFound:
                continue
            manifest = self._manifest_for_dir(directory)
            if manifest is not None:
                result.append(manifest)
        result.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return result

    def get_backup(self, backup_id: str) -> dict[str, Any]:
        validate_backup_id(backup_id)
        directory = self.backups_dir / backup_id
        if not directory.is_dir():
            raise BackupNotFound(f"backup not found: {backup_id}")
        manifest = self._manifest_for_dir(directory)
        if manifest is None:
            raise BackupNotFound(f"backup manifest is missing or invalid: {backup_id}")
        return manifest

    def backup_file_path(self, backup_id: str) -> Path:
        manifest = self.get_backup(backup_id)
        directory = (self.backups_dir / backup_id).resolve()
        path = (directory / str(manifest["filename"])).resolve()
        if directory not in path.parents:
            raise BackupInvalid("backup file path escapes its directory")
        if not path.is_file():
            raise BackupNotFound(f"backup database is missing: {backup_id}")
        return path

    def verify_backup(self, backup_id: str) -> dict[str, Any]:
        manifest = self.get_backup(backup_id)
        path = self.backup_file_path(backup_id)
        checksum = sha256_file(path)
        if checksum != manifest.get("sha256"):
            raise BackupInvalid(f"checksum mismatch for backup {backup_id}")
        validation = validate_save(path, integrity=True)
        if not validation.valid:
            raise BackupInvalid(validation.error or "backup is not a valid save")
        return {
            "backup": manifest,
            "checksum_ok": True,
            "validation": validation.to_dict(),
        }

    def restore(
        self,
        backup_id: str,
        *,
        confirm_server_stopped: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        with self.lock(blocking=True):
            return self._restore_locked(
                backup_id,
                confirm_server_stopped=confirm_server_stopped,
                force=force,
            )

    def _restore_locked(
        self,
        backup_id: str,
        *,
        confirm_server_stopped: bool,
        force: bool,
    ) -> dict[str, Any]:
        manifest = self.get_backup(backup_id)
        if manifest.get("filename") != self.settings.save_filename:
            raise RestoreBlocked(
                "backup belongs to another save; change SAVE_NAME first"
            )
        if self.monitor is not None and self.monitor.is_running():
            raise RestoreBlocked("server is still running; stop it before restore")
        if (
            self.settings.require_server_stopped
            and not confirm_server_stopped
            and not force
        ):
            raise RestoreConfirmationRequired(
                "confirm_server_stopped=true is required for restore"
            )
        verified = self.verify_backup(backup_id)
        source = self.backup_file_path(backup_id)
        target = self.resolver.path_from_relative(str(manifest["source_relative_path"]))
        if target.name != manifest["filename"]:
            raise BackupInvalid("backup target filename does not match manifest")

        safety: dict[str, Any] | None = None
        if target.exists():
            try:
                safety = self._create_backup_locked(
                    "pre-restore", apply_retention=False
                )
            except (SaveNotFound, InvalidSave) as exc:
                logger.warning("safety backup before restore failed: %s", exc)

        target.parent.mkdir(parents=True, exist_ok=True)
        temp_target = target.with_name(
            f".{target.name}.restore-{uuid.uuid4().hex[:8]}"
        )
        try:
            shutil.copyfile(source, temp_target)
            with temp_target.open("rb") as handle:
                os.fsync(handle.fileno())
            os.chmod(temp_target, 0o660)
            os.replace(temp_target, target)
            _fsync_dir(target.parent)
        except Exception:
            temp_target.unlink(missing_ok=True)
            raise
        for suffix in ("-wal", "-shm", "-journal"):
            target.with_name(target.name + suffix).unlink(missing_ok=True)
        restored_validation = validate_save(target, integrity=True)
        if not restored_validation.valid:
            if safety is not None:
                safety_source = self.backup_file_path(str(safety["id"]))
                shutil.copyfile(safety_source, target)
                os.chmod(target, 0o660)
            raise BackupInvalid(
                f"restored save failed validation: {restored_validation.error}"
            )
        self.restore_total += 1
        self.last_restore_at = _iso_now()
        logger.info("restored backup %s to %s", backup_id, target)
        return {
            "restored": True,
            "backup": verified["backup"],
            "target": str(target),
            "relative_path": self.resolver.relative_path(target),
            "safety_backup": safety,
            "validation": restored_validation.to_dict(),
        }

    def delete_backup(self, backup_id: str) -> None:
        with self.lock(blocking=False):
            manifest = self.get_backup(backup_id)
            directory = self.backups_dir / str(manifest["id"])
            shutil.rmtree(directory)
            _fsync_dir(self.backups_dir)

    def _apply_retention_locked(self) -> list[str]:
        backups = self.list_backups()
        if not backups:
            return []
        keep: set[str] = {str(backups[0]["id"])}
        today = datetime.now(timezone.utc).date()

        day_cutoff = today - timedelta(days=max(0, self.settings.backup_retention_days - 1))
        week_cutoff = today - timedelta(weeks=max(0, self.settings.backup_retention_weeks - 1))
        seen_days: set[Any] = set()
        seen_weeks: set[Any] = set()
        for manifest in backups:
            created = _parse_created_at(manifest.get("created_at"))
            if created is None:
                continue
            date = created.date()
            if self.settings.backup_retention_days > 0 and date >= day_cutoff:
                if date not in seen_days:
                    keep.add(str(manifest["id"]))
                    seen_days.add(date)
            if self.settings.backup_retention_weeks > 0 and date >= week_cutoff:
                week = date.isocalendar()[:2]
                if week not in seen_weeks:
                    keep.add(str(manifest["id"]))
                    seen_weeks.add(week)

        deleted: list[str] = []
        for manifest in backups:
            backup_id = str(manifest["id"])
            if backup_id in keep:
                continue
            directory = self.backups_dir / backup_id
            try:
                shutil.rmtree(directory)
                deleted.append(backup_id)
            except OSError as exc:
                logger.warning("cannot delete old backup %s: %s", backup_id, exc)
        if deleted:
            _fsync_dir(self.backups_dir)
            logger.info("retention deleted %d backup(s)", len(deleted))
        return deleted

    def stats(self) -> dict[str, Any]:
        backups = self.list_backups()
        return {
            "count": len(backups),
            "last": backups[0] if backups else None,
            "backup_total": self.backup_total,
            "backup_failures": self.backup_failures,
            "restore_total": self.restore_total,
            "last_success_epoch": self.last_success_epoch,
            "last_success_at": (
                datetime.fromtimestamp(self.last_success_epoch, timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
                if self.last_success_epoch
                else None
            ),
            "last_duration_seconds": self.last_duration_seconds,
            "last_restore_at": self.last_restore_at,
        }


def _parse_created_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
