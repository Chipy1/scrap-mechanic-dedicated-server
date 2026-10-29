# Technical reference

Detailed architecture and operations for **SM-dedicated-server**. For the
operator quick start see `README.md`; for repository rules see `AGENTS.md`.
The historical plan and decision log remain in `PLAN.md`.

## 1. Services and volumes

`compose.yaml` runs three services:

| Service | Network | Role |
| --- | --- | --- |
| `sm-game` | host | Steam client + GE-Proton + Scrap Mechanic dedicated server under Xvfb |
| `sm-control` | bridge, published only to `127.0.0.1:${SM_API_PORT}` | REST API, Prometheus metrics, backups/restore, log and save monitoring |
| `sm-panel` | host, bound to `127.0.0.1:${SM_PANEL_PORT}` | single-admin web UI, chat, logs, noVNC proxy |

Volumes:

| Volume | Mounted in | Purpose |
| --- | --- | --- |
| `sm_steam` | `sm-game` | Steam client, game install, Proton prefix, compatibility mapping |
| `sm_saves` | `sm-game`, `sm-control` | `User_*/Save` tree; control mounts it read-write for backup/restore |
| `sm_logs` | `sm-game`, `sm-control` (ro) | `Scrap Mechanic/Logs/game-*.log` |
| `sm_seed` | `sm-game` (ro), host seeding script | initialized SQLite seed save |
| `sm_backups` | `sm-control` only | immutable backup directories + manifests |
| `sm_control` | `sm-control` | operations DB and runtime config |

`sm-game` uses `network_mode: host` because Steam networking is peer-to-peer.
The published UDP ports are only useful for direct/native connections.

## 2. Game container startup

`docker/game/entrypoint.sh` runs as root, repairs volume ownership once per
volume, then `exec gosu steam` into `entrypoint-user.sh`. The user entrypoint
is the actual supervisor:

1. Validate `SERVER_STEAM_USER_LOGIN`, `SERVER_STEAM_USER_PASSWORD`,
   `SAVE_NAME`, `GAME_MODE`.
2. Start Xvfb (`XVFB_RESOLUTION`, `XVFB_FPS`), x11vnc on loopback, openbox and
   a D-Bus session.
3. Register GE-Proton as a Steam compatibility tool.
4. Run SteamCMD `app_update 387990` unless `SM_SKIP_UPDATE=true`.
5. Apply optional server patches (raid fix; legacy Lua revert).
6. Start the full Steam client, log in and wait for the account id.
7. Write the Proton `CompatToolMapping` and the EULA acceptance marker.
8. Resolve the SteamID and seed the save only when the target is absent.
9. Install low-graphics settings and the null-render D3D11 proxy.
10. Launch `steam -applaunch 387990 -dedicated_server -console -fps <cap>
    -open C:/.../<save>.db`.
11. Write `.sm-server-running`, heartbeat it every 10 s, wait for the game,
    then clean up on exit.

Important environment variables:

| Variable | Default | Effect |
| --- | --- | --- |
| `SAVE_NAME` | `something-test` | save stem, `.db` is appended |
| `GAME_MODE` | `survival` | `survival`, `custom`, `creative` |
| `SM_SKIP_UPDATE` | `false` | skip SteamCMD during development |
| `SM_LUA_PATCHES` | `false` | legacy switch; `true` only reverts old Lua patches |
| `SM_RAID_FIX` | `false` | apply the BaseUnit nil-character raid fix |
| `SM_NULL_RENDER` | `true` | install/remove the D3D11 null-render proxy |
| `SM_LOW_GRAPHICS` | `true` | install `low-settings.json` |
| `SM_GAME_EXTRA_ARGS` | empty | extra args before `-fps`/`-open` |
| `SM_STEAM_LOGIN_TIMEOUT` | `1200` | Steam login deadline |
| `SM_GAME_START_TIMEOUT` | `300` | game process deadline |
| `SM_STEAMCMD_TIMEOUT` | `1800` | SteamCMD/update deadline |

### Save selection and seeding

`GAME_MODE` controls the target directory:

- `survival` → `Save/Survival/<name>.db`
- `custom` → `Save/Custom/<name>.db`
- `creative` → `Save/<name>.db`

The seed is copied only when the target does not exist. An existing invalid
save or an absent/invalid seed is a hard failure; the server never creates a
new world. The target is re-validated after copying.

### Shutdown

On SIGTERM the supervisor tries Alt+F4/wmctrl for a graceful close, waits,
then escalates to SIGTERM/SIGKILL and `wineserver -k`. The marker is removed
only after the game and Steam are down, so restore cannot race shutdown.

