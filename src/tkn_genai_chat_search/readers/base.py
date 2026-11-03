"""What a reader receives and the file helpers every reader shares."""

from __future__ import annotations

import io
import json
import logging
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Protocol

import zstandard

from ..models import SourceCoverage

LOGGER = logging.getLogger("tkn_genai_chat_search")

# Errors that make one file unreadable without stopping the scan.
READ_ERRORS: tuple[type[BaseException], ...] = (OSError, UnicodeError, EOFError, zstandard.ZstdError)


@dataclass(frozen=True)
class ReadOptions:
    """Hints that let a reader skip files whose names show they cannot match.

    `session_ids`: case-folded IDs; only sessions with these IDs are wanted. With `match_parents`,
    sessions whose parent has one of these IDs are wanted too. `title_keys`: normalized `--title`
    values; a reader with a separate title store may skip sessions whose titles cannot match.
    Readers may still return sessions that do not match; the search applies the real filters.
    """

    session_ids: frozenset[str] = frozenset()
    match_parents: bool = False
    title_keys: tuple[str, ...] = ()


@dataclass
class Root:
    path: Path
    origin: str


class Reader(Protocol):
    source: str
    config_key: str
    not_searched: tuple[str, ...]

    def default_roots(self) -> list[Root]: ...

    def read(self, roots: list[Root], options: ReadOptions, coverage: SourceCoverage) -> Iterator[Any]: ...


def file_format(path: Path) -> str:
    name = path.name.casefold()
    if name.endswith(".jsonl.zst"):
        return "jsonl.zst"
    if name.endswith(".jsonl"):
        return "jsonl"
    return "other"


def open_text(path: Path) -> IO[str]:
    """Open a `.jsonl` or `.jsonl.zst` file as UTF-8 text (a BOM is accepted)."""
    raw = path.open("rb")
    if file_format(path) == "jsonl.zst":
        try:
            stream: IO[bytes] = zstandard.ZstdDecompressor().stream_reader(raw, read_across_frames=True, closefd=True)
        except BaseException:
            raw.close()
            raise
        return io.TextIOWrapper(stream, encoding="utf-8-sig")
    return io.TextIOWrapper(raw, encoding="utf-8-sig")


def walk_files(
    root: Path,
    coverage: SourceCoverage,
    prune: Callable[[Path, list[str]], list[str]] | None = None,
) -> Iterator[Path]:
    """Yield files under `root` in a stable order; count folders that cannot be listed."""

    def on_error(exc: OSError) -> None:
        coverage.unreadable_directories += 1
        LOGGER.warning("cannot list folder %s: %s", exc.filename, exc.strerror or type(exc).__name__)

    for current, directories, filenames in os.walk(root, onerror=on_error, followlinks=False):
        folder = Path(current)
        directories.sort()
        if prune is not None:
            directories[:] = prune(folder, directories)
        for name in sorted(filenames):
            yield folder / name


def iter_records(path: Path, coverage: SourceCoverage) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield `(line_number, object)` for each JSON object line; count and skip malformed lines."""
    with open_text(path) as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError:
                record_warning(coverage, path, line_number, "invalid JSON record")
                continue
            if not isinstance(value, dict):
                record_warning(coverage, path, line_number, "expected a JSON object")
                continue
            yield line_number, value


def record_warning(coverage: SourceCoverage, path: Path, line_number: int, reason: str) -> None:
    """Count a skipped record. The raw record is never logged because it may hold private text."""
    coverage.parser_warnings += 1
    coverage.warned_files.add(str(path))
    LOGGER.debug("%s:L%d: %s; record skipped", path, line_number, reason)


def unreadable(coverage: SourceCoverage, path: Path, exc: BaseException) -> None:
    coverage.unreadable_files += 1
    LOGGER.warning("cannot read %s: %s", path, type(exc).__name__)


def string(value: Any) -> str:
    return value if isinstance(value, str) else ""
