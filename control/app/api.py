from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .chat import read_messages, send_message
from .backups import (
    BackupBusy,
    BackupError,
    BackupInvalid,
    BackupManager,
    BackupNotFound,
    RestoreBlocked,
    RestoreConfirmationRequired,
)
from .config import BUILD_VERSION, RuntimeConfig, Settings
from .logs import LogMonitor
from .metrics import CONTENT_TYPE_LATEST, Metrics
from .saves import (
    AmbiguousSave,
    InvalidSave,
    PathSecurityError,
    SaveNotFound,
    SaveResolver,
)
from .server import ServerMonitor
from .store import OperationManager, OperationStore

logger = logging.getLogger(__name__)


class BackupRequest(BaseModel):
    trigger: str = Field(default="manual", description="manual or pre-update")


class RestoreRequest(BaseModel):
    confirm_server_stopped: bool = False
    force: bool = False


class ChatMessage(BaseModel):
    text: str = Field(min_length=1, max_length=400)


class ConfigPatch(BaseModel):
    save_name: str | None = None
    game_mode: str | None = None
    backup_interval_minutes: int | None = Field(default=None, ge=1)
    backup_retention_days: int | None = Field(default=None, ge=0)
    backup_retention_weeks: int | None = Field(default=None, ge=0)
    backup_auto: bool | None = None
    require_server_stopped: bool | None = None


