from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from conftest import write_save
from test_logs import log_text, write_log

HEADERS = {"Authorization": "Bearer test-token"}


def make_client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def test_health_and_auth(settings: Settings, save_path: Path) -> None:
    with make_client(settings) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
        assert client.get("/api/v1/status").status_code == 401
        assert client.get("/api/v1/status", headers={"Authorization": "Bearer nope"}).status_code == 401
        response = client.get("/api/v1/status", headers=HEADERS)
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["save"]["validation"]["valid"] is True
        assert payload["server"]["state"] == "stopped"


def test_metrics(settings: Settings, save_path: Path) -> None:
    with make_client(settings) as client:
        assert client.get("/metrics").status_code == 401
        response = client.get("/metrics", headers=HEADERS)
        assert response.status_code == 200
        assert "sm_server_up" in response.text
        assert "sm_savegameversion" in response.text


def test_backup_download_and_restore(settings: Settings, save_path: Path) -> None:
    with make_client(settings) as client:
        response = client.post("/api/v1/backups", headers=HEADERS)
        assert response.status_code == 201, response.text
        backup = response.json()
        assert backup["id"]
        assert backup["gametick"] == 100

        listing = client.get("/api/v1/backups", headers=HEADERS).json()
        assert listing["stats"]["count"] == 1

        downloaded = client.get(
            f"/api/v1/backups/{backup['id']}/download", headers=HEADERS
        )
        assert downloaded.status_code == 200
        assert downloaded.content[:16] == b"SQLite format 3\x00"

        write_save(save_path, gametick=777)
        restore = client.post(
            f"/api/v1/backups/{backup['id']}/restore",
            headers=HEADERS,
            json={"confirm_server_stopped": True},
        )
        assert restore.status_code == 200, restore.text
        assert restore.json()["restored"] is True

        status = client.get("/api/v1/status", headers=HEADERS).json()
        assert status["save"]["validation"]["gametick"] == 100


def test_restore_requires_confirmation(settings: Settings, save_path: Path) -> None:
    with make_client(settings) as client:
        backup = client.post("/api/v1/backups", headers=HEADERS).json()
        response = client.post(
            f"/api/v1/backups/{backup['id']}/restore",
            headers=HEADERS,
            json={},
        )
        assert response.status_code == 400


def test_config_patch(settings: Settings, save_path: Path) -> None:
    with make_client(settings) as client:
        response = client.patch(
            "/api/v1/config",
            headers=HEADERS,
            json={"backup_auto": True, "backup_interval_minutes": 5},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["backup"]["auto"] is True
        assert payload["backup"]["interval_minutes"] == 5

        bad = client.patch(
            "/api/v1/config",
            headers=HEADERS,
            json={"save_name": "../bad"},
        )
        assert bad.status_code == 422


def test_logs_and_players(settings: Settings, save_path: Path) -> None:
    write_log(settings, log_text())
    with make_client(settings) as client:
        response = client.get("/api/v1/logs?tail=20", headers=HEADERS)
        assert response.status_code == 200
        assert any("Game version" in line for line in response.json()["lines"])
        players = client.get("/api/v1/players", headers=HEADERS)
        assert players.status_code == 200
        # The background log loop may not have polled yet; force a status/log read
        # by waiting briefly.
        time.sleep(0.3)
        players = client.get("/api/v1/players", headers=HEADERS)
        assert players.json()["online"] in {0, 1}


def test_async_backup_operation(settings: Settings, save_path: Path) -> None:
    with make_client(settings) as client:
        response = client.post("/api/v1/backups?wait=false", headers=HEADERS)
        assert response.status_code == 202, response.text
        operation = response.json()["operation"]
        assert operation["status"] in {"queued", "running", "completed"}
        deadline = time.time() + 3
        while time.time() < deadline:
            current = client.get(
                f"/api/v1/operations/{operation['id']}", headers=HEADERS
            ).json()
            if current["status"] in {"completed", "failed"}:
                break
            time.sleep(0.05)
        assert current["status"] == "completed", current
