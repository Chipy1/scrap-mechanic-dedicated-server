from __future__ import annotations

from pathlib import Path

import pytest

from app.chat import read_messages, send_message


def test_chat_outbox_and_inbox(settings) -> None:
    directory = settings.saves_dir / ".sm-chat"
    send_message(settings.saves_dir, "hello from panel")
    outbox = (directory / "outbox.log").read_text(encoding="utf-8")
    assert outbox == "[SERVER] hello from panel\n"

    (directory / "inbox.log").write_text(
        "[12:00:00] <Player> hello\n[12:00:01] [SERVER] hi\n",
        encoding="utf-8",
    )
    assert read_messages(settings.saves_dir, 1) == ["[12:00:01] [SERVER] hi"]


def test_chat_rejects_empty_or_oversized(settings) -> None:
    with pytest.raises(ValueError):
        send_message(settings.saves_dir, "  \n ")
    with pytest.raises(ValueError):
        send_message(settings.saves_dir, "x" * 401)
