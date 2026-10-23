# Scrap Mechanic dedicated server

Steam-launched Scrap Mechanic dedicated server with CPU rendering, a small REST
API, Prometheus metrics, and SQLite backups. It runs without a GPU or `/dev/dri`.

## Requirements

- Docker Engine with Compose v2.
- Docker must permit the Steam runtime's user namespaces. Compose sets
  `seccomp:unconfined` and `apparmor:unconfined`; on Ubuntu 24.04 hosts,
  unprivileged user namespaces must also be enabled.
- A Steam account that owns Scrap Mechanic (`387990`).
- **Steam Guard disabled** on that account. There is no interactive Guard flow;
  login will time out if a code is required.
- An **initialized** Scrap Mechanic SQLite save. The server never creates a new
  world. The save must contain a valid `Game` table and pass SQLite
  `quick_check`; it is copied into `sm_seed` once.
- `SAVE_NAME` and `IS_CUSTOM_GAME` must match the save (`Save` vs `Save/Custom`).

Steam Cloud is intentionally unused; the server uses the save volume supplied
by Compose.

## Setup

```sh
scripts/import-creds.sh                 # reads ~/Documents/SMServer_creds.txt
scripts/seed-save.sh /path/to/initialized-save.db
docker compose build
docker compose up -d
```

`scripts/import-creds.sh` writes `.env` with mode `600`. Never commit it.

The first start downloads the Steam client and Steam Linux Runtime, then
installs/updates the game through Steam. The Scrap Mechanic EULA acceptance
marker is written into the persistent `sm_steam` volume so the headless server
can start without an interactive dialog.

The game container mounts:

| Volume | Purpose |
| --- | --- |
| `sm_steam` | Steam client, game install, Proton prefix |
| `sm_saves` | `User_<steamid>/Save` (the live save) |
| `sm_seed` | Read-only initialized seed save |
| `sm_logs` | Scrap Mechanic `Logs/game-*.log` |
| `sm_backups` | Control-plane only |
| `sm_control` | Control-plane state, operations DB |

Backups are not mounted into the game container.

## Host notes

On hosts that ship Ubuntu's `/etc/apparmor.d/steam` profile, that profile can
deny Steam client socket creation (`EACCES`) even when Docker runs with
`apparmor=unconfined`. Add network rules to the profile, or unload/remove it,
before starting the game:

```text
network inet stream,
network inet dgram,
network inet6 stream,
network inet6 dgram,
```

Reload with `apparmor_parser -r /etc/apparmor.d/steam`.

## Operation

The entrypoint runs SteamCMD for updates, starts Xvfb + openbox, logs in to the
Steam client, registers GE-Proton, seeds the save only when it is missing, and
launches:

```text
steam -applaunch 387990 -dedicated_server -console -open C:/.../Save/<name>.db
```

`-open` is used instead of `-last_save`. The live save and the save path parsed
from the game log are checked against `SAVE_NAME`; a mismatch is shown as
`error` in the API and metrics.

Useful commands:

```sh
docker compose logs -f sm-game
docker compose logs -f sm-control
docker compose stop
docker compose down          # keeps named volumes
```

Do not use `docker compose down -v` unless you intend to delete the world.

## API and metrics

The API is bound to `127.0.0.1:${SM_API_PORT}`. Every `/api/v1/*` route and
`/metrics` requires:

```text
Authorization: Bearer <SM_API_TOKEN>
```

```sh
TOKEN="$(sed -n "s/^SM_API_TOKEN='\\(.*\\)'/\\1/p" .env)"
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/api/v1/status
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/metrics
```

Routes:

- `GET /api/v1/status` — server/save/backup state and active save match.
- `GET /api/v1/players`, `GET /api/v1/logs?tail=200`.
- `GET /api/v1/logs/stream` — SSE snapshots.
- `POST /api/v1/backups` — online SQLite backup; `?wait=false` for async.
- `GET /api/v1/backups`, `GET /api/v1/backups/{id}/download`.
- `POST /api/v1/backups/{id}/restore` — server must be stopped; send
  `{"confirm_server_stopped": true}`. A pre-restore safety backup is made.
- `PATCH /api/v1/config` — runtime `SAVE_NAME`, `IS_CUSTOM_GAME`, backup
  retention/interval; persisted in `sm_control`.
- `GET /readyz`, `GET /healthz` — unauthenticated health checks.

A restore replaces the save file but does not start the server. Start it with
`docker compose start sm-game` (or `restart`).

Automatic backups run every `SM_BACKUP_INTERVAL_MINUTES` when the save changed.
Retention keeps the newest backup per day for `SM_BACKUP_RETENTION_DAYS` and the
newest per ISO week for `SM_BACKUP_RETENTION_WEEKS`; the latest is always kept.

## CPU rendering

The image is Ubuntu 24.04 with Mesa 25 lavapipe and GE-Proton's DXVK 3.1. The
container has no GPU device and exports `LIBGL_ALWAYS_SOFTWARE=1`,
`GALLIUM_DRIVER=llvmpipe`, and DXVK filtering for `llvmpipe`. The Steam Linux
Runtime exposes the host lavapipe ICD inside the pressure-vessel container.
Verify from the host with:

```sh
docker compose run --rm --entrypoint bash sm-game -lc \
  'export XDG_RUNTIME_DIR=/tmp/runtime-steam; mkdir -p "$XDG_RUNTIME_DIR"; \
   VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/lvp_icd.x86_64.json \
   VK_DRIVER_FILES=/usr/share/vulkan/icd.d/lvp_icd.x86_64.json \
   xvfb-run -a vulkaninfo --summary | grep -E "deviceName|driverName"'
```

The expected device is llvmpipe/lavapipe. The game reports the same device in
its log (`GPU: llvmpipe ...`) and runs without `/dev/dri`. `vulkaninfo` may
create a large amount of output; the summary is enough.

## Save rules

- The seed is copied only if `User_<steamid>/Save/<SAVE_NAME>.db` (or
  `Save/Custom/<SAVE_NAME>.db`) is absent.
- An existing invalid save or seed is a hard failure, not a new world.
- Changing `SAVE_NAME` or `IS_CUSTOM_GAME` after the save exists requires
  seeding the new path and restarting.
- The server account's SteamID is discovered from Steam after login; set
  `SM_STEAM_ID` in `.env` only if discovery is ambiguous.

## Remote deployment

Deployment is intentionally not automated here. The intended target is
`<remote-host>`, under `gameservers/scrap-mechanic`; transfer the project
without `.env`, `*.db`, or the Docker volumes, then recreate `.env` and seed the
save on the target.
