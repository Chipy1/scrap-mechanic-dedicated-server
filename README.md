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
- `SAVE_NAME` and `GAME_MODE` must match the save. `GAME_MODE` is one of
  `custom`, `survival`, or `creative`; survival maps to `Save/Survival`,
  custom to `Save/Custom`, and creative directly to `Save`.

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

## Null-render proxy

The game's own `-use_null_driver` flag does not disable the D3D11 renderer in
this build. The game image ships an app-local `d3d11.dll` proxy which forwards
device creation to the system DLL and replaces immediate-context draw calls
and `IDXGISwapChain::Present` with no-ops. Draw calls are disabled permanently;
the simulation and network loop continue. Look for `Release/nullrender.log`
in the game volume for load status.

This is version-sensitive native code. Set `SM_NULL_RENDER=false` and remove
`SM_GAME_EXTRA_ARGS`/the proxy to fall back to normal rendering. Steam updates
may replace the DLL; the entrypoint reinstalls it on every start.

## Chat message limit

The same app-local DLL installs a small runtime guard in the game's
`ChatServer` packet handler. Chat packets larger than 2048 bytes are dropped
before the handler parses their length-prefixed strings, so an oversized chat
message cannot overflow the handler's fixed stack buffers. Normal chat and
server commands are unaffected. The guard is version-sensitive: if a Steam
update changes the handler, the proxy logs
`chat limit: patch site does not match this game build, skipped` in
`Release/nullrender.log` instead of modifying unknown code.

## Inventory anti-cheat

Survival mode exposes the development-only `/unlimited` and `/limited` chat
commands. The dedicated server accepts the underlying `sv_setLimitedInventory`
network call without checking that the caller is the development host, so a
modified client can switch the whole world to creative inventory.

On every start the entrypoint applies a narrow patch to
`Survival/Scripts/game/SurvivalGame.lua`: both inventory-mode commands are
rejected, `sv_setLimitedInventory(false)` is converted to limited inventory,
and the sending player is kicked when the network layer provides a player
reference. Rejections are logged as `[sm-anticheat] ...` in the game log. The
patched server fixed-update additionally re-broadcasts `LimitedInventory=true`
once per second; this overwrites a client that changed only its local GameRules
byte with Cheat Engine. The patch is reapplied after Steam updates; if the
upstream Lua changes, the entrypoint logs `server Lua anti-cheat patch skipped`
and boots the unmodified game instead of guessing at new code.

The same patch also adds two container checks:

- `Chest.sv_n_open` rejects and kicks a player opening a chest from more than
  eight metres away.
- `BasePlayer.sv_n_exchangeItem` only accepts the calling player's own
  inventory or hotbar; a transaction naming another container is rejected and
  the player is kicked.

This is a mitigation, not a complete server-authoritative inventory system. A
client that freezes the local GameRules byte and keeps rewriting it can still
win the race between updates, and the stock server does not validate every
creative-inventory build packet. Client-side container GUI code can also display
data without a server event; these checks cover the server-visible open and
transaction paths. Full protection would require rejecting build/container
requests that are not backed by server-side state.

## Web panel and VNC

`sm-panel` is a separate host-network service bound to `127.0.0.1:${SM_PANEL_PORT:-8081}`.
It provides a single-admin web UI, server status, logs, manual backups, and a full-control
noVNC viewer. Raw VNC (port 5900) is never published; x11vnc listens only on loopback.

Open it through an SSH tunnel:

```sh
ssh -L 8081:127.0.0.1:8081 root@<remote-host>
# then open http://127.0.0.1:8081 in a browser
```

The login is `SM_PANEL_USER` / `SM_PANEL_PASSWORD` from `.env`. The panel session is
signed with `SM_PANEL_SECRET`; keep all three values secret. Do not bind the panel to
`0.0.0.0` without TLS and a firewall/VPN.

The panel also has a chat card. Incoming chat is captured from the native
`ChatServer` broadcast hook; messages sent from the panel are prefixed with
`[SERVER]` and broadcast through the same hook. They are therefore visible to
all connected clients and in the panel history. The transport is a small file
queue under the saves volume (`/saves/.sm-chat`); it does not modify game Lua.
The native hook is version-sensitive and stays disabled when its original bytes
do not match the installed game build.

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
- `PATCH /api/v1/config` — runtime `SAVE_NAME`, `GAME_MODE`, backup
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

For CPU-only hosts the default Xvfb screen is `1280x720x24` logical at `10` fake Hz with `MaxRenderScale=0.5` (640x360 render target)
(`XVFB_RESOLUTION`, `XVFB_FPS`) and llvmpipe uses `LP_NUM_THREADS=8`. A
1280x720/1 Hz Xvfb can make the game feel like slow motion; keep the fake
refresh at 10 or higher.
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

- The seed is copied only if the target path is absent:
  `Save/Survival/<SAVE_NAME>.db` for survival,
  `Save/Custom/<SAVE_NAME>.db` for custom, and
  `Save/<SAVE_NAME>.db` for creative.
- An existing invalid save or seed is a hard failure, not a new world.
- Changing `SAVE_NAME` or `GAME_MODE` after the save exists requires
  seeding the new path and restarting.
- The server account's SteamID is discovered from Steam after login; set
  `SM_STEAM_ID` in `.env` only if discovery is ambiguous.

## Remote deployment

Deployment is intentionally not automated here. The intended target is
`<remote-host>`, under `gameservers/scrap-mechanic`; transfer the project
without `.env`, `*.db`, or the Docker volumes, then recreate `.env` and seed the
save on the target.
