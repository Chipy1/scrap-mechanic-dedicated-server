#!/usr/bin/env bash
# Sync server scripts, then launch the local Scrap Mechanic client.
set -Eeuo pipefail

SYNC="${SM_SYNC_BIN:-$HOME/Documents/Projects/SM-dedicated-server/scripts/sync_client_scripts.sh}"

if pgrep -f 'ScrapMechanic\.exe' >/dev/null 2>&1; then
    echo "Scrap Mechanic is already running."
    exit 0
fi

if ! "$SYNC"; then
    echo "Script sync failed; not launching the game." >&2
    exit 1
fi

exec steam -applaunch 387990 "$@"
