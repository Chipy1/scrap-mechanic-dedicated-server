#!/usr/bin/env bash
set -Eeuo pipefail
umask 002

APP_ID="${APP_ID:-387990}"
HOME="${HOME:-/home/steam}"
STEAM_ROOT="${STEAM_ROOT:-$HOME/.local/share/Steam}"
GAME_DIR="$STEAM_ROOT/steamapps/common/Scrap Mechanic"
GAME_EXE="$GAME_DIR/Release/ScrapMechanic.exe"
GAME_PROCESS_PATTERN='^S:\\steamapps\\common\\Scrap Mechanic\\Release\\ScrapMechanic\.exe( |$)'
GE_PROTON_DIR="${GE_PROTON_DIR:-/opt/proton/current}"
GE_PROTON_NAME="${GE_PROTON_NAME:-GE-Proton11-11-x86_64}"
STEAM_BIN="${STEAM_BIN:-/usr/bin/steam}"
CACHE_DIR="${SM_CACHE_DIR:-$HOME/.cache/sm}"
SAVES_ROOT="${SM_SAVES_ROOT:-$STEAM_ROOT/steamapps/compatdata/$APP_ID/pfx/drive_c/users/steamuser/AppData/Roaming/Axolot Games/Scrap Mechanic/User}"
LOGS_ROOT="${SM_LOGS_ROOT:-$STEAM_ROOT/steamapps/common/Scrap Mechanic/Logs}"
SEED_DIR="${SM_SEED_DIR:-/seed}"
SAVE_NAME="${SAVE_NAME:-something-test}"
GAME_MODE="${GAME_MODE:-survival}"
LOGIN="${SERVER_STEAM_USER_LOGIN:-}"
PASSWORD="${SERVER_STEAM_USER_PASSWORD:-}"
STEAM_LOGIN_LOG="$STEAM_ROOT/logs/connection_log.txt"
STEAM_LOGIN_OFFSET=0
STEAM_LOGIN_STARTED_EPOCH=0
LOGIN_STEAM_ID=""
ACCOUNT_ID=""
STEAM_ID_SCRIPT="${STEAM_ID_SCRIPT:-/usr/local/lib/sm/steam_id.py}"
PATCH_SCRIPT="${PATCH_SCRIPT:-/usr/local/lib/sm/patch_steam_config.py}"
EULA_SCRIPT="${EULA_SCRIPT:-/usr/local/lib/sm/accept_eula.py}"
LOW_SETTINGS="${SM_LOW_SETTINGS:-/usr/local/lib/sm/low-settings.json}"
NULL_RENDER_DLL="${SM_NULL_RENDER_DLL:-/usr/local/lib/sm/d3d11-null.dll}"
SM_FPS_CAP="${SM_FPS_CAP:-10}"
VALIDATE_SCRIPT="${VALIDATE_SCRIPT:-/usr/local/lib/sm/validate_save.py}"

PREFIX="$STEAM_ROOT/steamapps/compatdata/$APP_ID/pfx"
MARKER="$SAVES_ROOT/.sm-server-running"
STEAM_PID=""
XVFB_PID=""
VNC_PID=""
OPENBOX_PID=""
HEARTBEAT_PID=""
STEAM_ID=""
TARGET=""
WIN_SAVE=""
CLEANED=0

mkdir -p "$CACHE_DIR" "$SAVES_ROOT" "$LOGS_ROOT"

log() {
    printf '[sm] %s\n' "$*"
}

die() {
    log "ERROR: $*"
    exit 1
}

require_config() {
    [[ -n "$LOGIN" ]] || die "SERVER_STEAM_USER_LOGIN is empty"
    [[ -n "$PASSWORD" ]] || die "SERVER_STEAM_USER_PASSWORD is empty"
    [[ -n "$SAVE_NAME" ]] || die "SAVE_NAME is empty"
    SAVE_NAME="${SAVE_NAME%.db}"
    [[ -n "$SAVE_NAME" ]] || die "SAVE_NAME must not be empty"
    SAVE_FILENAME="$SAVE_NAME.db"
    case "${GAME_MODE,,}" in
        survival) SAVE_SUBDIR="Save/Survival" ;;
        custom|creative) SAVE_SUBDIR="Save/Custom" ;;
        *) die "GAME_MODE must be custom, survival or creative (got: $GAME_MODE)" ;;
    esac
    SEED_FILE="$SEED_DIR/$SAVE_FILENAME"
}

