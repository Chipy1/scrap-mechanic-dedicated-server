from __future__ import annotations

from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest


class Metrics:
    def __init__(self, build_version: str) -> None:
        self.registry = CollectorRegistry()
        self.server_up = Gauge(
            "sm_server_up", "Whether the game server is considered up", registry=self.registry
        )
        self.server_state = Gauge(
            "sm_server_state",
            "Current server state (1 for the active state)",
            ["state"],
            registry=self.registry,
        )
        self.uptime = Gauge(
            "sm_uptime_seconds", "Server uptime in seconds", registry=self.registry
        )
        self.players = Gauge(
            "sm_players_online", "Connected players excluding the server account", registry=self.registry
        )
        self.game_info = Gauge(
            "sm_game_info", "Game version information", ["version"], registry=self.registry
        )
        self.save_size = Gauge(
            "sm_save_size_bytes", "Size of the active save in bytes", registry=self.registry
        )
        self.save_gametick = Gauge(
            "sm_save_gametick", "Game tick of the active save", registry=self.registry
        )
        self.savegameversion = Gauge(
            "sm_savegameversion", "Save game version of the active save", registry=self.registry
        )
        self.save_match = Gauge(
            "sm_save_match",
            "1 when the active log save matches SAVE_NAME, otherwise 0",
            registry=self.registry,
        )
        self.backup_last_success = Gauge(
            "sm_backup_last_success_timestamp_seconds",
            "Unix timestamp of the last successful backup",
            registry=self.registry,
        )
        self.backup_duration = Gauge(
            "sm_backup_duration_seconds", "Duration of the last backup", registry=self.registry
        )
        self.backup_size = Gauge(
            "sm_backup_size_bytes", "Size of the last backup in bytes", registry=self.registry
        )
        self.backup_failures = Gauge(
            "sm_backup_failures_total", "Backup failures since control-plane start", registry=self.registry
        )
        self.backup_total = Gauge(
            "sm_backup_total", "Successful backups since control-plane start", registry=self.registry
        )
        self.restore_total = Gauge(
            "sm_restore_total", "Successful restores since control-plane start", registry=self.registry
        )
        self.log_errors = Gauge(
            "sm_log_errors_total", "ERROR lines in the current game log", registry=self.registry
        )
        self.log_warnings = Gauge(
            "sm_log_warnings_total", "WARNING lines in the current game log", registry=self.registry
        )
        self.build_info = Gauge(
            "sm_control_build_info",
            "Control-plane build information",
            ["version"],
            registry=self.registry,
        )
        self.build_info.labels(version=build_version).set(1)

    def render(
        self,
        *,
        status: dict[str, Any],
        save_status: dict[str, Any],
        backup_stats: dict[str, Any],
        log_status: dict[str, Any],
    ) -> bytes:
        state = str(status.get("state", "unknown"))
        self.server_up.set(1 if status.get("up") else 0)
        self.server_state.clear()
        for known in ("running", "starting", "stopped", "error", "unknown"):
            self.server_state.labels(state=known).set(1 if known == state else 0)
        self.uptime.set(float(status.get("uptime_seconds") or 0.0))
        self.players.set(float(status.get("players_online") or 0))

        version = status.get("version") or "unknown"
        self.game_info.clear()
        self.game_info.labels(version=str(version)).set(1)

        validation = save_status.get("validation") or {}
        self.save_size.set(
            float(validation.get("size_bytes") or save_status.get("size_bytes") or 0)
        )
        self.save_gametick.set(float(validation.get("gametick") or 0))
        self.savegameversion.set(float(validation.get("savegameversion") or 0))
        match = status.get("save_match")
        self.save_match.set(1.0 if match is True else 0.0)

        self.backup_total.set(float(backup_stats.get("backup_total") or 0))
        self.backup_failures.set(float(backup_stats.get("backup_failures") or 0))
        self.restore_total.set(float(backup_stats.get("restore_total") or 0))
        self.backup_last_success.set(float(backup_stats.get("last_success_epoch") or 0))
        last = backup_stats.get("last") or {}
        self.backup_size.set(float(last.get("backup_size_bytes") or 0))
        self.backup_duration.set(
            float(
                last.get("duration_seconds")
                or backup_stats.get("last_duration_seconds")
                or 0
            )
        )

        self.log_errors.set(float(log_status.get("errors") or 0))
        self.log_warnings.set(float(log_status.get("warnings") or 0))
        return generate_latest(self.registry)


__all__ = ["Metrics", "CONTENT_TYPE_LATEST"]
