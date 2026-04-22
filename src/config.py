"""Shared configuration for the MQTT and REST services.

Values are taken from environment variables when available so the same code can
run locally (dev laptop) and on the Raspberry Pi gateway. Defaults match the
Architecture document in docs/Architecture.pdf.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"
CAPTURES_DIR = PROJECT_ROOT / "captures"
DB_PATH = Path(os.environ.get("CAMERA_DB_PATH", PROJECT_ROOT / "whitelist.sqlite"))

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
API_PORT = int(os.environ.get("API_PORT", "5000"))
API_KEY = os.environ.get("API_KEY", "")
API_KEY_HEADER = "X-API-Key"

for d in (ARTIFACTS_DIR, CAPTURES_DIR):
    d.mkdir(parents=True, exist_ok=True)
