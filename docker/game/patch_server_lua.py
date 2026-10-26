#!/usr/bin/env python3
"""Apply small, idempotent server-side Lua hardening after Steam updates.

Scrap Mechanic exposes survival cheat commands to the server without checking
whether the caller is actually the development host. A modified client can
therefore send `/unlimited` (or call `sv_setLimitedInventory(false)` directly)
and switch the whole server to the creative inventory. This patch keeps the
server in limited-inventory mode and kicks the offending player when the
network layer supplies one.

It also adds a narrow chest-distance check.

The patches are intentionally narrow and fail soft: if upstream Lua changes,
startup logs a warning and the unmodified game still boots.
"""

from __future__ import annotations

import os
import re
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

GIVE_MARKER = "-- sm-anticheat: reject client item grants"
GIVE_SIGNATURE = "function SurvivalGame.sv_giveItem( self, params )\n"
GIVE_GUARD = """
\t-- sm-anticheat: reject client item grants
\tprint( "[sm-anticheat] blocked item grant", params and params.item or "unknown", params and params.quantity or "?" )
\tif params and params.player then
\t\tsm.game.kickPlayer( params.player )
\tend
\treturn
"""

AUDIT_MARKER = "-- sm-anticheat: inventory change audit v2"
AUDIT_SIGNATURE = "function SurvivalPlayer.server_onInventoryChanges( self, container, changes )\n"
AUDIT_GUARD = """
\t-- sm-anticheat: inventory change audit
\tif container == self.player:getInventory() and changes then
\t\tfor _, item in ipairs( changes ) do
\t\t\tif item.difference > 0 then
\t\t\t\tsm.log.error( "[sm-anticheat] inventory add " .. tostring( self.player.name or "unknown" ) .. " " .. tostring( item.uuid ) .. " " .. tostring( item.difference ) )
\t\t\tend
\t\tend
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


def revert(game_dir: Path) -> int:
    """Remove the known anti-cheat insertions and restore stock Lua bodies."""
    path = game_dir / "Survival" / "Scripts" / "game" / "SurvivalGame.lua"
    if not path.is_file():
        print(f"[sm-anticheat] missing {path}", file=sys.stderr)
        return 1

    text = path.read_text(encoding="utf-8")
    text = text.replace(CHAT_GUARD, "")
    text = text.replace(PERIODIC_BLOCK, "")
    text = text.replace(NEW_LIMITED, OLD_LIMITED)
    text = text.replace(GIVE_GUARD, "")
    if text != path.read_text(encoding="utf-8"):
        replace_file(path, text)
        print(f"[sm-anticheat] reverted {path}")

    chest = game_dir / "Survival" / "Scripts" / "game" / "interactables" / "Chest.lua"
    if chest.is_file():
        current = chest.read_text(encoding="utf-8")
        while CHEST_GUARD in current:
            current = current.replace(CHEST_GUARD, "")
        if current != chest.read_text(encoding="utf-8"):
            replace_file(chest, current)
            print(f"[sm-anticheat] reverted {chest}")

    survival_player = game_dir / "Survival" / "Scripts" / "game" / "SurvivalPlayer.lua"
    if survival_player.is_file():
        current = survival_player.read_text(encoding="utf-8")
        pattern = re.compile(
            r"\n\t-- sm-anticheat: inventory change audit\n.*?\n\tQuestManager\.Sv_SendEvent",
            re.S,
        )
        current = pattern.sub("\n\tQuestManager.Sv_SendEvent", current, count=1)
        current = "\n".join(
            line for line in current.splitlines() if "sm-anticheat" not in line
        ) + "\n"
        if current != survival_player.read_text(encoding="utf-8"):
            replace_file(survival_player, current)
            print(f"[sm-anticheat] reverted {survival_player}")
    return 0


def main() -> int:
    args = sys.argv[1:]
    game_dir = Path(args[-1] if args and args[-1] != "--revert" else os.environ.get("GAME_DIR", ""))
    if not game_dir:
        print("[sm-anticheat] GAME_DIR is not set", file=sys.stderr)
        return 1
    if "--revert" in args:
        return revert(game_dir)

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
        path,
        GIVE_MARKER,
        GIVE_SIGNATURE,
        GIVE_GUARD,
    ) and ok
    ok = apply_insert_patch(
        game_dir / "Survival" / "Scripts" / "game" / "interactables" / "Chest.lua",
        CHEST_MARKER,
        CHEST_SIGNATURE,
        CHEST_GUARD,
    ) and ok
    ok = apply_insert_patch(
        game_dir / "Survival" / "Scripts" / "game" / "SurvivalPlayer.lua",
        AUDIT_MARKER,
        AUDIT_SIGNATURE,
        AUDIT_GUARD,
    ) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
