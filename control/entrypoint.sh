#!/usr/bin/env bash
set -Eeuo pipefail

# The compose volumes are normally created as root. Prepare only the mount
# roots, then run the application as the unprivileged uid used by the game
# container (1000).
for directory in /saves /backups /var/lib/sm-control; do
    mkdir -p "$directory" 2>/dev/null || true
    chown 1000:1000 "$directory" 2>/dev/null || true
done
chmod 0770 /saves /backups /var/lib/sm-control 2>/dev/null || true

exec gosu 1000:1000 "$@"
