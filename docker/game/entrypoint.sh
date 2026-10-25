#!/usr/bin/env bash
set -Eeuo pipefail
umask 002

STEAM_ROOT="${STEAM_ROOT:-/home/steam/.local/share/Steam}"
SAVES_ROOT="${SM_SAVES_ROOT:-$STEAM_ROOT/steamapps/compatdata/387990/pfx/drive_c/users/steamuser/AppData/Roaming/Axolot Games/Scrap Mechanic/User}"
LOGS_ROOT="${SM_LOGS_ROOT:-$STEAM_ROOT/steamapps/common/Scrap Mechanic/Logs}"
ENTRYPOINT_USER="${ENTRYPOINT_USER:-/usr/local/lib/sm/entrypoint-user.sh}"

mkdir -p "$STEAM_ROOT" "$SAVES_ROOT" "$LOGS_ROOT"

# Only walk each volume once. Subsequent starts only ensure the mount root is
# writable by the unprivileged Steam user.
for directory in "$STEAM_ROOT" "$SAVES_ROOT" "$LOGS_ROOT"; do
    marker="$directory/.sm-volume-initialized"
    if [[ ! -e "$marker" ]]; then
        chown -R steam:steam "$directory" 2>/dev/null || true
        touch "$marker" 2>/dev/null || true
    fi
    chown steam:steam "$directory" 2>/dev/null || true
    if [[ -e "$marker" ]]; then
        chown steam:steam "$marker" 2>/dev/null || true
    fi
done

# Repairs files copied into the volume by root (docker cp, manual restore,
# old seeds). A root-owned .db is readable but not writable by the game, which
# makes Scrap Mechanic fail with "attempt to write a readonly database".
find "$SAVES_ROOT" -xdev -not -user steam -exec chown steam:steam {} + 2>/dev/null || true

exec gosu steam:steam "$ENTRYPOINT_USER" "$@"
