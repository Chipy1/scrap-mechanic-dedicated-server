from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OperationStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(path), timeout=30, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA busy_timeout = 30000")
        with self._lock:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT
                )
                """
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        result: Any = None
        if row["result_json"]:
            try:
                result = json.loads(row["result_json"])
            except ValueError:
                result = None
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "result": result,
            "error": row["error"],
        }

    def create(
        self,
        kind: str,
        *,
        status: str = "queued",
        result: Any = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        operation_id = uuid.uuid4().hex
        now = _now_iso()
        result_json = json.dumps(result, default=str) if result is not None else None
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO operations
                    (id, kind, status, created_at, updated_at, result_json, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (operation_id, kind, status, now, now, result_json, error),
            )
            self._connection.commit()
            row = self._connection.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
        return self._row_to_dict(row)

    def update(
        self,
        operation_id: str,
        *,
        status: str,
        result: Any = None,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        result_json = json.dumps(result, default=str) if result is not None else None
        with self._lock:
            self._connection.execute(
                """
                UPDATE operations
                   SET status = ?, updated_at = ?, result_json = ?, error = ?
                 WHERE id = ?
                """,
                (status, _now_iso(), result_json, error, operation_id),
            )
            self._connection.commit()
            row = self._connection.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def get(self, operation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._lock:
            rows = list(
                self._connection.execute(
                    "SELECT * FROM operations ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            )
        return [self._row_to_dict(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = list(
                self._connection.execute(
                    "SELECT status, count(*) AS n FROM operations GROUP BY status"
                )
            )
        return {str(row["status"]): int(row["n"]) for row in rows}


class OperationManager:
    """Run blocking backup/restore work outside the event loop."""

    def __init__(self, store: OperationStore) -> None:
        self.store = store

    def submit(self, kind: str, function: Callable[[], Any]) -> dict[str, Any]:
        operation = self.store.create(kind, status="queued")
        thread = threading.Thread(
            target=self._run,
            args=(operation["id"], kind, function),
            name=f"sm-operation-{kind}",
            daemon=True,
        )
        thread.start()
        return operation

    def _run(self, operation_id: str, kind: str, function: Callable[[], Any]) -> None:
        self.store.update(operation_id, status="running")
        try:
            result = function()
        except Exception as exc:
            logger.exception("operation %s (%s) failed", operation_id, kind)
            self.store.update(
                operation_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        self.store.update(operation_id, status="completed", result=result)

    def get(self, operation_id: str) -> dict[str, Any] | None:
        return self.store.get(operation_id)
