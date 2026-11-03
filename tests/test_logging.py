"""Console log format, levels and color rules."""

from __future__ import annotations

import io
import logging

import pytest

from tkn_genai_chat_search import logging_utils
from tkn_genai_chat_search.logging_utils import SUCCESS, configure_logging, log_success, supports_color


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def lines(stream: io.StringIO) -> list[str]:
    return stream.getvalue().splitlines()


def test_success_level_sits_between_info_and_warning() -> None:
    assert logging.getLevelName(SUCCESS) == "SUCCESS"
    assert logging.INFO < SUCCESS < logging.WARNING


def test_prefix_and_level_filtering() -> None:
    for quiet, verbose, expected in (
        (False, False, ["[INFO] i", "[SUCCESS] s", "[WARNING] w", "[ERROR] e"]),
        (True, False, ["[ERROR] e"]),
        (False, True, ["[DEBUG] d", "[INFO] i", "[SUCCESS] s", "[WARNING] w", "[ERROR] e"]),
    ):
        stream = io.StringIO()
        logger = configure_logging(quiet=quiet, verbose=verbose, stream=stream)
        logger.debug("d")
        logger.info("i")
        log_success(logger, "s")
        logger.warning("w")
        logger.error("e")
        assert lines(stream) == expected


def test_colors_only_on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.setattr(logging_utils, "_windows_vt", lambda stream: True)
    stream = FakeTTY()
    logger = configure_logging(stream=stream)
    log_success(logger, "done")
    logger.error("failed")
    logger.critical("stopped")
    logger.info("plain")
    logger.warning("careful")
    assert lines(stream) == [
        "\x1b[32m[SUCCESS] done\x1b[0m",
        "\x1b[31m[ERROR] failed\x1b[0m",
        "\x1b[31m[CRITICAL] stopped\x1b[0m",
        "[INFO] plain",
        "[WARNING] careful",
    ]


@pytest.mark.parametrize("setting", ["no_color", "dumb", "pipe", "no_vt"])
def test_colorless_fallbacks(monkeypatch: pytest.MonkeyPatch, setting: str) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.setattr(logging_utils, "_windows_vt", lambda stream: setting != "no_vt")
    monkeypatch.setattr(logging_utils.os, "name", "nt")
    if setting == "no_color":
        monkeypatch.setenv("NO_COLOR", "")
    if setting == "dumb":
        monkeypatch.setenv("TERM", "dumb")
    stream: io.StringIO = io.StringIO() if setting == "pipe" else FakeTTY()
    assert not supports_color(stream)
    logger = configure_logging(stream=stream)
    logger.error("failed")
    assert "\x1b[" not in stream.getvalue()
