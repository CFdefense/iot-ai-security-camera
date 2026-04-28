"""Emit startup status banner into INFO logs for ``camera-service``."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

import serial

from ..data import db
from ..integrations.serial_bridge import format_serial_open_error
from . import config

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


def describe_serial_hardware() -> str:
    """One-off serial probe before the daemon thread attaches."""
    if not config.SERIAL_PORT:
        return "skipped (SERIAL_PORT empty; SERIAL_PORT=. in .env disables on laptop)"

    try:
        ser = serial.Serial(
            config.SERIAL_PORT,
            baudrate=config.SERIAL_BAUD,
            timeout=min(float(config.SERIAL_TIMEOUT), 1.0),
        )
        ser.close()
        return f"OK {config.SERIAL_PORT} @ {config.SERIAL_BAUD} baud"
    except serial.SerialException as e:
        return "WARN " + format_serial_open_error(config.SERIAL_PORT, e) + " (background bridge will retry)"


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
    clipped = inner if len(inner) <= 66 else inner[:63] + "..."
    return "| " + clipped.ljust(66) + " |"


def format_banner_lines(mqtt_svc: Any) -> list[str]:
    """Border + rows for systemd / terminal logs."""
    sep_eq = "+" + "=" * 70 + "+"
    sep_dash = "+" + "-" * 70 + "+"

    db_ok, detail = describe_database()

    prefix = "OK " if db_ok else "FAIL "
    db_line = prefix + detail

    return [
        sep_eq,
        _format_line("camera-service startup"),
        sep_eq,
        _format_line("Database (SQLite): " + db_line),
        _format_line("MQTT broker: " + mqtt_broker_snapshot(mqtt_svc)),
        _format_line("Arduino serial: " + describe_serial_hardware()),
        _format_line(
            f"REST API + dashboard: will bind {config.API_HOST}:{config.API_PORT}",
        ),
        _format_line(
            (
                "API key: configured for REST guards"
                if config.API_KEY
                else "API key: MISSING — set API_KEY (REST 500, /login 503)"
            ),
        ),
        sep_dash,
    ]


def log_banner(mqtt_svc: Any) -> None:
    """Emit one INFO row per banner line."""
    for line in format_banner_lines(mqtt_svc):
        log.info(line)
