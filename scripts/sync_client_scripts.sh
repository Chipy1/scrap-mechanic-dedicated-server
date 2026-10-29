#!/usr/bin/env bash
# Pull the dedicated server's Survival/Scripts tree into the local client.
#
# The stock game does not push Lua files to clients, so the client must use
# the same script files as the server.  This script is one-way and
# server-authoritative: changed/new files are copied, files removed on the
# server are removed locally, and every overwritten/deleted local file is
# backed up first.
set -Eeuo pipefail

SERVER="${SM_SYNC_SERVER:-root@192.168.50.41}"
REMOTE_DIR="${SM_SYNC_REMOTE_DIR:-/var/lib/docker/volumes/scrap-mechanic_sm_steam/_data/steamapps/common/Scrap Mechanic/Survival/Scripts}"
LOCAL_DIR="${SM_SYNC_LOCAL_DIR:-/mnt/p6/Steam/steamapps/common/Scrap Mechanic/Survival/Scripts}"
STATE_DIR="${SM_SYNC_STATE_DIR:-$HOME/.local/share/sm-script-sync}"
BACKUP_ROOT="${SM_SYNC_BACKUP_ROOT:-$STATE_DIR/backups}"

DRY_RUN=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --force) FORCE=1 ;;
        -h|--help)
            sed -n '1,20p' "$0"
            exit 0
            ;;
        *)
            echo "unknown argument: $arg" >&2
            exit 2
            ;;
    esac
done

command -v rsync >/dev/null || { echo "rsync is required" >&2; exit 1; }
command -v ssh >/dev/null || { echo "ssh is required" >&2; exit 1; }
[[ -d "$LOCAL_DIR" ]] || { echo "local scripts directory not found: $LOCAL_DIR" >&2; exit 1; }

if pgrep -f 'ScrapMechanic\.exe' >/dev/null 2>&1 && [[ "$FORCE" -eq 0 ]]; then
    echo "Scrap Mechanic is running; close it before syncing (or pass --force)." >&2
    exit 1
fi

mkdir -p "$STATE_DIR" "$BACKUP_ROOT"
stamp="$(date +%Y%m%dT%H%M%SZ)"
backup_dir="$BACKUP_ROOT/$stamp"
mkdir -p "$backup_dir"

rsync_args=(
    -r
    --checksum
    --no-perms
    --no-times
    --delete
    --backup
    --backup-dir="$backup_dir"
    --itemize-changes
    --exclude='*.bak'
    --exclude='*.bak-*'
    --exclude='*.backup*'
    --exclude='*.before-*'
    --exclude='*.orig'
    --exclude='*.rej'
    --exclude='*.disabled'
    --exclude='.DS_Store'
)
if [[ "$DRY_RUN" -eq 1 ]]; then
    rsync_args+=(--dry-run)
fi

export RSYNC_RSH="${RSYNC_RSH:-ssh -o BatchMode=yes -o ConnectTimeout=10}"

echo "Server: $SERVER:$REMOTE_DIR"
echo "Client: $LOCAL_DIR"
if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "Mode:   dry run"
fi

changes_file="$STATE_DIR/changes-$stamp.txt"
set +e
rsync "${rsync_args[@]}" -s --out-format='%i %n' \
    "$SERVER:$REMOTE_DIR/" "$LOCAL_DIR/" | tee "$changes_file"
rc=${PIPESTATUS[0]}
set -e
if [[ "$rc" -ne 0 ]]; then
    echo "rsync failed with exit code $rc" >&2
    rmdir "$backup_dir" 2>/dev/null || true
    exit "$rc"
fi

# Remove the empty backup directory when nothing was overwritten/deleted.
if [[ -z "$(find "$backup_dir" -mindepth 1 -print -quit)" ]]; then
    rmdir "$backup_dir" 2>/dev/null || true
    backup_dir=""
fi

count="$(grep -Ec '^([<>ch]f|\*deleting)' "$changes_file" || true)"
if [[ "$count" -eq 0 ]]; then
    echo "No script changes."
else
    echo "Changed files: $count"
    if [[ -n "$backup_dir" ]]; then
        echo "Backup: $backup_dir"
    fi
fi
if [[ "$DRY_RUN" -eq 0 ]]; then
    find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +30 \
        -exec rm -rf {} + 2>/dev/null || true
fi
