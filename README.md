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
- `pyproject.toml` — project metadata, deps, and ruff config (managed with `uv`)
- `.env.example` — config template

## Topic plan (from `docs/Architecture.pdf`)

| Topic | Purpose |
| --- | --- |
| `home/security/alerts` | Unknown-face alerts (drive mobile notifications). QoS 1. |
| `home/security/events` | Proximity / access-granted / toggle / low-quality-capture. |
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

Start the MQTT heartbeat on its own (useful when only testing the broker):

```bash
uv run camera-mqtt
```

Start the REST API (also starts the MQTT client + heartbeat in-process):

```bash
uv run camera-api
```

Either entry point can also be invoked as a module, e.g. `uv run python -m src.api_service`.

## Lint / format / test

```bash
uv run ruff check src/          # lint
uv run ruff check --fix src/    # autofix
uv run ruff format src/         # format
uv run pytest                   # tests (when added)
```

## Example REST calls

Register a new whitelist user (triggers a camera capture on the Pi):

```bash
curl -X POST http://127.0.0.1:5000/users/register \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "christian"}'
```

Toggle detection off (publishes a `detection_toggle` event on `home/security/events`):

```bash
curl -X POST http://127.0.0.1:5000/detection/toggle \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'
```

List registered users:

```bash
curl http://127.0.0.1:5000/users -H "X-API-Key: $API_KEY"
```

Liveness probe (no auth):

```bash
curl http://127.0.0.1:5000/healthz
```

## Example published messages

`home/security/alerts`:

```json
{
  "topic": "home/security/alerts",
  "timestamp": "2026-04-14T18:32:05Z",
  "event_type": "unknown_face_detected",
  "confidence": 0.34,
  "image_ref": "captures/2026-04-14_183205.jpg",
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
