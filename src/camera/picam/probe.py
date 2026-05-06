"""Lightweight Picamera2 / libcamera probe for startup diagnostics (no capture)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_PICAMERA2_IMPORT_ERR: str | None = None
try:
    from picamera2 import Picamera2  # type: ignore[import-not-found]
except (ImportError, ValueError, ModuleNotFoundError) as e:
    Picamera2 = None  # type: ignore[assignment]
    _PICAMERA2_IMPORT_ERR = str(e).split("\n", 1)[0]


def _format_ok(infos: list[dict[str, Any]]) -> str:
    if not infos:
        return "WARN no libcamera cameras detected"
    model = infos[0].get("Model", "?")
    n = len(infos)
    suffix = "sensor" if n == 1 else "sensors"
    return f"OK Picamera2 ({model}, {n} {suffix})"


_SUBPROCESS_SCRIPT = (
    "import json,sys\n"
    "from picamera2 import Picamera2\n"
    "sys.stdout.write(json.dumps(Picamera2.global_camera_info()))\n"
)


def _describe_via_system_python() -> str | None:
    """Probe using OS Python so apt Picamera2 works when ``uv`` venv cannot import it.

    Avoids mixing an isolated venv's NumPy with apt-built extensions (ABI mismatch).
    """
    candidates: list[str] = []
    sys_py = Path("/usr/bin/python3")
    if sys_py.is_file():
        candidates.append(str(sys_py))
    base_py = Path(sys.base_prefix) / "bin" / "python3"
    bp = str(base_py)
    if base_py.is_file() and bp not in candidates:
        candidates.append(bp)

    for py in candidates:
        try:
            r = subprocess.run(
                [py, "-c", _SUBPROCESS_SCRIPT],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode != 0:
            continue
        raw = (r.stdout or "").strip()
        if not raw:
            continue
        try:
            infos = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(infos, list):
            continue
        return _format_ok(infos)
    return None


def describe_picamera2() -> str:
    """Return ``OK ...`` if libcamera lists a sensor, else ``WARN ...``."""
    last_err: str | None = None
    if Picamera2 is None:
        last_err = _PICAMERA2_IMPORT_ERR or "picamera2 import unavailable in current environment"
    else:
        try:
            return _format_ok(Picamera2.global_camera_info())
        except Exception as e:
            return "WARN libcamera: " + str(e).split("\n", 1)[0]

    sub = _describe_via_system_python()
    if sub is not None:
        return sub
    suffix = f" ({last_err})" if last_err else ""
    return (
        "WARN Picamera2 probe failed"
        + suffix
        + " — on Pi use `uv venv --system-site-packages`, pin numpy<2, `uv sync` (see README)."
    )
