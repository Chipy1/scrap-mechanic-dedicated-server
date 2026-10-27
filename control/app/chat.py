from __future__ import annotations

import os
import threading
from pathlib import Path

MAX_MESSAGE = 400
MAX_INBOX_LINES = 2000

_lock = threading.Lock()


def chat_dir(saves_dir: Path) -> Path:
    return saves_dir / ".sm-chat"


def _clean(text: str) -> str:
    text = text.replace("\r", " ").replace("\n", " ").strip()
    return "".join(ch for ch in text if ch == "\t" or ord(ch) >= 32)


def read_messages(saves_dir: Path, limit: int = 100) -> list[str]:
    limit = max(1, min(int(limit), 500))
    path = chat_dir(saves_dir) / "inbox.log"
    if not path.exists():
        return []
    with _lock:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    return [line.rstrip("\r\n") for line in lines[-limit:] if line.strip()]


def send_message(saves_dir: Path, text: str) -> None:
    text = _clean(text)
    if not text:
        raise ValueError("empty chat message")
    if len(text) > MAX_MESSAGE:
        raise ValueError(f"chat message is longer than {MAX_MESSAGE} characters")

    directory = chat_dir(saves_dir)
    directory.mkdir(parents=True, exist_ok=True)
    outbox = directory / "outbox.log"
    with _lock:
        # Keep the native reader from accumulating an unbounded command file.
        if outbox.exists() and outbox.stat().st_size > 256 * 1024:
            outbox.write_text("", encoding="utf-8")
        with outbox.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"[SERVER] {text}\n")
            handle.flush()
            os.fsync(handle.fileno())