### Host AppArmor

On Ubuntu hosts that ship `/etc/apparmor.d/steam`, that profile can deny Steam
client socket creation (`EACCES`) even when Docker runs with
`apparmor=unconfined`. Add the following network rules to the profile (or
unload/remove it) before starting the game:

```text
network inet stream,
network inet dgram,
network inet6 stream,
network inet6 dgram,
```

Reload with `apparmor_parser -r /etc/apparmor.d/steam`.

## 3. Control plane

`control/app` is a FastAPI application. `create_app()` reads `Settings` from
environment variables and starts two background loops:

- log polling (`SM_LOG_POLL_SECONDS`)
- automatic backups (`SM_BACKUP_AUTO`, `SM_BACKUP_INTERVAL_MINUTES`)

Modules:

| Module | Responsibility |
| --- | --- |
| `config.py` | env parsing, validation, runtime config, path layout |
| `saves.py` | save discovery, SQLite validation, safe snapshot copy |
| `backups.py` | backup manifest, retention, verify, restore |
| `server.py` | heartbeat marker + log-derived server state |
| `logs.py` | incremental newest-log parser, player/save/version tracking |
| `store.py` | SQLite operations DB + background operation threads |
| `api.py` | routes, auth, exception mapping, metrics |
| `metrics.py` | Prometheus gauges |
| `chat.py` | file-backed chat inbox/outbox under `sm_saves/.sm-chat` |

### Authentication

Every `/api/v1/*` route and `/metrics` requires:

```text
Authorization: Bearer <SM_API_TOKEN>
```

`/healthz` and `/readyz` are public. `_auth_dependency` returns immediately
when `SM_API_TOKEN` is empty, which disables API authentication; keep the
token set in production.

### API surface

- `GET /api/v1/status` — server, save, backup and control status.
- `GET /api/v1/players`, `GET /api/v1/logs?tail=N`, `GET /api/v1/logs/stream`
  (SSE).
- `GET/POST /api/v1/chat` — read/send chat through the file queue.
- `GET/POST /api/v1/backups`; `GET /api/v1/backups/{id}`;
  `GET /api/v1/backups/{id}/download`; `POST .../restore`;
  `DELETE .../{id}`.
- `GET/PATCH /api/v1/config` — runtime save name/mode, backup settings,
  restore confirmation policy.
- `GET /api/v1/operations`, `GET /api/v1/operations/{id}` — async backup/restore
  jobs.
- `GET /metrics` — Prometheus text format.
- `GET /readyz` — checks mounts, save presence/validity, server state.

### Save resolution and validation

`SaveResolver` searches `User_*/Save/<mode>/<SAVE_NAME>.db`, optionally
restricted by `SM_STEAM_ID`/`STEAM_ID`. Zero matches → 404; multiple → 409.
Relative paths are resolved and checked against the saves root.

`validate_save()` opens SQLite read-only and checks:

- file exists, non-empty;
- `PRAGMA quick_check` (or `integrity_check`);
- a `Game` table exists;
- `gametick` and `savegameversion` columns exist;
- the `Game` row is present;
- optional `mods` hash, table count and metadata.

The game has no SQLite busy timeout, so an external read transaction can crash
its next commit. Backups therefore use `copy_save_snapshot()`: copy the main
database while no `-journal`/`-wal`/`-shm` sidecar exists, verify source
size/mtime did not change during the copy, fsync the destination, then validate
the detached copy with `integrity_check`.

### Backups and restore

A backup is a directory under `sm_backups` containing the database and
`manifest.json` (id, timestamps, trigger, save name/mode, source path, sizes,
SHA-256, gametick/savegameversion, duration). The directory is built in a
temporary directory and atomically renamed; a cross-process `flock` prevents
concurrent backup/restore.

Retention keeps the newest backup plus the newest per day for
`SM_BACKUP_RETENTION_DAYS` and the newest per ISO week for
`SM_BACKUP_RETENTION_WEEKS`.

Restore:

1. Requires a manifest whose filename matches the active `SAVE_NAME`.
2. Requires the server marker to be absent/stale.
3. Requires `confirm_server_stopped=true` unless `force=true` or the policy is
   disabled.
4. Verifies checksum + `integrity_check`.
5. Makes an optional pre-restore safety backup.
6. Writes a temp file, fsyncs, atomically replaces the target, removes
   `-wal`/`-shm`/`-journal`, validates the result, and restores the safety
   backup if validation fails.

Restore does not start the server. The operator starts `sm-game` afterward.

### Server state and logs

