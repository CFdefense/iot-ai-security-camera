#!/usr/bin/env python3
"""Emit fake Arduino-style sensor events as newline-delimited JSON.

Usage examples:
  python arduino/fake_sensor_data.py
  python arduino/fake_sensor_data.py --count 10 --delay 0.8
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import UTC, datetime

EVENT_TYPES = (
    "obstacle_detected",
)


def build_event(event_id: int, device_id: str) -> dict:
    """Create one fake event payload."""
    event_type = EVENT_TYPES[0]
    blocked = True

    payload = {
        "device_id": device_id,
        "event_id": event_id,
        "event_type": event_type,
        "sensor": "avoidance",
        "blocked": blocked,
        "value": 1 if blocked else 0,
        "ts": datetime.now(UTC).isoformat(),
        "ts_ms": int(time.time() * 1000),
    }
    return payload


def main() -> int:
    """Parse CLI args, emit fake events with delays, and return an exit code."""
    parser = argparse.ArgumentParser(description="Fake Arduino sensor data generator")
    parser.add_argument("--count", type=int, default=8, help="number of events to emit")
    parser.add_argument("--delay", type=float, default=0.7, help="base delay between events (seconds)")
    parser.add_argument(
        "--jitter",
        type=float,
        default=0.25,
        help="extra random delay (0..jitter seconds)",
    )
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for reproducible output")
    parser.add_argument("--device-id", default="fake-arduino-01", help="device_id field value")
    args = parser.parse_args()

    if args.count <= 0:
        print("--count must be greater than 0", file=sys.stderr)
        return 2
    if args.delay < 0 or args.jitter < 0:
        print("--delay and --jitter must be >= 0", file=sys.stderr)
        return 2

    random.seed(args.seed)
    for event_id in range(1, args.count + 1):
        print(json.dumps(build_event(event_id=event_id, device_id=args.device_id)), flush=True)
        sleep_for = args.delay + random.uniform(0, args.jitter)
        time.sleep(sleep_for)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
