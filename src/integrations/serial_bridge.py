"""Read newline-delimited JSON from Arduino and run detection on trigger events."""

import json
import logging
import threading

import serial

from ..camera.services import proximity
from ..core import config
from ..core.status_publish import publish_component_safe
from ..core.task_logging import TASK_LEVEL
from ..mqtt import MqttPublisher

log = logging.getLogger("serial_bridge")


def format_serial_open_error(port: str, exc: BaseException) -> str:
    """Short, single-line reason for a failed :class:`serial.Serial` open (logs + startup banner)."""
    errno = getattr(exc, "errno", None)
    if errno is None and isinstance(exc, OSError):
        errno = exc.errno
    if errno == 2:
        return f"{port} not found — connect the device or set SERIAL_PORT= to skip"
    if errno == 13:
        return f"{port} permission denied"
    if errno == 16:
        return f"{port} busy or in use"
    msg = str(exc).replace("\n", " ")
    if msg.count("[Errno") > 1 and "No such file" in msg:
        return f"{port} not found — connect the device or set SERIAL_PORT= to skip"
    return f"{port}: {msg}"


def should_trigger(msg: dict) -> bool:
    """Return True only for Arduino obstacle-detected trigger events."""
    event_type = msg.get("event_type")
    return event_type == "obstacle_detected"


def run_serial_bridge(
    mqtt_service: MqttPublisher,
    stop_event: threading.Event,
    serial_port: str,
    baudrate: int,
    timeout: float = config.SERIAL_TIMEOUT,
) -> None:
    """Read newline-delimited JSON from the Arduino and trigger detection when needed."""
    if timeout is None:
        timeout = config.SERIAL_TIMEOUT

    log_path = config.ARTIFACTS_DIR / "arduino_serial.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    max_backoff_s = 30.0
    open_backoff_s = 2.0

    while not stop_event.is_set():
        ser: serial.Serial | None = None
        attempt = 0
        ob = open_backoff_s
        while ser is None and not stop_event.is_set():
            attempt += 1
            try:
                log.log(TASK_LEVEL, "sensor-link: opening %s @ %s baud", serial_port, baudrate)
                ser = serial.Serial(serial_port, baudrate=baudrate, timeout=timeout)
            except serial.SerialException as e:
                if attempt == 1:
                    log.warning(
                        "Serial port %s not ready — %s. Retrying in background until open or shutdown.",
                        serial_port,
                        format_serial_open_error(serial_port, e),
                    )
                elif attempt % 10 == 0:
                    log.info(
                        "serial still waiting on %s (attempt %s) — %s",
                        serial_port,
                        attempt,
                        format_serial_open_error(serial_port, e),
                    )
                if stop_event.wait(ob):
                    return
                ob = min(max_backoff_s, ob * 1.2)
            except Exception:
                log.exception("Unexpected error opening serial port %s", serial_port)
                return

        if ser is None:
            return

        publish_component_safe(mqtt_service, "sensor", "up")
        log.log(TASK_LEVEL, "sensor-link: up (%s)", serial_port)
        reconnect_wait_s = 2.0

        lost_link = False
        try:
            while not stop_event.is_set():
                try:
                    line = ser.readline()
                    if not line:
                        continue

                    text = line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue

                    with log_path.open("a", encoding="utf-8") as f:
                        f.write(text + "\n")

                    msg = json.loads(text)

                    # Optional: publish raw Arduino event to MQTT for observability
                    mqtt_service.publish_event("arduino_serial_received", {"raw": msg})

                    if should_trigger(msg):
                        result = proximity.handle_trigger(mqtt_service)
                        mqtt_service.publish_event(
                            "detection_result",
                            {
                                "trigger_source": "arduino_serial",
                                "arduino_event": msg,
                                "result": result,
                            },
                        )

                except json.JSONDecodeError:
                    log.warning("bad JSON from serial")
                    mqtt_service.publish_event("serial_json_error")
                except serial.SerialException as e:
                    log.warning(
                        "serial link lost on %s (%s); sensor marked down, reconnecting after backoff",
                        serial_port,
                        str(e).replace("\n", " "),
                    )
                    log.log(TASK_LEVEL, "sensor-link: down (%s)", serial_port)
                    lost_link = True
                    break
                except Exception as e:
                    log.exception("serial bridge loop error")
                    mqtt_service.publish_event("serial_bridge_error", {"error": str(e)})
        finally:
            try:
                ser.close()
            except Exception:
                pass

        if stop_event.is_set():
            publish_component_safe(mqtt_service, "sensor", "down")
            return

        if lost_link:
            publish_component_safe(mqtt_service, "sensor", "down")
            rw = reconnect_wait_s
            reconnect_wait_s = min(max_backoff_s, reconnect_wait_s * 1.2)
            if stop_event.wait(rw):
                return
            continue
