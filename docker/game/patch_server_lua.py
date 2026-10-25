#!/usr/bin/env python3
"""Apply small, idempotent server-side Lua hardening after Steam updates.

Scrap Mechanic exposes the survival cheat commands to the server without
checking whether the caller is actually the development host. A modified client
can therefore send `/unlimited` (or call `sv_setLimitedInventory(false)`
directly) and switch the whole server to the creative inventory. This patch
keeps the server in limited-inventory mode and kicks the offending player when
the network layer supplies one.

The patch is intentionally narrow and fails soft: if the upstream Lua changes,
startup logs a warning and the unmodified game still boots.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

MARKER = "-- sm-anticheat: block creative/unlimited inventory"
CHAT_SIGNATURE = "function SurvivalGame.sv_onChatCommand( self, params, player )\n"
CHAT_GUARD = """
\t-- sm-anticheat: block creative/unlimited inventory
\tif params[1] == "/unlimited" or params[1] == "/limited" then
\t\tprint( "[sm-anticheat] blocked inventory command", params[1], player and player.name or "unknown" )
\t\tsm.game.setLimitedInventory( true )
\t\tif player then
\t\t\tsm.game.kickPlayer( player )
\t\tend
\t\treturn
\tend
"""

OLD_LIMITED = '''function SurvivalGame.sv_setLimitedInventory( self, state )
\tsm.game.setLimitedInventory( state )
\tself.network:sendToClients( "client_showMessage", ( state and "Limited inventory" or "Unlimited inventory"  ) )
end'''

NEW_LIMITED = '''function SurvivalGame.sv_setLimitedInventory( self, state, player )
\t-- sm-anticheat: block creative/unlimited inventory
\tif state ~= true then
\t\tprint( "[sm-anticheat] blocked unlimited inventory", player and player.name or "unknown" )
\t\tsm.game.setLimitedInventory( true )
\t\tif player then
\t\t\tsm.game.kickPlayer( player )
\t\tend
\t\tself.network:sendToClients( "client_showMessage", "Limited inventory" )
\t\treturn
\tend
\tsm.game.setLimitedInventory( true )
\tself.network:sendToClients( "client_showMessage", "Limited inventory" )
end'''


def main() -> int:
    game_dir = Path(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GAME_DIR", ""))
    if not game_dir:
        print("[sm-anticheat] GAME_DIR is not set", file=sys.stderr)
        return 1

    path = game_dir / "Survival" / "Scripts" / "game" / "SurvivalGame.lua"
    if not path.is_file():
        print(f"[sm-anticheat] missing {path}", file=sys.stderr)
        return 1

    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        print("[sm-anticheat] already applied")
        return 0

    if text.count(CHAT_SIGNATURE) != 1 or OLD_LIMITED not in text:
        print("[sm-anticheat] patch sites do not match this game build", file=sys.stderr)
        return 1

    text = text.replace(
        CHAT_SIGNATURE,
        CHAT_SIGNATURE + CHAT_GUARD,
        1,
    )
    text = text.replace(OLD_LIMITED, NEW_LIMITED, 1)

    temporary = path.with_name(path.name + ".sm-anticheat.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    print(f"[sm-anticheat] patched {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
