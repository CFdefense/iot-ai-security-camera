"""Read newline-delimited JSON from Arduino and run detection on trigger events."""

import json
import logging
import threading

import serial

from . import config, detection
from .mqtt_service import MqttPublisher

log = logging.getLogger("serial_bridge")


def should_trigger(msg: dict) -> bool:
    """Return True only for confirmed Arduino trigger events.

    Adjust this logic to match your actual JSON schema.
    """
    event_type = msg.get("event_type")
    return event_type in {"proximity_detected", "confirmed_trigger", "sensor_triggered"}


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
    except Exception:
        log.exception("failed to open serial port")
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
                    result = detection.handle_trigger(mqtt_service)
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