`sm-game` writes `.sm-server-running` in `sm_saves` and touches it every 10 s.
`ServerMonitor` treats a fresh marker as authoritative; without it the state is
`stopped` even if an old log says `Server started`. A fresh marker with no
server-start log is `starting`. A fresh marker plus a save mismatch is `error`.

`LogMonitor` follows the newest `game-*.log`, parses version, active save,
connection handles, player join/leave and `Server started`, and tracks
`ERROR`/`WARNING` counts. The server account is excluded from the online-player
count.

## 4. Web panel

`panel/app/main.py` is a single-admin FastAPI app:

- signed HMAC session cookie (`SM_PANEL_USER`, `SM_PANEL_PASSWORD`,
  `SM_PANEL_SECRET`);
- `/api/panel/*` proxies to the control API with `SM_API_TOKEN`;
- `/vnc/ws` proxies a WebSocket to loopback x11vnc;
- `/vnc/*` serves noVNC 1.5 static assets;
- chat, status, logs and manual backup buttons are inline HTML/JS.

The panel is loopback-bound by default. The cookie uses `secure=False` because
the default deployment is plain HTTP over an SSH tunnel; do not expose it
directly without TLS/reverse-proxy and firewall rules.

There are currently no automated panel tests.

## 5. Native proxy and game patches

### Null-render D3D11 proxy

`nullrender/d3d11_proxy.cpp` builds `d3d11.dll` with MinGW. The game imports
`D3D11CreateDevice`/`D3D11CreateDeviceAndSwapChain`; the proxy forwards device
creation to the system DLL, then replaces immediate-context draw/clear/dispatch
and swap-chain `Present` with no-ops. Resource creation and simulation remain.

The proxy also installs two version-sensitive hooks:

- **Chat limit:** drops `ChatServer` packets larger than 2048 bytes before the
  handler parses length-prefixed strings. If the original bytes at the patch
  site do not match, it logs `chat limit: patch site does not match this game
  build, skipped`.
- **Container guard:** blocks the native container transaction command that
  directly writes an item slot (`setItem`), while leaving normal
  `moveFromSlot`/`collectToSlot` paths alone.

Patch offsets are tied to the tested Scrap Mechanic build. Steam updates can
invalidate them; the proxy fails soft and logs to `Release/nullrender.log`.
`SM_NULL_RENDER=false` removes the proxy (if it matches the shipped DLL) and
falls back to normal rendering.

`nullrender/build.sh` cross-compiles and copies the DLL into
`docker/game/d3d11.dll`; both the source tree and the game build context keep a
copy of the binary.

### Optional Lua patches

`docker/game/patch_server_lua.py` contains older anti-cheat patches
(inventory commands, remote chest, item grants, inventory audit). The server
now keeps game Lua **stock by default** because Scrap Mechanic rejects clients
whose server Lua differs from the shipped files:

```sh
SM_LUA_PATCHES=false  # default: leave game Lua untouched
```

`SM_LUA_PATCHES=true` is a legacy migration switch that runs
`patch_server_lua.py --revert`; it does not apply the old patches. Do not
re-enable them without synchronizing the same Lua to every client (see
`scripts/sync_client_scripts.sh`) and re-testing the client handshake.

`patch_server_raid.py` fixes a stock `BaseUnit.sv_onDeath()` crash where a
player without a character aborts the raid cleanup. It is gated by
`SM_RAID_FIX=true`, idempotent, and removes `Cache/Bundle/core_data.cbo` after
patching so the game recompiles Lua.

`patch_steam_config.py`, `accept_eula.py`, `steam_id.py` and `validate_save.py`
are narrow, idempotent helpers for VDF edits, EULA markers, SteamID discovery
and SQLite validation.

## 6. CPU rendering

The image is Ubuntu 24.04 with Mesa llvmpipe/lavapipe and DXVK. It exports
`LIBGL_ALWAYS_SOFTWARE=1`, `GALLIUM_DRIVER=llvmpipe`, and DXVK device filtering;
the Steam Linux Runtime exposes the host lavapipe ICD inside pressure-vessel.

Defaults: Xvfb `1280x720x24` logical at 10 fake Hz, `MaxRenderScale=0.5`
(640x360 render target), and `LP_NUM_THREADS=2` in `.env.example`; tune both to
the host. A 1 Hz Xvfb can make the game feel like slow motion; keep the fake
refresh at 10 or higher.

Verify from the host:

