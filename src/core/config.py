"""Shared configuration for the MQTT and REST services.

Values are taken from environment variables when available so the same code can
run locally (dev laptop) and on the Raspberry Pi gateway. Defaults match the
Architecture document in docs/Architecture.pdf.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # repo root (not ``src/``)
load_dotenv(PROJECT_ROOT / ".env")
ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"
DB_PATH = Path(os.environ.get("CAMERA_DB_PATH", PROJECT_ROOT / "whitelist.sqlite"))

# Serial Configuration
# Set SERIAL_PORT= (empty value) in .env to skip the Arduino serial bridge on laptops without hardware.
SERIAL_PORT_RAW = os.environ.get("SERIAL_PORT")
SERIAL_PORT = SERIAL_PORT_RAW.strip() if SERIAL_PORT_RAW is not None else "/dev/ttyACM0"
SERIAL_BAUD = int(os.environ.get("SERIAL_BAUD", "115200"))
SERIAL_TIMEOUT = float(os.environ.get("SERIAL_TIMEOUT", "1.0"))

# MQTT configuration
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_KEEPALIVE = int(os.environ.get("MQTT_KEEPALIVE", "60"))
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "pi-security-camera")

TOPIC_ALERTS = "home/security/alerts"
TOPIC_EVENTS = "home/security/events"
TOPIC_STATUS = "home/security/status"

# Heartbeat cadence from the architecture doc: "Periodic heartbeat is
# published every 60 seconds" on home/security/status.
HEARTBEAT_INTERVAL_SEC = int(os.environ.get("HEARTBEAT_INTERVAL_SEC", "60"))

# Detection configuration
# Cosine similarity threshold for face match (see the Architecture document).
MATCH_THRESHOLD = float(os.environ.get("MATCH_THRESHOLD", "0.6"))
SENSOR_ID = os.environ.get("SENSOR_ID", "front_door_cam")

# REST API configuration
# Binds to the local network interface only (not 0.0.0.0), per the
API_HOST = os.environ.get("API_HOST", "127.0.0.1")
# Default 5050: on macOS, AirPlay Receiver often listens on TCP 5000 — browsers then
# hit that service (403, no Flask logs) while security-system appears "healthy".
API_PORT = int(os.environ.get("API_PORT", "5050"))
API_KEY = os.environ.get("API_KEY", "").strip()
API_KEY_HEADER = "X-API-Key"

# Web dashboard session auth; dashboard_users reset from env once at Flask startup (create_app).
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
USER_EMAIL = os.environ.get("USER_EMAIL", "").strip()
USER_PASSWORD = os.environ.get("USER_PASSWORD", "")

ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
