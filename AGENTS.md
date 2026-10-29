# AGENTS.md

Repository rules for coding agents and contributors working on
**SM-dedicated-server**.

Read `README.md` for the operator quick start and `TECHNICAL.md` for
architecture, startup flow, API semantics, patches and known risks. `PLAN.md`
is the historical design/decision log, not current operator documentation.

## Hard constraints

- Never commit `.env`, `*.db`, backups, logs or Docker volumes. `.env` is
  mode `600`; do not print or copy its values into docs, tests or chat.
- Never run `docker compose down -v` or delete `sm_saves`/`sm_seed` unless the
  task explicitly asks to destroy the world.
- Restore is allowed only when the server is stopped. Do not weaken the marker
  check or the `confirm_server_stopped` guard.
- Keep game Lua stock by default. Scrap Mechanic rejects clients whose server
  Lua differs; `SM_LUA_PATCHES=true` is a legacy revert switch, not a general
  patch-enable switch. If you change server Lua, synchronize the same files to
  every client and re-test the handshake.
- Native `d3d11.dll` hooks are version-specific. Preserve the byte/site checks
  and fail-soft logging; never assume offsets survive a Steam update.
- The panel is loopback-only by default. Do not bind it to `0.0.0.0` or remove
  the SSH/TLS warning without an explicit security decision.
- Keep `SAVE_NAME`/`GAME_MODE` path semantics:
  survival → `Save/Survival`, custom → `Save/Custom`, creative → `Save`.

## Repository map

| Path | Role |
| --- | --- |
| `compose.yaml` | `sm-game`, `sm-control`, `sm-panel`; volumes and ports |
| `.env.example` | documented runtime configuration; copy/import to `.env` |
| `docker/game/` | game image, Steam/Proton/Xvfb supervisor, save/Steam helpers |
| `docker/game/entrypoint-user.sh` | main 600+ line game supervisor |
| `docker/game/d3d11.dll` | prebuilt null-render/chat/container proxy |
| `nullrender/` | C++ proxy source and build script |
| `control/` | FastAPI control-plane, SQLite operations, backups, tests |
| `panel/` | FastAPI single-admin panel, noVNC and chat UI |
| `scripts/` | host-side credentials, seed, client-sync helpers |
| `PLAN.md` | historical architecture/decision log |

Untracked files may exist in the working tree (for example raid-fix or
client-sync helpers). Check `git status` before assuming a file ships.

## Validation

Run before yielding:

```sh
cd control
python3 -m pytest -q
python3 -m compileall -q app ../panel/app ../docker/game/*.py
cd ..
docker compose config >/dev/null
bash -n docker/game/entrypoint.sh docker/game/entrypoint-user.sh \
  control/entrypoint.sh scripts/*.sh
```

`docker compose config` requires a usable Compose plugin but not a running
daemon. Python compile checks catch syntax errors only; they are not
behavioural proof.

A full live test needs Docker, a Steam account with Guard disabled, the game
image and an initialized SQLite save. Minimum smoke test:

1. `docker compose build && docker compose up -d`.
2. Follow `docker compose logs -f sm-game` until the game process is running.
3. Check `/healthz`, `/readyz`, authenticated `/api/v1/status`, `/metrics`.
4. Confirm the active save in the game log matches `SAVE_NAME`.
5. Create/download a backup; stop `sm-game`; restore; start again.
6. Open the panel through an SSH tunnel and verify noVNC/chat.

If Docker or Steam is unavailable, state that live validation was not run.

## Change workflow

1. Read the relevant section of `TECHNICAL.md`; do not infer current behaviour
   from `PLAN.md` or from the old README.
2. Find the smallest existing pattern. Control-plane changes belong in
   `control/app` + a test in `control/tests`; game-side changes in
   `docker/game`; panel changes in `panel/app`.
3. Keep blocking I/O off the event loop (`asyncio.to_thread`) and backup/
   restore work inside `BackupManager`'s lock.
4. For game-side changes, preserve idempotence, `set -Eeuo pipefail`, root →
   `gosu steam` transition, and fail-soft patch behavior.
5. Update `TECHNICAL.md` for architecture/behaviour changes. Update `README.md`
   only for operator-visible changes. Update `PLAN.md` only for decisions or
   roadmap changes.
6. Remove temporary diagnostics, caches and generated files.

## Review checklist

- Auth: `/api/v1/*` and `/metrics` require a bearer token; empty
  `SM_API_TOKEN` silently disables auth. Do not introduce another unauth route
  by accident.
- Restore: server-stopped check, manifest/save-name match, checksum +
  integrity validation, safety backup, atomic replace, sidecar cleanup.
- Save validation must not hold a long read transaction against the live DB.
- Paths: no traversal outside `SM_SAVES_DIR`/`SM_BACKUPS_DIR`; resolve before
  checking.
- Backups: manifest/id consistency, SHA-256, retention, cross-process `flock`.
- Logs: newest `game-*.log`, rotation, partial lines, English marker strings.
- Secrets: no `.env` contents, tokens, passwords or Steam credentials in diffs.
- Panel: loopback bind, signed session, no direct VNC exposure, no unbounded
  proxy/stream.
- Docs: README short; details in TECHNICAL; agent rules here.

## Known pitfalls

- The restore guard is marker-age based. A stopped heartbeat, a hung
  entrypoint, or a restore during the shutdown window can pass the check while
  the game still has the save open. Do not make restore easier without a
  stronger liveness check.
- `ensure_dirs()` creates `saves`/`logs` paths, so `/readyz` can report a
  mounted volume when Docker actually mounted an empty directory.
- `save_match` compares only custom-vs-not; creative and survival both map to
  `is_custom=False`. Treat a reported match as a heuristic, not proof.
- `OperationStore` uses one SQLite connection under a lock; `store.close()` can
  race in-flight daemon operation threads during shutdown.
- Log parsing depends on English literals and a stable format. A Steam update
  can turn state into `starting`/`save_match=None` without an error.
- The native chat/container hooks patch fixed RVAs. Check
  `Release/nullrender.log` for `patch site does not match` after updates.
- First boot can fail because `validate_save.py` opens the read-only `/seed`
  SQLite file with `mode=rw`. Fix the validation path rather than making the
  seed volume writable.
- Steam passwords are passed in argv to `steam -login`/`steamcmd +login`; do not
  add more logging that captures process arguments.
- `steam_id.py` falls back to the first account in `loginusers.vdf` when the
  name does not match. Set `SM_STEAM_ID` or fail closed; do not seed the wrong
  `User_<id>`.
- `nullrender/d3d11.dll` and `docker/game/d3d11.dll` are duplicated; use
  `nullrender/build.sh` as the source of truth.
- The panel has no automated tests; the control plane has unit/API tests but no
  Docker/Steam integration test and no committed CI workflow.
