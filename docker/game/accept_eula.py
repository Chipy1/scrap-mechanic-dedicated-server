#!/usr/bin/env python3
"""Pre-accept the Scrap Mechanic EULA in Steam's local config.

Steam stores app EULA acceptance in
``userdata/<steamid>/config/localconfig.vdf``.  The key is
``<appid>_eula_<index>`` and the value is the accepted EULA version.  The
dedicated server is headless, so the dialog cannot be answered interactively.
This edits only that key and leaves the rest of the file untouched.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path


def find_matching_brace(text: str, open_index: int) -> int:
    depth = 0
    in_quote = False
    escaped = False
    index = open_index
    while index < len(text):
        char = text[index]
        if in_quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_quote = False
        else:
            if char == '"':
                in_quote = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return index
        index += 1
    return -1


def find_key_block(
    text: str, key: str, start: int = 0, end: int | None = None
) -> tuple[int, int, int] | None:
    limit = len(text) if end is None else end
    match = re.search(r'"%s"\s*\{' % re.escape(key), text[start:limit])
    if not match:
        return None
    match_start = start + match.start()
    match_end = start + match.end()
    open_index = text.find("{", match_start, match_end)
    if open_index < 0:
        return None
    close_index = find_matching_brace(text, open_index)
    if close_index < 0:
        return None
    return match_start, open_index, close_index


def add_eula(text: str, app_id: str, version: str) -> tuple[str, str]:
    apps = find_key_block(text, "apps")
    if apps is None:
        return text, "missing-apps"
    _, apps_open, apps_close = apps
    body_start = apps_open + 1
    body_end = apps_close
    body = text[body_start:body_end]
    app = find_key_block(body, app_id)
    if app is None:
        inserted = (
            f'\n\t\t\t\t\t"{app_id}"\n'
            "\t\t\t\t\t{\n"
            f'\t\t\t\t\t\t"{app_id}_eula_0"\t\t"{version}"\n'
            "\t\t\t\t\t}"
        )
        return text[:apps_close] + inserted + text[apps_close:], "created-app"

    app_start, app_open, app_close = app
    absolute_open = body_start + app_open
    absolute_close = body_start + app_close
    block = text[absolute_open : absolute_close + 1]
    if re.search(r'"%s_eula_\d+"\s+"[^"]*"' % re.escape(app_id), block):
        return text, "unchanged"
    # Match the indentation used by the application block's contents.
    inner_indent = "\t\t\t\t\t\t"
    match = re.search(r"\n([ \t]+)\"", block[1:])
    if match:
        inner_indent = match.group(1)
    inserted = (
        f'\n{inner_indent}"{app_id}_eula_0"\t\t"{version}"'
    )
    return text[:absolute_close] + inserted + text[absolute_close:], "changed"


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            "usage: accept_eula.py <localconfig.vdf> <appid> <eula-version>",
            file=sys.stderr,
        )
        return 2
    path = Path(argv[1])
    app_id = argv[2]
    version = argv[3]
    if not app_id.isdigit() or not version.isdigit():
        print("invalid app id or eula version", file=sys.stderr)
        return 2
    if not path.is_file():
        print(f"missing {path}", file=sys.stderr)
        return 3
    text = path.read_text(encoding="utf-8", errors="replace")
    updated, status = add_eula(text, app_id, version)
    if status in {"missing-apps", "unchanged"}:
        print(status)
        return 0 if status == "unchanged" else 4
    write_atomic(path, updated)
    print(status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
