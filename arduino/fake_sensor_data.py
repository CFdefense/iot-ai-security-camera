#!/usr/bin/env python3
"""Emulate Arduino ``Serial.println`` NDJSON — same baud and line discipline as avoidance firmware.

Writes newline-terminated UTF-8 lines so :class:`serial.Serial` + ``readline()`` sees the same
chunks as hardware.

Modes:

* **pty** (default, macOS/Linux): creates a pseudoterminal slave path (e.g. ``/dev/ttysNNN``).
  Point your app at it: ``export SERIAL_PORT=<printed path>`` (use ``SERIAL_BAUD=115200``).
* **serial**: open a real path with pyserial (``--port``), e.g. loopback or ``socat`` pair.
* **stdout**: print lines for piping (``|``) or tests.

Only ``obstacle_detected`` events are emitted (matches what the serial bridge treats as a trigger).

When stdin is a terminal, events are sent **one per Enter** (default ``--count`` is 3). With a non-interactive stdin (pipes), the script falls back to timed spacing using ``--delay`` / ``--jitter``.

Usage examples:

  uv run fake-sensor
  python arduino/fake_sensor_data.py
  python arduino/fake_sensor_data.py --output serial --port /dev/tty.NNN
  python arduino/fake_sensor_data.py --output stdout | your_consumer
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import random
import sys
import time
from typing import TextIO

try:
    import pty
except ImportError:
    pty = None  # type: ignore[assignment, misc]


def build_obstacle_line(
    *,
    event_id: int,
    device_id: str,
    rule_name: str,
    ts_ms: int,
) -> str:
    """Return one JSON line matching ``avoidance_serial.ino`` obstacle JSON (no trailing newline)."""
    payload = {
        "device_id": device_id,
        "ts_ms": ts_ms,
        "event_id": event_id,
        "event_type": "obstacle_detected",
        "sensor": "avoidance",
        "value": 1,
        "rule": rule_name,
    }
    return json.dumps(payload, separators=(",", ":"))


def write_serial_line(fd: int, line: str) -> None:
    r"""Write one Arduino-style line: UTF-8 bytes plus ``\n`` (``Serial.println``)."""
    os.write(fd, (line + "\n").encode("utf-8"))


def write_serial_line_when_ready(
    fd: int,
    line: str,
    *,
    poll_s: float = 0.05,
    max_wait_s: float = 120.0,
) -> None:
    """Write one line to a PTY master, retrying until a reader opens the slave (avoids macOS EIO)."""
    data = (line + "\n").encode("utf-8")
    deadline = time.monotonic() + max_wait_s
    while True:
        try:
            os.write(fd, data)
            return
        except OSError as e:
            if e.errno != errno.EIO or time.monotonic() >= deadline:
                raise
            time.sleep(poll_s)


def write_text_stream(stream: TextIO, line: str) -> None:
    """Write one line to a text stream and flush (stdout / StringIO)."""
    stream.write(line + "\n")
    stream.flush()


def open_pty() -> tuple[int, str]:
    """Create a PTY and return ``(master_fd, slave_path)`` for the reader's ``SERIAL_PORT``."""
    if pty is None:
        raise RuntimeError("PTY mode is not available on this platform (install is Unix-like).")
    master_fd, slave_fd = pty.openpty()
    try:
        slave_path = os.ttyname(slave_fd)
    finally:
        os.close(slave_fd)
    return master_fd, slave_path


def main() -> int:
    """Parse CLI args and emit fake obstacle events in the chosen output mode."""
    parser = argparse.ArgumentParser(
        description="Fake Arduino serial NDJSON (obstacle_detected) for local development",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=3,
        help="number of events to emit (one per Enter when interactive; default 3)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.7,
        help="base delay between events when stdin is not a TTY (seconds)",
    )
    parser.add_argument(
        "--jitter",
        type=float,
        default=0.25,
        help="extra random delay (0..jitter seconds)",
    )
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for reproducible delays")
    parser.add_argument("--device-id", default="frank-arduino-01", help="device_id field (match firmware)")
    parser.add_argument(
        "--rule",
        default="avoidance_hysteresis_v1",
        help="rule field (match avoidance_serial.ino RULE_NAME)",
    )
    parser.add_argument(
        "--output",
        choices=("pty", "serial", "stdout"),
        default="pty",
        help="pty=virtual serial device (default), serial=--port, stdout=pipe-friendly",
    )
    parser.add_argument(
        "--port",
        default="",
        help="serial device path when --output serial (e.g. /dev/ttyUSB0 or socat PTY)",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=115200,
        help="baud rate when --output serial (Arduino uses 115200)",
    )
    args = parser.parse_args()

    if args.count <= 0:
        print("--count must be greater than 0", file=sys.stderr)
        return 2
    if args.delay < 0 or args.jitter < 0:
        print("--delay and --jitter must be >= 0", file=sys.stderr)
        return 2
    if args.output == "serial" and not args.port:
        print("--output serial requires --port", file=sys.stderr)
        return 2

    random.seed(args.seed)
    start_ns = time.monotonic_ns()

    master_fd: int | None = None
    ser = None  # Lazy ``serial.Serial`` when --output serial

    try:
        if args.output == "pty":
            try:
                master_fd, slave_path = open_pty()
            except (OSError, RuntimeError) as e:
                print(
                    f"Could not create PTY ({e}). Use --output stdout, or --output serial --port /path.",
                    file=sys.stderr,
                )
                return 2
            print(
                "Fake Arduino on PTY slave (same as plugging in serial — point pyserial here):\n"
                f"  export SERIAL_PORT={slave_path}\n"
                f"  export SERIAL_BAUD={args.baud}\n"
                "Start the camera/serial reader on that port (first line waits until the slave is open).\n"
                f"Then press Enter up to {args.count} time(s) here to send one fake obstacle JSON each.\n",
                file=sys.stderr,
                end="",
            )

            def out_write(line: str) -> None:
                assert master_fd is not None
                write_serial_line_when_ready(master_fd, line)

        elif args.output == "serial":
            import serial

            ser = serial.Serial(args.port, baudrate=args.baud, timeout=0.2)
            if sys.stdin.isatty():
                print(
                    f"Using serial {args.port} @ {args.baud}. "
                    f"Press Enter up to {args.count} time(s) to send one line each.\n",
                    file=sys.stderr,
                    end="",
                )

            def out_write(line: str) -> None:
                assert ser is not None
                ser.write((line + "\n").encode("utf-8"))
                ser.flush()

        else:

            def out_write(line: str) -> None:
                write_text_stream(sys.stdout, line)

            if sys.stdin.isatty():
                print(
                    f"Press Enter up to {args.count} time(s) to print one line to stdout each.\n",
                    file=sys.stderr,
                    end="",
                )

        interactive = sys.stdin.isatty()
        for i in range(1, args.count + 1):
            if interactive:
                try:
                    input(
                        f"Press Enter to send obstacle {i}/{args.count} … ",
                    )
                except EOFError:
                    break
            else:
                sleep_for = args.delay + random.uniform(0, args.jitter)
                time.sleep(sleep_for)
            ts_ms = int((time.monotonic_ns() - start_ns) / 1_000_000)
            line = build_obstacle_line(
                event_id=i,
                device_id=args.device_id,
                rule_name=args.rule,
                ts_ms=ts_ms,
            )
            out_write(line)
    finally:
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        if master_fd is not None:
            try:
                os.close(master_fd)
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
