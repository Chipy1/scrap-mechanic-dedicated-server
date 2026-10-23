#!/usr/bin/env python3
"""Read the SteamID for an account from Steam's loginusers.vdf."""
from __future__ import annotations

import re
import sys
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


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: steam_id.py <account> <loginusers.vdf>", file=sys.stderr)
        return 2
    account = argv[1]
    path = Path(argv[2])
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return 1

    users = re.search(r'"users"\s*\{', text)
    candidates: list[tuple[str, str]] = []
    if users:
        open_index = text.find("{", users.start(), users.end())
        close_index = find_matching_brace(text, open_index)
        if close_index > open_index:
            body = text[open_index + 1 : close_index]
            for match in re.finditer(r'"(\d{17})"\s*\{', body):
                user_open = body.find("{", match.start(), match.end())
                user_close = find_matching_brace(body, user_open)
                if user_close < 0:
                    continue
                block = body[user_open + 1 : user_close]
                candidates.append((match.group(1), block))
    else:
        for match in re.finditer(r'"(\d{17})"\s*\{', text):
            open_index = text.find("{", match.start(), match.end())
            close_index = find_matching_brace(text, open_index)
            if close_index > open_index:
                candidates.append((match.group(1), text[open_index + 1 : close_index]))

    for steam_id, block in candidates:
        account_match = re.search(r'"AccountName"\s*"([^"]*)"', block)
        if account_match and account_match.group(1) == account:
            print(steam_id)
            return 0
    if candidates:
        print(candidates[0][0])
        return 0
    print(f"no SteamID found for {account}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