start_vnc() {
    if ! command -v x11vnc >/dev/null 2>&1; then
        log "x11vnc is not installed"
        return 1
    fi
    x11vnc -display "$DISPLAY" -rfbport 5900 -localhost -nopw \
        -forever -shared -noxdamage -repeat -nolookup -quiet \
        >"$CACHE_DIR/x11vnc.log" 2>&1 &
    VNC_PID=$!
    sleep 1
    if ! kill -0 "$VNC_PID" 2>/dev/null; then
        tail -n 30 "$CACHE_DIR/x11vnc.log" >&2 || true
        return 1
    fi
    log "VNC ready on 127.0.0.1:5900"
}

start_x() {
    export DISPLAY="${DISPLAY:-:99}"
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/runtime-steam}"
    mkdir -p "$XDG_RUNTIME_DIR"
    chmod 700 "$XDG_RUNTIME_DIR"

    # No GPU is passed into the container. Force Mesa's software rasterizers
    # for both the Steam client and DXVK inside Proton.
    export LIBGL_ALWAYS_SOFTWARE=1
    export GALLIUM_DRIVER=llvmpipe
    export MESA_LOADER_DRIVER_OVERRIDE=llvmpipe
    export LP_NUM_THREADS="${LP_NUM_THREADS:-2}"
    export DXVK_FPS_LIMIT="${DXVK_FPS_LIMIT:-$SM_FPS_CAP}"
    # Paths under /usr/local are not visible in the Steam Linux Runtime
    # container, so keep the DXVK config in the Steam volume instead.
    export DXVK_CONFIG_FILE="$STEAM_ROOT/dxvk.conf"
    install -m 0644 /usr/local/lib/sm/dxvk.conf "$DXVK_CONFIG_FILE" 2>/dev/null || true
    export PROTON_NO_ESYNC=1
    export PROTON_NO_FSYNC=1
    export WINEDEBUG="${WINEDEBUG:--all}"
    # Do not set VK_ICD_FILENAMES here: inside the Steam Linux Runtime the
    # host ICDs are exposed under /run/host, not /usr/share.

    local display_number="${DISPLAY#:}"
    rm -f "/tmp/.X${display_number}-lock" "/tmp/.X11-unix/X${display_number}" 2>/dev/null || true
    Xvfb "$DISPLAY" -screen 0 "${XVFB_RESOLUTION:-1280x720x24}" \
        -fakescreenfps "${XVFB_FPS:-10}" \
        -nolisten tcp -ac +extension GLX +render -noreset \
        >"$CACHE_DIR/xvfb.log" 2>&1 &
    XVFB_PID=$!

    local i
    for i in $(seq 1 50); do
        if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
            break
        fi
        sleep 0.2
    done
    xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 || die "Xvfb did not start"

    openbox --sm-disable >"$CACHE_DIR/openbox.log" 2>&1 &
    OPENBOX_PID=$!

    if command -v dbus-launch >/dev/null 2>&1; then
        eval "$(dbus-launch --sh-syntax)" || true
    fi
    log "X display $DISPLAY is ready"
}

stop_steam() {
    if command -v "$STEAM_BIN" >/dev/null 2>&1; then
        timeout 15 "$STEAM_BIN" -shutdown >/dev/null 2>&1 || true
    fi
    if [[ -n "$STEAM_PID" ]] && kill -0 "$STEAM_PID" 2>/dev/null; then
        local i
        for i in $(seq 1 20); do
            kill -0 "$STEAM_PID" 2>/dev/null || break
            sleep 1
        done
        kill -TERM "$STEAM_PID" 2>/dev/null || true
    fi
    pkill -TERM -x steam 2>/dev/null || true
    pkill -TERM -f '[s]teamwebhelper' 2>/dev/null || true
    sleep 2
    pkill -KILL -x steam 2>/dev/null || true
    pkill -KILL -f '[s]teamwebhelper' 2>/dev/null || true
    STEAM_PID=""
}