```sh
docker compose run --rm --entrypoint bash sm-game -lc \
  'export XDG_RUNTIME_DIR=/tmp/runtime-steam; mkdir -p "$XDG_RUNTIME_DIR"; \
   VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/lvp_icd.x86_64.json \
   VK_DRIVER_FILES=/usr/share/vulkan/icd.d/lvp_icd.x86_64.json \
   xvfb-run -a vulkaninfo --summary | grep -E "deviceName|driverName"'
```

Expected: llvmpipe/lavapipe, no `/dev/dri`.

## 7. Client script synchronization

Scrap Mechanic does not push server Lua to clients. If the server runs
modified Lua, clients must use the same files or the handshake can fail. The
host-side helpers are:

```sh
scripts/sync_client_scripts.sh          # rsync server Survival/Scripts -> local client
scripts/play_client_synced.sh           # sync, then steam -applaunch 387990
```

The sync is one-way and server-authoritative, backs up overwritten/deleted
files under `~/.local/share/sm-script-sync/backups`, and refuses while the
local game is running unless `--force`. Defaults are overridable with
`SM_SYNC_SERVER`, `SM_SYNC_REMOTE_DIR`, `SM_SYNC_LOCAL_DIR`.

Because the current entrypoint keeps server Lua stock, this is mainly needed
for servers that explicitly enable Lua patches or custom server scripts.

## 8. Validation and tests

Control plane:

```sh
cd control
python3 -m pytest -q
python3 -m compileall -q app ../panel/app ../docker/game/*.py
```

Current coverage: 42 control-plane tests across config/saves/backups/logs/
server/API/chat. No panel tests, no game-container integration tests, and no
CI workflow is committed.

Compose/config validation:

```sh
docker compose config >/dev/null
bash -n docker/game/entrypoint.sh docker/game/entrypoint-user.sh \
  control/entrypoint.sh scripts/*.sh
```

`docker compose config` catches service/volume/port wiring errors without a
running daemon. Shell syntax checks do not exercise Steam or the game.

A full smoke test requires Docker, a Steam account with Guard disabled, the
game image, and an initialized save. The minimum live checks are:

1. `docker compose build && docker compose up -d`.
2. `docker compose logs -f sm-game` until the game process is running.
3. `curl` `/healthz`, `/readyz`, authenticated `/api/v1/status`, `/metrics`.
4. Verify the active save in the game log matches `SAVE_NAME`.
5. Create a backup, download it, stop `sm-game`, restore, start it again.
6. Open the panel through an SSH tunnel and verify noVNC/chat.

## 9. Review findings and risks

### Control-plane correctness risks

These are concrete review findings, not hypotheticals; they are documented so
an operator can decide whether to fix them before relying on the feature.

- **Restore rollback gap:** restore replaces the target before validating the
  restored file. If no pre-restore safety backup was created (or its creation
  failed with a non-`SaveError`), a failed validation leaves the corrupted
  target in place; the rollback copy itself is not validated/fsynced.
- **Live restore race:** the guard uses marker age, not a live process check.
  A stopped heartbeat, a hung entrypoint, or a restore during the shutdown
  window can overwrite a save still open by the game, then unlink
  `-wal`/`-shm`/`-journal` sidecars. Treat restore as safe only after the game
  container is fully stopped (`docker compose stop sm-game`).
- **Live validation read transaction:** `validate_save()` opens a read
  transaction with a 10 s busy timeout. It is called on the live database when
  the marker is stale/absent; Scrap Mechanic has no busy timeout, so this can
  make its next commit fail. Avoid validating the live file while the game may
  still be running.
- **`/readyz` can be tautological:** `ensure_dirs()` creates the saves/logs
  paths, so `saves_mounted`/`logs_mounted` can be true even when Docker mounted
  an empty directory. Check actual volume contents/ownership in deployment.
- **Creative/survival save match:** `save_match` compares only
  `is_custom_game` (custom vs not); a creative world can be reported as a match
  for a survival save. Treat it as a heuristic, not proof.
- **Backup failure accounting:** an invalid source save can increment
  `backup_failures` twice (once before validation and once in the outer
  handler), so metrics overstate failures.
- **Operation shutdown race:** `?wait=false` spawns an unbounded daemon thread
  per request. On shutdown, `OperationStore.close()` can race an in-flight
  thread and produce `Cannot operate on a closed database`.
- **Log/SSE scaling:** `tail()` reads the whole log; the SSE loop calls it
  every 2 s per client. The log is unbounded, and there is no client cap.
- **Chat file queue:** truncating `outbox.log` with `write_text` while the
  native reader has it open can race a line; the Python lock does not protect
  the C++ reader.
