from __future__ import annotations

import logging
import sys

LOGGER_NAME = "brinss"

_BYTE_UNITS = ("B", "KB", "MB", "GB", "TB")


def get_logger() -> logging.Logger:
    """Return the library logger, configuring it on first use.

    Follows the same approach as pooch (whose ``Downloading file ...`` line
    shows up right next to ours): a stderr handler at INFO level, so the
    progress messages are visible without any setup on the caller's side,
    and ``propagate = False`` so an application's own root logging config
    does not print them a second time.

    To silence the messages::

        logging.getLogger("brinss").setLevel(logging.WARNING)
    """
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        if logger.level == logging.NOTSET:
            logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def format_bytes(size: int) -> str:
    """Render a byte count in the largest unit that keeps it readable."""
    value = float(size)
    for unit in _BYTE_UNITS:
        if value < 1024 or unit == _BYTE_UNITS[-1]:
            precision = 0 if unit == "B" else 1
            return f"{value:.{precision}f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")  # pragma: no cover


def format_seconds(seconds: float) -> str:
    """Render an elapsed time, switching to minutes past the 60s mark."""
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes} min {remainder} s"
