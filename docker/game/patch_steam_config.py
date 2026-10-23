#!/usr/bin/env python3
"""Add a Steam CompatToolMapping entry without a VDF library.

This intentionally only edits the small CompatToolMapping section. It does not
rewrite the rest of Steam's config, so comments/formatting are preserved.
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


def find_key_block(text: str, key: str, start: int = 0, end: int | None = None) -> tuple[int, int, int] | None:
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


def mapping_block(app_id: str, name: str, indent: str = "\t\t\t\t\t") -> str:
    inner = f"{indent}\t"
    return (
        f'\n{indent}"{app_id}"\n'
        f"{indent}{{\n"
        f'{inner}"name"\t\t"{name}"\n'
        f'{inner}"config"\t\t""\n'
        f'{inner}"priority"\t\t"250"\n'
        f"{indent}}}"
    )


def minimal_config(app_id: str, name: str) -> str:
    return (
        '"InstallConfigStore"\n'
        "{\n"
        '\t"Software"\n'
        "\t{\n"
        '\t\t"Valve"\n'
        "\t\t{\n"
        '\t\t\t"Steam"\n'
        "\t\t\t{\n"
        '\t\t\t\t"CompatToolMapping"\n'
        "\t\t\t\t{\n"
        + mapping_block(app_id, name, "\t\t\t\t\t")
        + "\n\t\t\t\t}\n"
        "\t\t\t}\n"
        "\t\t}\n"
        "\t}\n"
        "}\n"
    )


def add_mapping(text: str, app_id: str, name: str) -> tuple[str, str]:
    if not text.strip():
        return minimal_config(app_id, name), "created"

    mapping = find_key_block(text, "CompatToolMapping")
    if mapping is None:
        steam = find_key_block(text, "Steam")
        if steam is None:
            return text, "missing"
        _, _, steam_close = steam
        inserted = (
            '\n\t\t\t\t"CompatToolMapping"\n'
            "\t\t\t\t{"
            + mapping_block(app_id, name, "\t\t\t\t\t")
            + "\n\t\t\t\t}"
        )
        return text[:steam_close] + inserted + text[steam_close:], "changed"

    _, mapping_open, mapping_close = mapping
    body_start = mapping_open + 1
    body_end = mapping_close
    body = text[body_start:body_end]
    app_block = find_key_block(body, app_id)
    if app_block is not None:
        app_start, app_open, app_close = app_block
        absolute_open = body_start + app_open
        absolute_close = body_start + app_close
        block = text[absolute_open : absolute_close + 1]
        name_match = re.search(r'("name"\s+")[^"]*(")', block)
        if name_match:
            new_block = (
                block[: name_match.start()]
                + name_match.group(1)
                + name
                + name_match.group(2)
                + block[name_match.end() :]
            )
        else:
            new_block = (
                block[:1]
                + f'\n\t\t\t\t\t\t"name"\t\t"{name}"'
                + block[1:]
            )
        if new_block == block:
            return text, "unchanged"
        return text[:absolute_open] + new_block + text[absolute_close + 1 :], "changed"

    inserted = mapping_block(app_id, name)
    return text[:mapping_close] + inserted + text[mapping_close:], "changed"


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
            "usage: patch_steam_config.py <config.vdf> <appid> <tool-name>",
            file=sys.stderr,
        )
        return 2
    path = Path(argv[1])
    app_id = argv[2]
    name = argv[3]
    if not app_id.isdigit() or any(ch in name for ch in ('"', "\\", "\n")):
        print("invalid app id or tool name", file=sys.stderr)
        return 2
    if path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace")
    else:
        text = ""
    updated, status = add_mapping(text, app_id, name)
    if status == "missing":
        print("could not locate a Steam section in config.vdf", file=sys.stderr)
        return 3
    if status == "unchanged":
        print("unchanged")
        return 0
    write_atomic(path, updated)
    print(status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
