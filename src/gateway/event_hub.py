"""Fan-out published MQTT payloads to dashboard SSE subscribers (in-process)."""

from __future__ import annotations

import json
import queue
import threading
from typing import Any


class EventHub:
    """Thread-safe broadcast of JSON lines to one queue per connected browser tab."""

    def __init__(self, *, max_queue: int = 64) -> None:
        self._subs: list[queue.Queue[str]] = []
        self._lock = threading.Lock()
        self._max_queue = max_queue

    def emit(self, topic: str, payload: dict[str, Any]) -> None:
        """Serialize one broker-equivalent message for live UI consumers."""
        line = json.dumps({"topic": topic, "payload": payload}, separators=(",", ":"))
        with self._lock:
            subscribers = list(self._subs)
        for q in subscribers:
            try:
                q.put_nowait(line)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(line)
                except queue.Full:
                    self.unsubscribe(q)

    def subscribe(self) -> queue.Queue[str]:
        """Return a bounded queue drained by one :class:`~flask.Response` SSE stream."""
        q: queue.Queue[str] = queue.Queue(maxsize=self._max_queue)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[str]) -> None:
        """Remove ``q`` after the SSE client disconnects."""
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass
