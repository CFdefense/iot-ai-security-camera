"""Emit startup status banner into TASK logs for ``security-system``."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

import serial

from ..camera.picam.probe import describe_picamera2
from ..data import db
from ..integrations.serial_bridge import format_serial_open_error
from . import config
from .task_logging import TASK_LEVEL

log = logging.getLogger("startup_banner")


def describe_database() -> tuple[bool, str]:
    """Return whether SQLite responds and absolute path."""
    try:
        with db.connect() as conn:
            conn.execute("SELECT 1").fetchone()
        resolved = Path(config.DB_PATH).expanduser().resolve()
        return True, resolved.as_posix()
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as e:
        return False, str(e)


def describe_serial_hardware(*, probe_wait_sec: float = 4.0, poll_interval_sec: float = 0.25) -> str:
    """Probe serial before the bridge thread attaches; brief wait so PTY/fake-sensor can appear."""
    if not config.SERIAL_PORT:
        return "skipped (SERIAL_PORT empty; SERIAL_PORT=. in .env disables on laptop)"

    deadline = time.monotonic() + probe_wait_sec
    last_line = ""
    while True:
        try:
            ser = serial.Serial(
                config.SERIAL_PORT,
                baudrate=config.SERIAL_BAUD,
                timeout=min(float(config.SERIAL_TIMEOUT), 1.0),
            )
            ser.close()
            return f"OK {config.SERIAL_PORT} @ {config.SERIAL_BAUD} baud"
        except serial.SerialException as e:
            last_line = "WARN " + format_serial_open_error(config.SERIAL_PORT, e) + " (bridge retries in background)"
            if time.monotonic() >= deadline:
                return last_line
            time.sleep(poll_interval_sec)


def mqtt_broker_snapshot(mqtt_svc: Any, *, max_wait_sec: float = 1.0) -> str:
    """Brief wait so connect_async can report connected."""
    host = mqtt_svc.host
    port_n = mqtt_svc.port
    elapsed = 0.0
    step = 0.15
    while elapsed < max_wait_sec and not mqtt_svc.broker_connected:
        time.sleep(step)
        elapsed += step
    if mqtt_svc.broker_connected:
        return f"connected {host}:{port_n}"
    return f"pending {host}:{port_n} (async broker handshake; retries continue; waited {elapsed:.1f}s)"


def _format_line(inner: str) -> str:
    clipped = inner if len(inner) <= 74 else inner[:71] + "..."
    return "| " + clipped.ljust(74) + " |"


def format_banner_lines(mqtt_svc: Any) -> list[str]:
    """Border + rows for systemd / terminal logs."""
    sep_eq = "+" + "=" * 78 + "+"
    sep_dash = "+" + "-" * 78 + "+"

    db_ok, detail = describe_database()
    mqtt_line = mqtt_broker_snapshot(mqtt_svc)
    mqtt_up = mqtt_line.startswith("connected ")
    camera_line = describe_picamera2()
    camera_up = camera_line.startswith("OK ")
    serial_line = describe_serial_hardware()
    # Sensor = serial bridge; optional when SERIAL_PORT is unset (laptop / no Arduino).
    sensor_up = serial_line.startswith("OK ") or serial_line.startswith("skipped")

    errors: list[str] = []
    if not db_ok:
        errors.append("database: " + detail)
    if not mqtt_up:
        errors.append("mqtt: " + mqtt_line)
    if not camera_up:
        errors.append("camera: " + camera_line)
    if not sensor_up:
        errors.append("sensor: " + serial_line)

    return [
        sep_eq,
        _format_line("SECURITY-SYSTEM STARTUP"),
        sep_eq,
        _format_line("COMPONENTS"),
        _format_line("  API      : UP"),
        _format_line("  MQTT     : " + ("UP" if mqtt_up else "DOWN")),
        _format_line("  DATABASE : " + ("UP" if db_ok else "DOWN")),
        _format_line("  CAMERA   : " + ("UP" if camera_up else "DOWN")),
        _format_line("  SENSOR   : " + ("UP" if sensor_up else "DOWN")),
        _format_line("-" * 30),
        _format_line("NOTES"),
        *([_format_line("- none")] if not errors else [_format_line("- " + e) for e in errors]),
        _format_line("-" * 30),
        _format_line("RUNTIME"),
        _format_line(
            f"Dashboard: http://{config.API_HOST}:{config.API_PORT}",
        ),
        _format_line(
            (
                "REST auth: API key configured"
                if config.API_KEY
                else "REST auth: API key missing (REST 500, /login 503)"
            ),
        ),
        _format_line(f"Capture artifacts: {(config.ARTIFACTS_DIR / 'capture').as_posix()}"),
        sep_dash,
    ]


def log_banner(mqtt_svc: Any) -> None:
    """Emit one TASK row per banner line."""
    for line in format_banner_lines(mqtt_svc):
        log.log(TASK_LEVEL, line)
