# Scrap Mechanic dedicated server

Steam-launched Scrap Mechanic dedicated server with CPU rendering, a REST API,
Prometheus metrics, SQLite backups and a small web panel. It runs without a GPU
or `/dev/dri`.

## Requirements

- Docker Engine with Compose v2.
- Docker must permit the Steam runtime's user namespaces. Compose sets
  `seccomp:unconfined` and `apparmor:unconfined`; Ubuntu 24.04 hosts may also
  need unprivileged user namespaces enabled.
- A Steam account that owns Scrap Mechanic (`387990`) with **Steam Guard
  disabled**. There is no interactive Guard flow.
- An **initialized** Scrap Mechanic SQLite save. The server never creates a new
  world; the save must pass SQLite `quick_check`.
- `SAVE_NAME` and `GAME_MODE` must match the save. `GAME_MODE` is `custom`,
  `survival` or `creative`; survival uses `Save/Survival`, custom uses
  `Save/Custom`, creative uses `Save`.

## Setup

```sh
scripts/import-creds.sh                 # reads ~/Documents/SMServer_creds.txt
scripts/seed-save.sh /path/to/initialized-save.db
docker compose build
docker compose up -d
```

`scripts/import-creds.sh` writes `.env` with mode `600`. Never commit it.
The first start downloads Steam, Steam Linux Runtime and the game, accepts the
EULA marker non-interactively, seeds the save once, then launches the server.

If the first boot fails while validating the read-only seed with SQLite, see
the seed-validation risk in `TECHNICAL.md` §9 before changing the volume mode.

## Services and volumes

| Service | Role |
| --- | --- |
| `sm-game` | Steam + GE-Proton + Scrap Mechanic under Xvfb; host network |
| `sm-control` | REST API, `/metrics`, backups/restore; loopback API port |
| `sm-panel` | single-admin UI, chat, logs, noVNC; loopback panel port |

| Volume | Purpose |
| --- | --- |
| `sm_steam` | Steam client, game install, Proton prefix |
| `sm_saves` | live `User_<steamid>/Save` tree |
| `sm_seed` | read-only initialized seed save |
| `sm_logs` | Scrap Mechanic `Logs/game-*.log` |
| `sm_backups` | control-plane backups only |
| `sm_control` | operations DB and runtime config |

## Operation

```sh
docker compose logs -f sm-game
docker compose logs -f sm-control
docker compose stop
docker compose down          # keeps named volumes
```

Do not use `docker compose down -v` unless you intend to delete the world.

The entrypoint runs SteamCMD updates, starts Xvfb/openbox/x11vnc, logs in to
Steam, registers GE-Proton, seeds the save only when missing, and launches:

```text
steam -applaunch 387990 -dedicated_server -console -open C:/.../Save/<name>.db
```

A fresh `.sm-server-running` marker is the liveness signal; the live save and
the path parsed from the game log are checked against `SAVE_NAME`. A mismatch
shows as `error` in the API and metrics.

## Access

The control API is bound to `127.0.0.1:${SM_API_PORT}` and requires a bearer
token:

```sh
TOKEN="$(sed -n "s/^SM_API_TOKEN='\\(.*\\)'/\\1/p" .env)"
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/api/v1/status
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/metrics
```

The panel is bound to `127.0.0.1:${SM_PANEL_PORT}`. Open it through an SSH
tunnel:

```sh
ssh -L 8081:127.0.0.1:8081 root@<remote-host>
# http://127.0.0.1:8081
```

Login uses `SM_PANEL_USER` / `SM_PANEL_PASSWORD`; the session is signed with
`SM_PANEL_SECRET`. Raw VNC (5900) is loopback-only and reached through the
panel's noVNC proxy.

## Runtime notes

- **CPU rendering:** Mesa llvmpipe/lavapipe and DXVK, no `/dev/dri`. Tune
  `XVFB_RESOLUTION`, `XVFB_FPS`, `LP_NUM_THREADS` and `SM_FPS_CAP` for the host.
- **Null-render proxy:** the game image ships a `d3d11.dll` proxy that disables
  draw calls but keeps simulation and networking. It also enforces the 2048-byte
  chat limit and blocks a native container item-write path. Offsets are
  build-specific; see `Release/nullrender.log` after Steam updates.
  Set `SM_NULL_RENDER=false` to fall back to normal rendering.
- **Game Lua stays stock by default.** Scrap Mechanic rejects clients whose
  server Lua differs. `SM_LUA_PATCHES=true` only reverts the legacy Lua patch;
  it is not a general patch-enable switch.
- **Optional raid fix:** `SM_RAID_FIX=true` applies the `BaseUnit` nil-character
  fix and clears the Lua cache.
- **Backups:** online snapshot copies validate with `integrity_check`; restore
  requires the server to be stopped and `confirm_server_stopped=true`.
  Retention keeps the newest backup per day/week.
- **Client sync:** if you run modified server Lua, use
  `scripts/sync_client_scripts.sh` (or `scripts/play_client_synced.sh`) so the
  client uses the same scripts.

## Save rules

- The seed is copied only if the target is absent. An existing invalid save or
  an absent/invalid seed is a hard failure; no new world is created.
- Changing `SAVE_NAME` or `GAME_MODE` after the save exists requires seeding the
  new path and restarting.
- The server account's SteamID is discovered from Steam; set `SM_STEAM_ID` only
  if discovery is ambiguous.

## Documentation

- [TECHNICAL.md](TECHNICAL.md) — architecture, startup flow, API, backups,
  native patches, CPU rendering, client sync, tests and known risks.
- [AGENTS.md](AGENTS.md) — repository rules, validation commands and review
  checklist for contributors and coding agents.
- [PLAN.md](PLAN.md) — historical design/decision log and roadmap.

## Security

`.env` contains Steam, API and panel secrets. Keep it mode `600`, never commit
it, and keep the API/panel loopback-bound. An empty `SM_API_TOKEN` disables API
authentication. Do not expose the panel directly without TLS/reverse-proxy and
firewall/VPN rules.

## Remote deployment

Deployment is intentionally manual. Target:
`<remote-host>:/opt/docker/gameservers/scrap-mechanic`. Transfer the repository
without `.env`, `*.db` or Docker volumes, recreate `.env` on the target, seed the
initialized save, then `docker compose build && docker compose up -d`.