def _auth_dependency(request: Request, authorization: str | None = Header(default=None)) -> None:
    token = request.app.state.settings.api_token
    if not token:
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    supplied = authorization.split(" ", 1)[1].strip()
    if not secrets.compare_digest(supplied, token):
        raise HTTPException(
            status_code=401,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def _log_loop(app: FastAPI) -> None:
    while True:
        try:
            await asyncio.to_thread(app.state.log_monitor.poll)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("log poll failed")
        await asyncio.sleep(max(0.1, app.state.settings.log_poll_seconds))


async def _backup_loop(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(30)
        settings: Settings = app.state.settings
        if not settings.backup_auto:
            continue
        now = time.time()
        interval = settings.backup_interval_minutes * 60
        if now - app.state.last_auto_backup_attempt < interval:
            continue
        app.state.last_auto_backup_attempt = now
        try:
            result = await asyncio.to_thread(
                app.state.backups.create_backup,
                "scheduled",
                only_if_changed=True,
            )
            if result:
                logger.info("automatic backup created: %s", result.get("id"))
        except BackupBusy:
            logger.info("automatic backup skipped: backup already running")
        except (SaveNotFound, InvalidSave) as exc:
            logger.warning("automatic backup skipped: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("automatic backup failed")


def _ready_checks(settings: Settings) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    for name, path in (
        ("backups", settings.backups_dir),
        ("state", settings.state_dir),
    ):
        try:
            checks[name] = path.exists() and os.access(path, os.W_OK)
        except OSError:
            checks[name] = False
    checks["saves_mounted"] = settings.saves_dir.exists()
    checks["logs_mounted"] = settings.logs_dir.exists()
    return checks


def create_app(settings: Settings | None = None) -> FastAPI:
    base_settings = settings or Settings.from_env()
    base_settings.validate()
    runtime_config = RuntimeConfig(base_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        base_settings.ensure_dirs()
        runtime_config.load()
        app.state.settings = base_settings
        app.state.config = runtime_config
        logs = LogMonitor(base_settings)
        resolver = SaveResolver(base_settings)
        monitor = ServerMonitor(base_settings, logs, resolver)
        store = OperationStore(base_settings.state_dir / "control.db")
        manager = BackupManager(base_settings, resolver, monitor)
        app.state.log_monitor = logs
        app.state.save_resolver = resolver
        app.state.server_monitor = monitor
        app.state.operation_store = store
        app.state.operations = OperationManager(store)
        app.state.backups = manager
        app.state.metrics = Metrics(base_settings.build_version)
        app.state.started_at = time.time()
        app.state.last_auto_backup_attempt = time.time()
        try:
            await asyncio.to_thread(logs.poll)
        except Exception:
            logger.exception("initial log poll failed")
        tasks = [
            asyncio.create_task(_log_loop(app), name="sm-log-loop"),
            asyncio.create_task(_backup_loop(app), name="sm-backup-loop"),
        ]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            store.close()

    app = FastAPI(
        title="Scrap Mechanic control-plane",
        version=BUILD_VERSION,
        lifespan=lifespan,
    )

    @app.exception_handler(SaveNotFound)
    async def _save_not_found(_: Request, exc: SaveNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc), "error": "save_not_found"})

    @app.exception_handler(AmbiguousSave)
    async def _ambiguous_save(_: Request, exc: AmbiguousSave) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc), "error": "ambiguous_save"})

    @app.exception_handler(InvalidSave)
    async def _invalid_save(_: Request, exc: InvalidSave) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc), "error": "invalid_save"})

    @app.exception_handler(PathSecurityError)
    async def _path_security(_: Request, exc: PathSecurityError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc), "error": "path_security"})

    @app.exception_handler(BackupNotFound)
    async def _backup_not_found(_: Request, exc: BackupNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc), "error": "backup_not_found"})

    @app.exception_handler(BackupBusy)
    async def _backup_busy(_: Request, exc: BackupBusy) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc), "error": "backup_busy"})

    @app.exception_handler(BackupInvalid)
    async def _backup_invalid(_: Request, exc: BackupInvalid) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc), "error": "backup_invalid"})

    @app.exception_handler(RestoreBlocked)
    async def _restore_blocked(_: Request, exc: RestoreBlocked) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc), "error": "restore_blocked"})

    @app.exception_handler(RestoreConfirmationRequired)
    async def _restore_confirm(_: Request, exc: RestoreConfirmationRequired) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc), "error": "confirmation_required"})

    @app.exception_handler(BackupError)
    async def _backup_error(_: Request, exc: BackupError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc), "error": "backup_error"})

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "version": BUILD_VERSION}

    @app.get("/readyz")
    async def readyz(request: Request) -> dict[str, Any]:
        checks = _ready_checks(request.app.state.settings)
        server_status = request.app.state.server_monitor.status()
        save_status = request.app.state.save_resolver.status(
            validate=not bool(server_status.get("up"))
        )
        return {
            "ready": all(checks.values()),
            "checks": checks,
            "save_found": save_status.get("found", False),
            "save_valid": (save_status.get("validation") or {}).get("valid"),
            "save_validation_skipped": save_status.get("validation_skipped"),
        }

    api = APIRouter(prefix="/api/v1", dependencies=[Depends(_auth_dependency)])

    @api.get("/status")
    async def api_status(request: Request) -> dict[str, Any]:
        status = request.app.state.server_monitor.status()
        save_status = request.app.state.save_resolver.status(
            validate=not bool(status.get("up"))
        )
        backup_stats = request.app.state.backups.stats()
        return {
            "ok": True,
            "server": status,
            "save": save_status,
            "players": {
                "online": status.get("players_online", 0),
                "list": status.get("players", []),
            },
            "backups": backup_stats,
            "control": {
                "build_version": BUILD_VERSION,
                "now": datetime.now(timezone.utc).isoformat(),
                "uptime_seconds": round(time.time() - request.app.state.started_at, 3),
                "paths": {
                    "saves": str(request.app.state.settings.saves_dir),
                    "logs": str(request.app.state.settings.logs_dir),
                    "backups": str(request.app.state.settings.backups_dir),
                },
            },
        }

    @api.get("/chat")
    async def api_chat(request: Request, tail: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
        messages = await asyncio.to_thread(
            read_messages, request.app.state.settings.saves_dir, tail
        )
        return {"messages": messages}

    @api.post("/chat")
    async def api_chat_send(request: Request, payload: ChatMessage) -> dict[str, Any]:
        try:
            await asyncio.to_thread(
                send_message, request.app.state.settings.saves_dir, payload.text
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True}

    @api.get("/players")
    async def api_players(request: Request) -> dict[str, Any]:
        status = request.app.state.server_monitor.status()
        return {
            "online": status.get("players_online", 0),
            "players": status.get("players", []),
            "state": status.get("state"),
        }

    @api.get("/logs")
    async def api_logs(
        request: Request,
        tail: int = Query(default=200, ge=1, le=5000),
    ) -> dict[str, Any]:
        lines = await asyncio.to_thread(request.app.state.log_monitor.tail, tail)
        info = request.app.state.log_monitor.to_dict()
        return {"path": info.get("path"), "lines": lines, "log": info}

    @api.get("/logs/stream")
    async def api_logs_stream(request: Request) -> StreamingResponse:
        async def event_source() -> AsyncIterator[str]:
            last_signature: tuple[Any, ...] | None = None
            while True:
                lines = await asyncio.to_thread(
                    request.app.state.log_monitor.tail, 100
                )
                info = request.app.state.log_monitor.to_dict()
                signature = (
                    info.get("path"),
                    info.get("last_line_at"),
                    len(lines),
                    lines[-1] if lines else "",
                )
                if signature != last_signature:
                    payload = json.dumps(
                        {"path": info.get("path"), "lines": lines, "log": info},
                        ensure_ascii=False,
                    )
                    yield f"event: log\ndata: {payload}\n\n"
                    last_signature = signature
                else:
                    yield ": keepalive\n\n"
                await asyncio.sleep(2)

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @api.get("/backups")
    async def api_backups(request: Request) -> dict[str, Any]:
        manager: BackupManager = request.app.state.backups
        return {
            "backups": manager.list_backups(),
            "stats": manager.stats(),
        }

    @api.post("/backups")
    async def api_create_backup(
        request: Request,
        body: BackupRequest | None = None,
        wait: bool = Query(default=True),
    ) -> Response:
        trigger = (body.trigger if body else "manual").strip().lower()
        if trigger not in {"manual", "pre-update"}:
            raise HTTPException(
                status_code=422,
                detail="trigger must be manual or pre-update",
            )
        manager: BackupManager = request.app.state.backups
        if wait:
            result = await asyncio.to_thread(manager.create_backup, trigger)
            if result is None:
                raise HTTPException(status_code=409, detail="backup was skipped")
            return JSONResponse(status_code=201, content=result)
        operation = request.app.state.operations.submit(
            "backup", lambda: manager.create_backup(trigger)
        )
        return JSONResponse(status_code=202, content={"operation": operation})

    @api.get("/backups/{backup_id}")
    async def api_get_backup(backup_id: str, request: Request) -> dict[str, Any]:
        return request.app.state.backups.get_backup(backup_id)

    @api.get("/backups/{backup_id}/download")
    async def api_download_backup(backup_id: str, request: Request) -> StreamingResponse:
        manager: BackupManager = request.app.state.backups
        manifest = manager.get_backup(backup_id)
        path = manager.backup_file_path(backup_id)
        filename = str(manifest.get("filename") or f"{backup_id}.db")

        def chunks():
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk

        return StreamingResponse(
            chunks(),
            media_type="application/vnd.sqlite3",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(path.stat().st_size),
                "Cache-Control": "no-store",
            },
        )

    @api.post("/backups/{backup_id}/restore")
    async def api_restore_backup(
        backup_id: str,
        request: Request,
        body: RestoreRequest | None = None,
        wait: bool = Query(default=True),
    ) -> Response:
        body = body or RestoreRequest()
        manager: BackupManager = request.app.state.backups
        manager.get_backup(backup_id)
        monitor: ServerMonitor = request.app.state.server_monitor
        if monitor.is_running():
            raise RestoreBlocked("server is still running; stop it before restore")
        if (
            request.app.state.settings.require_server_stopped
            and not body.confirm_server_stopped
            and not body.force
        ):
            raise RestoreConfirmationRequired(
                "confirm_server_stopped=true is required for restore"
            )
        if wait:
            result = await asyncio.to_thread(
                manager.restore,
                backup_id,
                confirm_server_stopped=body.confirm_server_stopped,
                force=body.force,
            )
            return JSONResponse(status_code=200, content=result)
        operation = request.app.state.operations.submit(
            "restore",
            lambda: manager.restore(
                backup_id,
                confirm_server_stopped=body.confirm_server_stopped,
                force=body.force,
            ),
        )
        return JSONResponse(status_code=202, content={"operation": operation})

    @api.delete("/backups/{backup_id}", status_code=204)
    async def api_delete_backup(backup_id: str, request: Request) -> Response:
        request.app.state.backups.delete_backup(backup_id)
        return Response(status_code=204)

    @api.get("/config")
    async def api_get_config(request: Request) -> dict[str, Any]:
        return request.app.state.config.public_dict()

    @api.patch("/config")
    async def api_patch_config(request: Request, body: ConfigPatch) -> dict[str, Any]:
        patch = body.model_dump(exclude_unset=True)
        if not patch:
            return request.app.state.config.public_dict()
        try:
            return request.app.state.config.update(patch)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @api.get("/operations")
    async def api_operations(request: Request, limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
        operations = request.app.state.operation_store.list(limit)
        return {"operations": operations}

    @api.get("/operations/{operation_id}")
    async def api_operation(operation_id: str, request: Request) -> dict[str, Any]:
        operation = request.app.state.operations.get(operation_id)
        if operation is None:
            raise HTTPException(status_code=404, detail="operation not found")
        return operation

    app.include_router(api)

    @app.get("/metrics", dependencies=[Depends(_auth_dependency)])
    async def metrics(request: Request) -> Response:
        status = request.app.state.server_monitor.status()
        save_status = request.app.state.save_resolver.status(
            validate=not bool(status.get("up"))
        )
        backup_stats = request.app.state.backups.stats()
        log_status = request.app.state.log_monitor.to_dict()
        body = request.app.state.metrics.render(
            status=status,
            save_status=save_status,
            backup_stats=backup_stats,
            log_status=log_status,
        )
        return Response(content=body, media_type=CONTENT_TYPE_LATEST)

    return app
