"""Single lock: Picamera2 cannot be used concurrently (still vs IMX500 session)."""

from __future__ import annotations

import threading

PICAMERA2_SESSION_LOCK = threading.Lock()
