#!/usr/bin/env python3
"""Fix the stock BaseUnit death loop on dedicated servers.

Scrap Mechanic's BaseUnit.sv_onDeath() loops over every connected player and
calls player:getCharacter() without a nil check. On a dedicated server a
player can exist without a character (for example while joining or after a
disconnect), so the loop throws:

    BaseUnit.lua:96: attempt to index a nil value

The error aborts sv_onDeath before RobotOnDestroy(self), which means raiders
are never removed from RaidManager and the raid progress bar never decreases.

This patch ignores players without characters. It is intentionally tiny and
idempotent. Delete the game cache after applying it so the game recompiles the
Lua bundle.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

MARKER = "-- sm-raid-fix: ignore players without a character"

STOCK = """\t\tfor _, player in ipairs(sm.player.getAllPlayers()) do
\t\t\tlocal playerWorld = player:getCharacter():getWorld()
\t\t\tif unitWorld == playerWorld then
\t\t\t\tlocal distSqrd = (player:getCharacter():getWorldPosition() - deathPos):length2()
\t\t\t\tif distSqrd < sqrdAchievementRange then
\t\t\t\t\ttable.insert(playersInRange, player.id)
\t\t\t\tend
\t\t\tend
\t\tend"""

FIXED = """\t\t-- sm-raid-fix: ignore players without a character
\t\tfor _, player in ipairs(sm.player.getAllPlayers()) do
\t\t\tlocal playerCharacter = player:getCharacter()
\t\t\tif playerCharacter then
\t\t\t\tlocal playerWorld = playerCharacter:getWorld()
\t\t\t\tif unitWorld == playerWorld then
\t\t\t\t\tlocal distSqrd = (playerCharacter:getWorldPosition() - deathPos):length2()
\t\t\t\t\tif distSqrd < sqrdAchievementRange then
\t\t\t\t\t\ttable.insert(playersInRange, player.id)
\t\t\t\t\tend
\t\t\t\tend
\t\t\tend
\t\tend"""


def main() -> int:
    if len(sys.argv) < 2:
        print("[sm-raid-fix] usage: patch_server_raid.py GAME_DIR", file=sys.stderr)
        return 2

    path = Path(sys.argv[1]) / "Survival" / "Scripts" / "game" / "units" / "BaseUnit.lua"
    if not path.is_file():
        print(f"[sm-raid-fix] missing {path}", file=sys.stderr)
        return 1

    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if MARKER in text:
        print(f"[sm-raid-fix] already applied: {path}")
        return 0
    if text.count(STOCK) != 1:
        print(f"[sm-raid-fix] patch site does not match {path}", file=sys.stderr)
        return 1

    temporary = path.with_name(path.name + ".sm-raid-fix.tmp")
    temporary.write_text(text.replace(STOCK, FIXED, 1), encoding="utf-8")
    os.replace(temporary, path)
    print(f"[sm-raid-fix] patched {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
