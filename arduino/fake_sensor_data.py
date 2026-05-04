#!/usr/bin/env python3
"""Fake Arduino NDJSON over a PTY.

Run it, point ``SERIAL_PORT`` at the printed path, press Enter to fire ``obstacle_detected``.

  uv run fake-sensor

Then in another shell::

  export SERIAL_PORT=/dev/pts/N   # path printed by fake-sensor
  uv run security-system
"""

from __future__ import annotations

import errno
import json
import os
import sys
import time

try:
    import termios
    import tty
except ImportError:
    termios = None  # type: ignore[assignment, misc]
    tty = None  # type: ignore[assignment, misc]

try:
    import pty
except ImportError:
    pty = None  # type: ignore[assignment, misc]

_BAUD = 115200
_DEVICE_ID = "frank-arduino-01"
_RULE = "avoidance_hysteresis_v1"


def _obstacle_line(event_id: int, ts_ms: int) -> str:
    payload = {
        "device_id": _DEVICE_ID,
        "ts_ms": ts_ms,
        "event_id": event_id,
        "event_type": "obstacle_detected",
        "sensor": "avoidance",
        "value": 1,
        "rule": _RULE,
    }
    return json.dumps(payload, separators=(",", ":"))


def _wait_for_enter(stdin_fd: int) -> None:
    """Block until Enter (no readline — avoids Tab completion / layout junk on repeated sends)."""
    if termios is None or tty is None or not os.isatty(stdin_fd):
        line = sys.stdin.readline()
        if not line:
            raise EOFError
        return
    old = termios.tcgetattr(stdin_fd)
    try:
        tty.setcbreak(stdin_fd)
        while True:
            ch = os.read(stdin_fd, 1)
            if ch in (b"\n", b"\r"):
                return
            if ch == b"\x04":
                raise EOFError
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old)


def _write_line_when_ready(master_fd: int, line: str) -> None:
    data = (line + "\n").encode("utf-8")
    deadline = time.monotonic() + 120.0
    while True:
        try:
            os.write(master_fd, data)
            return
        except OSError as e:
            if e.errno != errno.EIO or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def main() -> int:
    """Open a PTY, print its path, send ``obstacle_detected`` lines when Enter is pressed."""
    if pty is None:
        print("PTY not available on this platform.", file=sys.stderr)
        return 2
    if not sys.stdin.isatty():
        print("Run fake-sensor in a terminal (stdin must be a TTY).", file=sys.stderr)
        return 2

    master_fd, slave_fd = pty.openpty()
    try:
        slave_path = os.ttyname(slave_fd)
    finally:
        os.close(slave_fd)

    print(
        "Point the app at this port (115200 baud):\n"
        f"  export SERIAL_PORT={slave_path}\n"
        f"  export SERIAL_BAUD={_BAUD}\n"
        "Press Enter to send one obstacle_detected line (Ctrl+C to quit).\n",
        file=sys.stderr,
        end="",
    )

    start_ns = time.monotonic_ns()
    event_id = 0
    try:
        while True:
            try:
                _wait_for_enter(sys.stdin.fileno())
            except EOFError:
                break
            event_id += 1
            ts_ms = int((time.monotonic_ns() - start_ns) / 1_000_000)
            _write_line_when_ready(master_fd, _obstacle_line(event_id, ts_ms))
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
