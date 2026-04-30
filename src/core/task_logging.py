"""Project logging helpers: add ``TASK`` level and sane defaults for console monitoring."""

from __future__ import annotations

import logging
from typing import Final

from . import config

TASK_LEVEL: Final[int] = 25


def install_task_level() -> None:
    """Register a ``TASK`` logging level between INFO and WARNING."""
    if logging.getLevelName(TASK_LEVEL) != "TASK":
        logging.addLevelName(TASK_LEVEL, "TASK")

    if not hasattr(logging.Logger, "task"):

        def task(self: logging.Logger, message: str, *args, **kwargs) -> None:
            if self.isEnabledFor(TASK_LEVEL):
                self._log(TASK_LEVEL, message, args, **kwargs)

        logging.Logger.task = task  # type: ignore[attr-defined]


def setup_logging() -> None:
    """Configure root logger and quiet noisy libraries by default."""
    install_task_level()
    root_level_name = (config.LOG_LEVEL or "TASK").strip().upper()
    root_level = TASK_LEVEL if root_level_name == "TASK" else logging.getLevelName(root_level_name)
    if not isinstance(root_level, int):
        root_level = TASK_LEVEL

    logging.basicConfig(
        level=root_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Keep framework chatter out of normal ops view unless explicitly requested.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("paho").setLevel(logging.WARNING)
