"""Console logging for the CLI: `[LEVEL] message` lines on stderr."""

from __future__ import annotations

import logging
import os
import sys
from typing import TextIO

SUCCESS = 25
logging.addLevelName(SUCCESS, "SUCCESS")

RESET = "\x1b[0m"
LEVEL_COLORS = {SUCCESS: "\x1b[32m", logging.ERROR: "\x1b[31m", logging.CRITICAL: "\x1b[31m"}
LOGGER_NAME = "tkn_genai_chat_search"


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def log_success(logger: logging.Logger, message: str, *args: object) -> None:
    logger.log(SUCCESS, message, *args)


def _windows_vt(stream: TextIO) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(stream.fileno())
        mode = ctypes.c_ulong()
        kernel = ctypes.windll.kernel32
        if not kernel.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)):
            return False
        return bool(kernel.SetConsoleMode(ctypes.c_void_p(handle), mode.value | 0x0004))
    except (ImportError, AttributeError, OSError, ValueError):
        return False


def supports_color(stream: TextIO) -> bool:
    if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return False
    try:
        return stream.isatty() and (os.name != "nt" or _windows_vt(stream))
    except (OSError, AttributeError, ValueError):
        return False


class ColorFormatter(logging.Formatter):
    def __init__(self, *, use_color: bool) -> None:
        super().__init__("[%(levelname)s] %(message)s")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        color = LEVEL_COLORS.get(record.levelno) if self.use_color else None
        return f"{color}{line}{RESET}" if color else line


def configure_logging(*, quiet: bool = False, verbose: bool = False, stream: TextIO | None = None) -> logging.Logger:
    target = stream if stream is not None else sys.stderr
    logger = get_logger()
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.ERROR if quiet else logging.DEBUG if verbose else logging.INFO)
    handler = logging.StreamHandler(target)
    handler.setFormatter(ColorFormatter(use_color=supports_color(target)))
    logger.addHandler(handler)
    return logger
