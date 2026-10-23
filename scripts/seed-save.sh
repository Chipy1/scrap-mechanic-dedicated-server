#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="${1:-$HOME/.local/share/Steam/steamapps/compatdata/387990/pfx/drive_c/users/steamuser/AppData/Roaming/Axolot Games/Scrap Mechanic/User/User_76561198095993293/Save/something-test.db}"

[[ -f "$ROOT/.env" ]] || { echo "missing $ROOT/.env; run scripts/import-creds.sh first" >&2; exit 1; }
[[ -f "$SOURCE" ]] || { echo "save not found: $SOURCE" >&2; exit 1; }

SAVE_NAME="$(python3 - "$ROOT/.env" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
values = {}
for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    values[key.strip()] = value
print(values.get("SAVE_NAME", "something-test"))
PY
)"
SAVE_FILENAME="${SAVE_NAME%.db}.db"
python3 "$ROOT/docker/game/validate_save.py" "$SOURCE" >/dev/null \
    || { echo "source save failed validation: $SOURCE" >&2; exit 1; }

PROJECT="$(cd "$ROOT" && docker compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin).get("name") or "")')"
PROJECT="${PROJECT:-scrap-mechanic}"
VOLUME="${PROJECT}_sm_seed"

docker volume create \
    --label "com.docker.compose.project=$PROJECT" \
    --label "com.docker.compose.volume=sm_seed" \
    "$VOLUME" >/dev/null
docker run --rm \
    -e "SAVE_FILENAME=$SAVE_FILENAME" \
    -v "$VOLUME:/seed" \
    -v "$SOURCE:/src/save.db:ro" \
    ubuntu:22.04 \
    bash -c 'set -euo pipefail; cp /src/save.db "/seed/$SAVE_FILENAME"; chmod 0644 "/seed/$SAVE_FILENAME"; ls -l "/seed/$SAVE_FILENAME"'

echo "seeded volume $VOLUME with $SAVE_FILENAME"