- **`force` semantics:** `force=true` bypasses the confirmation flag but not
  the marker check. It is not an audited override.

### Game-container correctness and security risks

- **First-boot seed validation can fail:** `seed_save()` validates the seed
  before copying, but `validate_save.py` opens SQLite with `mode=rw`; `sm_seed`
  is mounted `:ro`, so O_RDWR can fail with `EROFS`. Validate a copy, or open
  the seed with `mode=ro` while still allowing the target copy to recover hot
  journals.
- **Steam login happens twice:** the initial login is followed by
  `patch_proton_mapping()` stopping Steam, patching `config.vdf`/EULA, then
  logging in again. This doubles login timeouts and can leave the first client
  state inconsistent.
- **Password exposure:** the Steam password is passed in argv to
  `steam -login` and `steamcmd +login`, visible to other processes in the
  container/host PID namespace during startup. A credential file/`--password`
  stdin flow would be safer.
- **EULA version is hardcoded** to `3`. A future EULA change is treated as an
  existing acceptance, and a missing `apps` block is fatal rather than a
  warning.
- **SteamID discovery falls back to the first user** in `loginusers.vdf` when
  the account name does not match. That can seed/read the wrong `User_<id>`
  directory instead of failing.
- **Low graphics overwrite:** `settings.json` is installed on every start when
  `SM_LOW_GRAPHICS=true`; operator customization is lost.
- **Null-render install failure is fatal:** a missing proxy DLL makes
  `install_null_render` return non-zero under `set -e`, aborting without a
  targeted `die` message. `SM_GAME_EXTRA_ARGS` is word-split and placed before
  `-fps`/`-open`.
- **Native proxy scope:** the DLL also installs a ChatServer file bridge,
  container-transaction guards and a core-files-modified bypass, not just
  draw-call no-ops. The bridge assumes the proxy's `Present` path keeps
  running; hardcoded RVAs become no-ops after an update and log to
  `Release/nullrender.log`, not the logs volume.
- **Client helpers are host-specific:** the seed/sync/play scripts hardcode a
  SteamID, `/mnt/p6` paths or `root@192.168.50.41`; use their env overrides or
  edit before reuse.

### Operational and maintenance risks

- The old README claimed the inventory Lua anti-cheat was applied on every
  start. The current entrypoint keeps game Lua stock by default and
  `SM_LUA_PATCHES=true` only reverts the legacy patch. The README was corrected
  and the details moved here.
- `SM_API_TOKEN` empty disables API authentication. Keep it set; `.env.example`
  already instructs generating a long random token.
- The panel session cookie is `secure=False` for loopback HTTP. Do not bind the
  panel to `0.0.0.0` without TLS/VPN/firewall.
- Native offsets in `d3d11_proxy.cpp` are build-specific. Steam updates may
  silently disable the chat/container guards; check `Release/nullrender.log`.
- `nullrender/d3d11.dll` and `docker/game/d3d11.dll` are duplicated binaries.
  Keep the build script as the source of truth and avoid hand-editing either.
- `docker/game/entrypoint-user.sh` is a 600+ line supervisor. It is hard to
  unit-test; shell syntax checks and live Docker smoke tests are the current
  safety net.
- `panel/app/main.py` has no tests and embeds a large HTML/JS string. Consider
  extracting templates/static assets and adding login/proxy/VNC tests.
- Generated `__pycache__`/`.pytest_cache` directories are ignored; do not
  commit them. A stale `patch_server_metrics.pyc` had no matching source and
  was removed during this cleanup.
- `scripts/seed-save.sh`, `scripts/sync_client_scripts.sh` and
  `scripts/play_client_synced.sh` hardcode this workstation's SteamID/paths or
  `root@192.168.50.41`; override the documented environment variables (or edit
  them) before reusing on another host.
- The working tree may contain uncommitted files (for example the raid fix and
  client-sync helpers). Check `git status` before assuming a file ships.
- `PLAN.md` is a historical design/decision log, not an operator manual. Keep it
  for rationale; keep current behavior in this file.
- There is no CI. Adding at least `pytest`, `compileall`, `docker compose
  config`, and shell syntax checks would catch most regressions.

## 10. Deployment

Deployment is intentionally manual. Target:
`<remote-host>:/opt/docker/gameservers/scrap-mechanic` (the plan uses
`gameservers/scrap-mechanic`). Transfer the repository without `.env`, `*.db`
or Docker volumes, recreate `.env` on the target, seed the initialized save,
then:

```sh
docker compose build
docker compose up -d
docker compose logs -f sm-game
```

Never use `docker compose down -v` unless deleting the world is intended.
