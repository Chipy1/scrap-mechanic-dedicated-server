#!/usr/bin/env python3
"""Apply small, idempotent server-side Lua hardening after Steam updates.

Scrap Mechanic exposes survival cheat commands to the server without checking
whether the caller is actually the development host. A modified client can
therefore send `/unlimited` (or call `sv_setLimitedInventory(false)` directly)
and switch the whole server to the creative inventory. This patch keeps the
server in limited-inventory mode and kicks the offending player when the
network layer supplies one.

It also adds narrow checks around container access:
- a chest can only be opened from a reasonable distance;
- `sv_n_exchangeItem` can only touch the calling player's own inventory/hotbar.

The patches are intentionally narrow and fail soft: if upstream Lua changes,
startup logs a warning and the unmodified game still boots.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

MARKER = "-- sm-anticheat: block creative/unlimited inventory"
PERIODIC_MARKER = "-- sm-anticheat: periodic limited inventory enforcement"
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


PERIODIC_BLOCK = '''

-- sm-anticheat: periodic limited inventory enforcement
local sm_anticheat_server_onFixedUpdate = SurvivalGame.server_onFixedUpdate
function SurvivalGame.server_onFixedUpdate( self, timeStep )
\tif sm_anticheat_server_onFixedUpdate then
\t\tsm_anticheat_server_onFixedUpdate( self, timeStep )
\tend
\tself.sv = self.sv or {}
\tself.sv.smAntiCheatTimer = ( self.sv.smAntiCheatTimer or 0 ) + ( timeStep or 0 )
\tif self.sv.smAntiCheatTimer >= 1.0 then
\t\tself.sv.smAntiCheatTimer = 0
\t\tsm.game.setLimitedInventory( true )
\tend
end
'''

CHEST_MARKER = "-- sm-anticheat: reject remote chest access"
CHEST_SIGNATURE = "function Chest.sv_n_open( self, _, player )\n"
CHEST_GUARD = """
\t-- sm-anticheat: reject remote chest access
\tif not player or not player.character or not self.shape then
\t\treturn
\tend
\tlocal distance = ( player.character.worldPosition - self.shape.worldPosition ):length()
\tif distance > 8.0 then
\t\tprint( "[sm-anticheat] blocked remote chest access", player.name or "unknown", distance )
\t\tsm.game.kickPlayer( player )
\t\treturn
\tend
"""

BASE_MARKER = "-- sm-anticheat: reject foreign inventory transaction"
BASE_SIGNATURE = "function BasePlayer.sv_n_exchangeItem( self, params )\n"
BASE_GUARD = """
\t-- sm-anticheat: reject foreign inventory transaction
\tif not sm.exists( self.player ) then
\t\treturn
\tend
\tlocal ownInventory = self.player:getInventory()
\tlocal ownHotbar = self.player:getHotbar()
\tif params.playerInventory ~= ownInventory and params.playerInventory ~= ownHotbar then
\t\tprint( "[sm-anticheat] blocked foreign inventory transaction", self.player.name or "unknown" )
\t\tsm.game.kickPlayer( self.player )
\t\treturn
\tend
"""


def replace_file(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".sm-anticheat.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def apply_insert_patch(path: Path, marker: str, signature: str, guard: str) -> bool:
    if not path.is_file():
        print(f"[sm-anticheat] missing {path}", file=sys.stderr)
        return False

    text = path.read_text(encoding="utf-8")
    if marker in text:
        return True
    if text.count(signature) != 1:
        print(f"[sm-anticheat] patch site does not match {path}", file=sys.stderr)
        return False

    replace_file(path, text.replace(signature, signature + guard, 1))
    print(f"[sm-anticheat] patched {path}")
    return True


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
    if MARKER not in text:
        if text.count(CHAT_SIGNATURE) != 1 or OLD_LIMITED not in text:
            print("[sm-anticheat] patch sites do not match this game build", file=sys.stderr)
            return 1

        text = text.replace(
            CHAT_SIGNATURE,
            CHAT_SIGNATURE + CHAT_GUARD,
            1,
        )
        text = text.replace(OLD_LIMITED, NEW_LIMITED, 1)

    if PERIODIC_MARKER not in text:
        text = text.rstrip() + PERIODIC_BLOCK

    if MARKER in text and PERIODIC_MARKER in text and text == path.read_text(encoding="utf-8"):
        print("[sm-anticheat] already applied")
    else:
        replace_file(path, text)
        print(f"[sm-anticheat] patched {path}")

    ok = True
    ok = apply_insert_patch(
        game_dir / "Survival" / "Scripts" / "game" / "interactables" / "Chest.lua",
        CHEST_MARKER,
        CHEST_SIGNATURE,
        CHEST_GUARD,
    ) and ok
    ok = apply_insert_patch(
        game_dir / "Data" / "Scripts" / "game" / "BasePlayer.lua",
        BASE_MARKER,
        BASE_SIGNATURE,
        BASE_GUARD,
    ) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