start_steam() {
    log "starting Steam client"
    STEAM_LOGIN_OFFSET="$(stat -c %s "$STEAM_LOGIN_LOG" 2>/dev/null || echo 0)"
    STEAM_LOGIN_STARTED_EPOCH="$(date +%s)"
    "$STEAM_BIN" -login "$LOGIN" "$PASSWORD" \
        -cef-disable-gpu -cef-disable-sandbox \
        >>"$CACHE_DIR/steam-client.log" 2>&1 &
    STEAM_PID=$!
}

login_id_from_log() {
    python3 - "$STEAM_LOGIN_LOG" "${STEAM_LOGIN_OFFSET:-0}" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    offset = int(sys.argv[2])
except ValueError:
    offset = 0
try:
    data = path.read_bytes()
except OSError:
    raise SystemExit(0)
if 0 < offset <= len(data):
    data = data[offset:]
matches = re.findall(
    rb"\[U:1:(\d+)\][^\n]*RecvMsgClientLogOnResponse\(\)[^\n]*'OK'",
    data,
)
if matches:
    print(76561197960265728 + int(matches[-1]))
PY
}

wait_for_login() {
    local timeout="${SM_STEAM_LOGIN_TIMEOUT:-1200}"
    local deadline=$((SECONDS + timeout))
    local login_file="$STEAM_ROOT/config/loginusers.vdf"
    local steam_restarts=0
    while (( SECONDS < deadline )); do
        if [[ -f "$login_file" ]]; then
            local login_mtime
            login_mtime="$(stat -c %Y "$login_file" 2>/dev/null || echo 0)"
            if (( login_mtime + 2 >= STEAM_LOGIN_STARTED_EPOCH )); then
                local from_file
                from_file="$(python3 "$STEAM_ID_SCRIPT" "$LOGIN" "$login_file" 2>/dev/null || true)"
                if [[ "$from_file" =~ ^[0-9]{17}$ ]]; then
                    LOGIN_STEAM_ID="$from_file"
                    sleep "${SM_STEAM_LOGIN_SETTLE_SECONDS:-15}"
                    return 0
                fi
            fi
        fi

        # Recent Steam clients do not always write loginusers.vdf for a
        # command-line login. The connection log has the authoritative log-on
        # response and the account id; use the part written after this launch.
        local from_log
        from_log="$(login_id_from_log 2>/dev/null || true)"
        if [[ "$from_log" =~ ^[0-9]{17}$ ]]; then
            LOGIN_STEAM_ID="$from_log"
            sleep "${SM_STEAM_LOGIN_SETTLE_SECONDS:-15}"
            return 0
        fi
        if ! pgrep -x steam >/dev/null 2>&1 \
            && ! kill -0 "$STEAM_PID" 2>/dev/null; then
            # The Steam launcher can exit after installing/updating its
            # bootstrap while the client is still being prepared. Relaunch it
            # a few times instead of treating that as a login failure.
            if (( steam_restarts >= 5 )); then
                log "Steam client exited repeatedly during login"
                tail -n 30 "$CACHE_DIR/steam-client.log" >&2 || true
                return 1
            fi
            steam_restarts=$((steam_restarts + 1))
            log "Steam client exited during startup; restarting ($steam_restarts/5)"
            sleep 3
            start_steam
            continue
        fi
        sleep 2
    done
    log "Steam login timed out after ${timeout}s"
    tail -n 40 "$CACHE_DIR/steam-client.log" >&2 || true
    return 1
}

