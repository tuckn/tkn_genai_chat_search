"""Comparison helpers shared by every reader and by the search filters."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime, timedelta, timezone

UTC_OFFSET_PATTERN = re.compile(r"[+-](?:[01][0-9]|2[0-3]):[0-5][0-9]")
_EPOCH = datetime.min.replace(tzinfo=UTC)


def parse_timestamp(value: str) -> datetime | None:
    """ISO 8601 with `Z` or an offset; a naive value is read as UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def timestamp_sort_key(value: str) -> datetime:
    return parse_timestamp(value) or _EPOCH


def parse_utc_offset(value: str) -> timezone:
    if not UTC_OFFSET_PATTERN.fullmatch(value):
        raise ValueError("use a UTC offset such as +09:00 or -05:00")
    minutes = int(value[1:3]) * 60 + int(value[4:])
    return timezone(timedelta(minutes=minutes if value[0] == "+" else -minutes))


def format_utc_offset(offset: timezone) -> str:
    delta = offset.utcoffset(None)
    minutes = int(delta.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def normalize_path_text(value: str) -> str:
    """Case-folded backslash form; WSL `/mnt/c/...` and `\\\\?\\` prefixes map to `c:\\...`."""
    normalized = value.replace("/", "\\")
    if normalized.startswith("\\\\?\\"):
        normalized = normalized[4:]
    normalized = normalized.rstrip("\\")
    wsl_mount = re.match(r"^\\mnt\\([A-Za-z])(?:\\(.*))?$", normalized)
    if wsl_mount:
        normalized = f"{wsl_mount.group(1)}:\\{wsl_mount.group(2) or ''}".rstrip("\\")
    return normalized.casefold()


def cwd_contains(value: str, terms: list[str]) -> bool:
    normalized = normalize_path_text(value)
    return all(normalize_path_text(term) in normalized for term in terms)


def title_key(value: str) -> str:
    """Compare titles without case, full/half-width, or whitespace differences."""
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def one_line(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: max(0, limit - 1)] + "…"
