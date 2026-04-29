"""Read newline-delimited JSON from Arduino and run detection on trigger events."""

import json
import logging
import threading

import serial

from ..camera.services import proximity
from ..core import config
from ..mqtt_service import MqttPublisher

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
    log.info("opening serial port %s at %s baud", serial_port, baudrate)

    if timeout is None:
        timeout = config.SERIAL_TIMEOUT

    try:
        ser = serial.Serial(serial_port, baudrate=baudrate, timeout=timeout)
    except serial.SerialException as e:
        log.warning(
            "Serial bridge disabled — %s. HTTP and MQTT still run without serial.",
            format_serial_open_error(serial_port, e),
        )
        return
    except Exception:
        log.exception("Unexpected error opening serial port %s", serial_port)
        return

    log_path = config.ARTIFACTS_DIR / "arduino_serial.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

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
            except Exception as e:
                log.exception("serial bridge loop error")
                mqtt_service.publish_event("serial_bridge_error", {"error": str(e)})

    finally:
        try:
            ser.close()
        except Exception:
            pass