determine_steam_id() {
    local candidate="${SM_STEAM_ID:-${LOGIN_STEAM_ID:-}}"
    if [[ -z "$candidate" && -f "$STEAM_ROOT/config/loginusers.vdf" ]]; then
        candidate="$(python3 "$STEAM_ID_SCRIPT" "$LOGIN" "$STEAM_ROOT/config/loginusers.vdf" 2>/dev/null || true)"
    fi
    if [[ -z "$candidate" ]]; then
        local dirs=( "$SAVES_ROOT"/User_* )
        if [[ ${#dirs[@]} -eq 1 && -d "${dirs[0]}" ]]; then
            candidate="${dirs[0]##*User_}"
        fi
    fi
    [[ "$candidate" =~ ^[0-9]{17}$ ]] || die "could not determine the server SteamID"
    STEAM_ID="$candidate"
    ACCOUNT_ID="$((10#$STEAM_ID - 76561197960265728))"
    log "using SteamID $STEAM_ID (account $ACCOUNT_ID)"
}

ensure_compat_tool() {
    [[ -d "$GE_PROTON_DIR" ]] || die "GE-Proton not found at $GE_PROTON_DIR"
    mkdir -p "$STEAM_ROOT/compatibilitytools.d"
    ln -sfn "$GE_PROTON_DIR" "$STEAM_ROOT/compatibilitytools.d/$GE_PROTON_NAME"
    log "registered compatibility tool $GE_PROTON_NAME"
}

run_steamcmd() {
    local log_file="$CACHE_DIR/steamcmd.log"
    local timeout_seconds="${SM_STEAMCMD_TIMEOUT:-1800}"
    local deadline=$((SECONDS + timeout_seconds))
    log "updating app $APP_ID through SteamCMD"
    set +e
    timeout "$timeout_seconds" steamcmd \
        +force_install_dir "$GAME_DIR" \
        +@sSteamCmdForcePlatformType windows \
        +login "$LOGIN" "$PASSWORD" \
        +app_update "$APP_ID" \
        +quit >"$log_file" 2>&1
    local rc=$?
    set -e
    if [[ $rc -ne 0 ]]; then
        tail -n 50 "$log_file" >&2 || true
        die "SteamCMD failed with exit code $rc"
    fi

    # steamcmd.sh can return after a self-update while the real linux32
    # client keeps downloading. Wait for that child before checking the
    # installation, otherwise a fresh install looks incomplete.
    while pgrep -f '^/opt/steamcmd/linux32/steamcmd( |$)' >/dev/null 2>&1; do
        if (( SECONDS >= deadline )); then
            tail -n 50 "$log_file" >&2 || true
            die "SteamCMD did not finish within ${timeout_seconds}s"
        fi
        sleep 2
    done

    # SteamCMD installs app manifests beside the forced install directory.
    # Mirror the manifest into the Steam library so the Steam client sees the
    # same installed build as SteamCMD.
    if [[ -f "$GAME_DIR/steamapps/appmanifest_$APP_ID.acf" ]]; then
        mkdir -p "$STEAM_ROOT/steamapps"
        cp -f "$GAME_DIR/steamapps/appmanifest_$APP_ID.acf" \
            "$STEAM_ROOT/steamapps/appmanifest_$APP_ID.acf"
    fi

    if [[ ! -f "$GAME_EXE" ]]; then
        tail -n 50 "$log_file" >&2 || true
        die "game executable was not installed at $GAME_EXE"
    fi
}

install_or_update_game() {
    if [[ "${SM_SKIP_UPDATE:-false}" == "true" ]]; then
        [[ -f "$GAME_EXE" ]] || die "SM_SKIP_UPDATE=true but $GAME_EXE is missing"
        log "skipping SteamCMD update"
        return 0
    fi
    run_steamcmd
}

patch_proton_mapping() {
    local config="$STEAM_ROOT/config/config.vdf"
    local output=""
    local status=0

    # Steam rewrites config.vdf when it exits. Stop it first, then patch the
    # on-disk file, so the mapping cannot be overwritten by the shutdown flush.
    log "stopping Steam before writing the compatibility mapping"
    stop_steam
    output="$(python3 "$PATCH_SCRIPT" "$config" "$APP_ID" "$GE_PROTON_NAME" 2>&1)" || status=$?
    if [[ $status -ne 0 ]]; then
        log "could not update Proton mapping: $output"
        die "Steam compatibility mapping could not be configured"
    fi
    log "Proton mapping: $output"

    # Steam shows the game EULA before the first launch. The server is
    # headless, so write the accepted marker while Steam is stopped.
    local localconfig="$STEAM_ROOT/userdata/$ACCOUNT_ID/config/localconfig.vdf"
    if [[ -f "$localconfig" ]]; then
        output="$(python3 "$EULA_SCRIPT" "$localconfig" "$APP_ID" 3 2>&1)" || status=$?
        if [[ $status -ne 0 ]]; then
            log "could not record EULA acceptance: $output"
            die "Steam EULA acceptance could not be configured"
        fi
        log "EULA: $output"
    else
        log "EULA: localconfig not found, leaving Steam EULA state unchanged"
    fi

    start_steam
    wait_for_login || die "Steam did not log in after applying the Proton mapping"
}

seed_save() {
    local user_dir="$SAVES_ROOT/User_$STEAM_ID"
    TARGET_DIR="$user_dir/$SAVE_SUBDIR"
    TARGET="$TARGET_DIR/$SAVE_FILENAME"
    mkdir -p "$TARGET_DIR"
    chmod 770 "$user_dir" "$TARGET_DIR" 2>/dev/null || true

    if [[ -f "$TARGET" ]]; then
        validate_save "$TARGET" || die "existing save is invalid: $TARGET"
        log "using existing save $TARGET"
        return 0
    fi

    if [[ ! -f "$SEED_FILE" ]]; then
        die "save is missing and no seed exists at $SEED_FILE"
    fi
    validate_save "$SEED_FILE" || die "seed save is invalid: $SEED_FILE"
    install -m 0660 "$SEED_FILE" "$TARGET"
    validate_save "$TARGET" || die "copied save failed validation: $TARGET"
    log "seeded save from $SEED_FILE"
}

validate_save() {
    python3 "$VALIDATE_SCRIPT" "$1" >/dev/null
}

install_null_render() {
    local target="$GAME_DIR/Release/d3d11.dll"
    if [[ "${SM_NULL_RENDER:-true}" != "true" ]]; then
        if [[ -f "$target" && -f "$NULL_RENDER_DLL" ]] && cmp -s "$target" "$NULL_RENDER_DLL"; then
            rm -f "$target"
            log "removed null-render D3D11 proxy"
        fi
        return 0
    fi
    if [[ ! -f "$NULL_RENDER_DLL" ]]; then
        log "null-render DLL not found: $NULL_RENDER_DLL"
        return 1
    fi
    install -m 0644 "$NULL_RENDER_DLL" "$target"
    log "installed null-render D3D11 proxy"
}

install_settings() {
    if [[ "${SM_LOW_GRAPHICS:-true}" != "true" ]]; then
        return 0
    fi
    local settings="$SAVES_ROOT/User_$STEAM_ID/settings.json"
    install -m 0660 "$LOW_SETTINGS" "$settings"
    log "installed low graphics settings"
}

make_windows_path() {
    local relative="${TARGET#"$PREFIX/drive_c/"}"
    if [[ "$relative" == "$TARGET" ]]; then
        die "save path is outside the Proton prefix: $TARGET"
    fi
    WIN_SAVE="C:/$relative"
    log "launch save path: $WIN_SAVE"
}

write_marker() {
    python3 - "$MARKER" "$$" "$STEAM_ID" "$SAVE_FILENAME" "$WIN_SAVE" <<'PY'
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

path, pid, steam_id, save_name, save_path = sys.argv[1:6]
payload = {
    "pid": int(pid),
    "started_at": datetime.now(timezone.utc).isoformat(),
    "started_epoch": time.time(),
    "steam_id": steam_id,
    "save_name": save_name,
    "save_path": save_path,
}
directory = os.path.dirname(path)
os.makedirs(directory, exist_ok=True)
fd, temp_name = tempfile.mkstemp(prefix=".sm-marker.", dir=directory)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp_name, 0o660)
    os.replace(temp_name, path)
except Exception:
    try:
        os.unlink(temp_name)
    except OSError:
        pass
    raise
PY
    chmod 660 "$MARKER" 2>/dev/null || true
}

start_heartbeat() {
    (
        while [[ -f "$MARKER" ]]; do
            touch "$MARKER" 2>/dev/null || true
            sleep 10
        done
    ) &
    HEARTBEAT_PID=$!
}

launch_game() {
    write_marker
    start_heartbeat
    log "launching app $APP_ID through Steam"
    local extra_args=()
    if [[ -n "${SM_GAME_EXTRA_ARGS:-}" ]]; then
        read -r -a extra_args <<< "$SM_GAME_EXTRA_ARGS"
    fi
    "$STEAM_BIN" -applaunch "$APP_ID" -dedicated_server -console \
        "${extra_args[@]}" -fps "$SM_FPS_CAP" -open "$WIN_SAVE" \
        >>"$CACHE_DIR/steam-launch.log" 2>&1 &
    local launcher_pid=$!

    local timeout="${SM_GAME_START_TIMEOUT:-300}"
    local i
    for i in $(seq 1 "$timeout"); do
        if pgrep -f "$GAME_PROCESS_PATTERN" >/dev/null 2>&1; then
            log "game process is running"
            break
        fi
        if ! kill -0 "$launcher_pid" 2>/dev/null && ! pgrep -x steam >/dev/null 2>&1; then
            log "Steam exited before the game started"
            tail -n 40 "$CACHE_DIR/steam-launch.log" >&2 || true
            return 1
        fi
        sleep 1
    done
    if ! pgrep -f "$GAME_PROCESS_PATTERN" >/dev/null 2>&1; then
        log "game did not start within ${timeout}s"
        tail -n 60 "$CACHE_DIR/steam-launch.log" >&2 || true
        return 1
    fi
    wait "$launcher_pid" 2>/dev/null || true
    while pgrep -f "$GAME_PROCESS_PATTERN" >/dev/null 2>&1; do
        sleep 5
    done
    log "game process exited"
}

cleanup() {
    local rc=$?
    if [[ "$CLEANED" -eq 1 ]]; then
        return
    fi
    CLEANED=1
    trap - EXIT TERM INT HUP

    if pgrep -f "$GAME_PROCESS_PATTERN" >/dev/null 2>&1; then
        log "stopping game"
        if command -v xdotool >/dev/null 2>&1; then
            local wid
            for wid in $(xdotool search --onlyvisible --name 'Scrap Mechanic' 2>/dev/null || true); do
                xdotool windowactivate --sync "$wid" >/dev/null 2>&1 || true
                xdotool key --window "$wid" --clearmodifiers alt+F4 >/dev/null 2>&1 || true
            done
        fi
        if command -v wmctrl >/dev/null 2>&1; then
            wmctrl -c 'Scrap Mechanic' >/dev/null 2>&1 || true
        fi
        local i
        for i in $(seq 1 30); do
            pgrep -f "$GAME_PROCESS_PATTERN" >/dev/null 2>&1 || break
            sleep 1
        done
        if pgrep -f "$GAME_PROCESS_PATTERN" >/dev/null 2>&1; then
            pkill -TERM -f "$GAME_PROCESS_PATTERN" 2>/dev/null || true
            for i in $(seq 1 15); do
                pgrep -f "$GAME_PROCESS_PATTERN" >/dev/null 2>&1 || break
                sleep 1
            done
        fi
        pkill -KILL -f "$GAME_PROCESS_PATTERN" 2>/dev/null || true
    fi

    if [[ -n "$PREFIX" && -d "$PREFIX" && -x "$GE_PROTON_DIR/files/bin/wineserver" ]]; then
        WINEPREFIX="$PREFIX" PATH="$GE_PROTON_DIR/files/bin:$PATH" \
            timeout 20 wineserver -k >/dev/null 2>&1 || true
    fi

    pkill -TERM -f '^/opt/steamcmd/linux32/steamcmd( |$)' 2>/dev/null || true
    stop_steam
    if [[ -n "$HEARTBEAT_PID" ]]; then
        kill "$HEARTBEAT_PID" 2>/dev/null || true
        wait "$HEARTBEAT_PID" 2>/dev/null || true
    fi
    # Remove the marker only after the game and Steam are actually down, so a
    # restore cannot race a server that is still shutting down.
    rm -f "$MARKER" 2>/dev/null || true
    if [[ -n "$OPENBOX_PID" ]]; then
        kill "$OPENBOX_PID" 2>/dev/null || true
    fi
    if [[ -n "$VNC_PID" ]]; then
        kill "$VNC_PID" 2>/dev/null || true
    fi
    if [[ -n "$XVFB_PID" ]]; then
        kill "$XVFB_PID" 2>/dev/null || true
    fi
    if [[ -n "${DBUS_SESSION_BUS_PID:-}" ]]; then
        kill "$DBUS_SESSION_BUS_PID" 2>/dev/null || true
    fi
    log "cleanup complete"
    return "$rc"
}

trap cleanup EXIT
trap 'log "received termination signal"; exit 0' TERM INT HUP

main() {
    require_config
    log "Scrap Mechanic dedicated server starting"
    log "save: $SAVE_NAME (mode=$GAME_MODE)"
    start_x
    start_vnc || die "x11vnc failed to start"
    ensure_compat_tool
    install_or_update_game
    start_steam
    wait_for_login || die "Steam login failed"
    determine_steam_id
    patch_proton_mapping
    seed_save
    install_settings
    install_null_render
    make_windows_path
    launch_game || die "game launch failed"
}

main "$@"
