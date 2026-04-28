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

- `docs/` — design notes and diagrams
- `src/` — Python services that run on the Raspberry Pi gateway
  - `config.py` — shared config (topics, broker, API key, thresholds)
  - `db.py` — SQLite whitelist + cosine-similarity match
  - `capture.py` — camera + face-embedding hooks (stubbed off-Pi)
  - `mqtt_service.py` — MQTT publisher + 60 s heartbeat loop
  - `api_service.py` — Flask REST API (registration, detection toggle)
  - `detection.py` — single-trigger detection flow used by the Arduino bridge
- `test/` — pytest suite (config, db, capture, mqtt, detection, api)
- `pyproject.toml` — project metadata, deps, and ruff config (managed with `uv`)
- `.env.example` — config template

## Topic plan (from `docs/Architecture.pdf`)

| Topic                  | Purpose                                                      |
| ---------------------- | ------------------------------------------------------------ |
| `home/security/alerts` | Unknown-face alerts (drive mobile notifications). QoS 1.     |
| `home/security/events` | Proximity / access-granted / toggle / low-quality-capture.   |
| `home/security/status` | Retained heartbeat every 60 s with uptime + detection state. |

## Setup

This project uses [`uv`](https://docs.astral.sh/uv/) for env + dependency
management and [`ruff`](https://docs.astral.sh/ruff/) for lint/format.

```bash
# One-time: install uv (macOS/Linux)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install project deps (incl. dev group) into .venv
uv sync

# Config
cp .env.example .env   # then edit API_KEY, MQTT_HOST, etc.
set -a; source .env; set +a
```

On the Raspberry Pi, also pull in the hardware-only extras:

```bash
uv sync --extra pi
```

## Run

### 1. Start an MQTT broker

Both services publish to `$MQTT_HOST:$MQTT_PORT` (default `localhost:1883`). If no
broker is reachable, the services still start and retry in the background — but
publishes are dropped until the broker is up. Easiest local option is
[Mosquitto](https://mosquitto.org/):

```bash
# macOS — Homebrew installs the binary but ships only a .conf.example,
# so use the dev config checked into this repo and run it in the foreground
# (avoids flaky `brew services` / launchd errors):
brew install mosquitto
mosquitto -c dev/mosquitto.conf -v
# Tip: if `mosquitto` isn't on PATH, use the full path:
# /opt/homebrew/opt/mosquitto/sbin/mosquitto -c dev/mosquitto.conf -v

# Debian / Raspberry Pi OS
sudo apt install -y mosquitto mosquitto-clients
sudo systemctl enable --now mosquitto

# Docker (no install required)
docker run -it --rm -p 1883:1883 eclipse-mosquitto
```

Smoke-test the broker from another shell:

```bash
mosquitto_sub -h localhost -t 'home/security/#' -v
```

### 2. Start the service

For normal use, run the combined service — it serves the REST control plane
**and** runs the MQTT publisher + 60s heartbeat in the same process:

```bash
uv run camera-service
```

For broker-wiring smoke tests only, you can start the MQTT side by itself
(no HTTP, just the heartbeat + manual publishes):

```bash
uv run camera-mqtt
```

Either entry point can also be invoked as a module, e.g. `uv run python -m src.api_service`.
Do **not** run files as scripts (`python src/mqtt_service.py`) — they use relative
imports and need to be loaded as package members.

### Troubleshooting (browser 403, no HTTP lines in the terminal)

If Chrome shows **HTTP ERROR 403** on `http://127.0.0.1:…` but the Flask process prints **no** werkzeug line like `"GET / HTTP/1.1"`, the request never reached **camera-service**. On macOS, **AirPlay Receiver** commonly binds **TCP port 5000**, so traffic can go to macOS instead of Flask (403, empty body). Fix one of: set **`API_PORT=5050`** in `.env` (the project default), open **`http://127.0.0.1:5050`**, or disable AirPlay Receiver under **System Settings → General → AirDrop & Handoff**. Confirm what listens with:

```bash
lsof -nP -iTCP -sTCP:LISTEN | grep -E '5000|5050'
```

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

`home/security/status` (retained, published every 60 s):

```json
{
  "topic": "home/security/status",
  "timestamp": "2026-04-14T18:33:05Z",
  "event_type": "heartbeat",
  "uptime_sec": 3660,
  "detection_enabled": true,
  "connected": true,
  "sensor_id": "front_door_cam"
}
```

## Security notes

- The Flask server binds to `API_HOST` (default `127.0.0.1`). Do **not** set
  it to `0.0.0.0` without also putting the service behind a firewall.
- Every non-health route requires the `X-API-Key` header. Requests missing
  or providing a wrong key get a `401`.
- The `capture.embed_face` stub hashes image bytes so local runs work
  without the `face_recognition` library; swap in the real implementation
  on the Pi (see the docstring in `src/capture.py`).

## Status

Early-stage / WIP.
