# iot-ai-security-camera

AI-powered security camera system (IoT + on-device/edge inference).

## Team

Psychic Securities

Christian Farrell, Jeremy Frank

## Idea

Smart Security Camera for Verified User Access (Computer Vision IoT system).

## Docs

- `docs/Proposal.pdf`
- `docs/Architecture.pdf`
- `docs/Block-Diagram.svg`

## Repo layout

- `docs/` — design docs and diagrams
- `src/security_system.py` — single process (REST + dashboard + MQTT + heartbeat + serial bridge)
- `src/mqtt/` — MQTT client package used by ``security_system``
- `src/data/db.py` — SQLite users + detection alerts
- `src/integrations/serial_bridge.py` — Arduino serial trigger bridge
- `src/web/` — Flask dashboard (`templates/`, `static/`, `web_ui.py`)
- `src/core/` — shared config, startup banner, status helpers
- `test/` — pytest suite
- `.env.example` — configuration template

## MQTT topics

- `home/security/alerts` — unknown-face alert notifications (QoS 1)
- `home/security/events` — user/activity events (register, unregister, toggle, etc.)
- `home/security/status` — retained heartbeat/status snapshots

## Setup

This project uses [`uv`](https://docs.astral.sh/uv/) for env + dependency
management and [`ruff`](https://docs.astral.sh/ruff/) for lint/format.

```bash
# One-time: install uv (macOS/Linux)
curl -LsSf https://astral.sh/uv/install.sh | sh

uv sync

cp .env.example .env   # then edit API_KEY, MQTT_HOST, etc.
set -a; source .env; set +a
```

**Raspberry Pi:** `sudo apt install python3-picamera2 python3-libcamera` (Picamera2 is not on PyPI here). Use a venv with `--system-site-packages` if `import picamera2` fails under `uv run`.

## Run

### 1. Start an MQTT broker

Both services publish to `$MQTT_HOST:$MQTT_PORT` (default `localhost:1883`). If no
broker is reachable, the services still start and retry in the background — but
publishes are dropped until the broker is up. Easiest local option is
[Mosquitto](https://mosquitto.org/):

```bash
# Arch Linux
sudo pacman -S mosquitto
sudo systemctl enable --now mosquitto

# macOS (Homebrew)
brew install mosquitto
mosquitto -c dev/mosquitto.conf -v

# Debian / Raspberry Pi OS
sudo apt install -y mosquitto mosquitto-clients
sudo systemctl enable --now mosquitto
```

Smoke-test the broker from another shell:

```bash
mosquitto_sub -h localhost -t 'home/security/#' -v
```

### 2. Start the service

For normal use, run the combined service — it serves the REST control plane
**and** runs the MQTT publisher + 60s heartbeat in the same process:

```bash
uv run security-system
```

Or: `uv run python -m src.security_system`. Use package imports (not `python path/to/file.py`).

### 3. Open the dashboard

- Go to `http://127.0.0.1:5050/dashboard`
- Log in with credentials from `.env` (`USER_EMAIL` / `USER_PASSWORD`)

## Lint / format / test

```bash
uv run ruff check .          # lint everything
uv run ruff check --fix .    # autofix
uv run ruff format .         # format
uv run pytest                # full pytest suite
uv run pytest -v test/test_mqtt_service.py   # run one module
```

## Example REST calls

Register a new whitelist user (triggers a camera capture on the Pi):

```bash
curl -X POST http://127.0.0.1:5050/users/register \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "christian"}'
```

Toggle detection off (publishes a `detection_toggle` event on `home/security/events`):

```bash
curl -X POST http://127.0.0.1:5050/detection/toggle \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'
```

List registered users:

```bash
curl http://127.0.0.1:5050/users -H "X-API-Key: $API_KEY"
```

Liveness probe (no auth):

```bash
curl http://127.0.0.1:5050/healthz
```

## Dashboard notes

- Status panel shows: `API`, `MQTT`, `Camera`, `Sensor` with `UP/DOWN`.
- `API` and `MQTT` publish status now.
- `Camera` and `Sensor` are `DOWN` until serial/camera status publishers are wired.
- Register/toggle/unregister are async (no full page reload).

## Example published messages

`home/security/alerts`:

```json
{
  "topic": "home/security/alerts",
  "timestamp": "2026-04-14T18:32:05Z",
  "event_type": "unknown_face_detected",
  "confidence": 0.34,
  "image_ref": "inline:2026-04-14_183205",
  "sensor_id": "front_door_cam"
}
```

`home/security/status` (retained):

```json
{
  "topic": "home/security/status",
  "timestamp": "2026-04-14T18:33:05Z",
  "event_type": "heartbeat",
  "uptime_sec": 3660,
  "detection_enabled": true,
  "connected": true,
  "components": {
    "mqtt": {"state": "up"},
    "api": {"state": "up"}
  },
  "sensor_id": "front_door_cam"
}
```

## Security notes

- The Flask server binds to `API_HOST` (default `127.0.0.1`). Do **not** set
  it to `0.0.0.0` without also putting the service behind a firewall.
- Every non-health route requires the `X-API-Key` header. Requests missing
  or providing a wrong key get a `401`.
- Camera: Picamera2 (OS packages on Pi) plus `face_recognition` / OpenCV from `uv sync`.

## Status

Early-stage / WIP.
