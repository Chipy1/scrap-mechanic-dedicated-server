#!/usr/bin/env python3
"""Validate a Scrap Mechanic SQLite save. Used by the game entrypoint."""
from __future__ import annotations

import sqlite3
import sys
import urllib.parse
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print("usage: validate_save.py <save.db> [--integrity]", file=sys.stderr)
        return 2
    path = Path(argv[1])
    integrity = len(argv) == 3 and argv[2] == "--integrity"
    if not path.is_file():
        print(f"missing save: {path}", file=sys.stderr)
        return 1
    if path.stat().st_size <= 0:
        print(f"empty save: {path}", file=sys.stderr)
        return 1
    # Read-write is required so SQLite can roll back a hot journal left by a
    # previous crash before quick_check runs. No game process is running yet.
    uri = "file:" + urllib.parse.quote(str(path.resolve()), safe="/") + "?mode=rw"
    connection = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        pragma = "integrity_check" if integrity else "quick_check"
        checks = [str(row[0]) for row in connection.execute(f"PRAGMA {pragma}")]
        if not checks or any(value.lower() != "ok" for value in checks):
            print(f"{pragma} failed: {'; '.join(checks)}", file=sys.stderr)
            return 1
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND lower(name)='game'"
        ).fetchone():
            print("Game table not found", file=sys.stderr)
            return 1
        columns = {
            str(row[1]).lower()
            for row in connection.execute('PRAGMA table_info("Game")')
        }
        if "gametick" not in columns or "savegameversion" not in columns:
            print("Game table lacks gametick/savegameversion", file=sys.stderr)
            return 1
        row = connection.execute('SELECT * FROM "Game" LIMIT 1').fetchone()
        if row is None:
            print("Game table is empty", file=sys.stderr)
            return 1
        print(
            f"ok size={path.stat().st_size} "
            f"gametick={row['gametick']} savegameversion={row['savegameversion']}"
        )
        return 0
    except Exception as exc:
        print(f"invalid SQLite save: {exc}", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
