"""Read single chat logs from bytes, for tools that read them from an archive instead of an app folder.

The functions use the same readers as the search, so a change of a log format is handled in one
place. They touch no file system path: `path` only names the bytes (an archive's relative path
such as `projects/<folder>/<session-id>.jsonl`) and decides the session ID and kind as a file
name would. With `detail=True` (the default here) the session also carries `tool_events` and
`message_attributes`; the search itself never asks for them.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, cast

from .models import Session, SourceCoverage, Title, TitleStore, ToolEvent
from .readers.base import READ_ERRORS, file_format
from .readers.claude_code import SET_ASIDE_MARKER, SKIPPED_PROJECT_FOLDERS, read_transcript
from .readers.codex import LOG_FOLDERS, read_rollout, titles_from_index

__all__ = [
    "LogRead",
    "Session",
    "Title",
    "ToolEvent",
    "claude_code_transcript_kind",
    "codex_rollout_status",
    "read_claude_code",
    "read_codex",
    "read_codex_titles",
]


@dataclass
class LogRead:
    """One log's session (None when it holds no conversation) and what reading it saw."""

    session: Session | None
    coverage: SourceCoverage
    error: str = ""

    @property
    def coverage_details(self) -> dict[str, Any]:
        return self.coverage.to_dict()


def _path(path: str) -> Any:
    # A PurePosixPath behaves like a Path for the readers' name checks and is never opened.
    return PurePosixPath(path.replace("\\", "/"))


def claude_code_transcript_kind(relative_path: str) -> str | None:
    """`main` or `subagent` for a transcript path under `projects/`, else None (tool results, memory ...)."""
    parts = PurePosixPath(relative_path.replace("\\", "/")).parts
    if len(parts) < 3 or parts[0] != "projects" or not parts[-1].endswith(".jsonl"):
        return None
    if SET_ASIDE_MARKER in parts[-1] or parts[2] in SKIPPED_PROJECT_FOLDERS:
        return None
    if len(parts) == 3:
        # Older Claude Code versions wrote subagents next to the transcripts as `agent-*.jsonl`.
        return "subagent" if parts[2].startswith("agent-") else "main"
    if len(parts) == 5 and parts[3] == "subagents":
        return "subagent"
    return None


def codex_rollout_status(relative_path: str) -> str | None:
    """`active` or `archived` for a rollout path under `sessions/` or `archived_sessions/`, else None."""
    path = PurePosixPath(relative_path.replace("\\", "/"))
    if not path.parts or file_format(cast(Any, path)) == "other":
        return None
    return next((status for folder, status in LOG_FOLDERS if path.parts[0] == folder), None)


def _count(coverage: SourceCoverage, path: str) -> None:
    """The bookkeeping the folder scan does for each file, for a single log read from bytes."""
    coverage.files_discovered += 1
    coverage.files_by_format[file_format(cast(Any, _path(path)))] += 1
    coverage.files_read += 1


def _counted(coverage: SourceCoverage, session: Session | None) -> LogRead:
    if session is not None:
        coverage.sessions_read += 1
        coverage.sessions_by_kind[session.kind] += 1
    return LogRead(session, coverage)


def read_claude_code(data: bytes, path: str, *, detail: bool = True) -> LogRead:
    coverage = SourceCoverage("claude-code")
    coverage.details["ignored_record_types"] = Counter()
    _count(coverage, path)
    try:
        session = read_transcript(_path(path), coverage, data=data, detail=detail)
    except READ_ERRORS as exc:
        coverage.unreadable_files += 1
        return LogRead(None, coverage, type(exc).__name__)
    if session is None:
        coverage.files_without_messages += 1
    return _counted(coverage, session)


def read_codex(
    data: bytes,
    path: str,
    *,
    titles: dict[str, list[Title]] | None = None,
    detail: bool = True,
) -> LogRead:
    coverage = SourceCoverage("codex")
    status = codex_rollout_status(path) or "active"
    _count(coverage, path)
    try:
        session = read_rollout(_path(path), coverage, status, titles or {}, data=data, detail=detail)
    except READ_ERRORS as exc:
        coverage.unreadable_files += 1
        return LogRead(None, coverage, type(exc).__name__)
    return _counted(coverage, session)


def read_codex_titles(data: bytes, path: str = "session_index.jsonl") -> tuple[dict[str, list[Title]], TitleStore]:
    """Thread names from the bytes of a Codex `session_index.jsonl`, keyed by case-folded thread ID.

    A store with skipped (malformed) records has status `partial` and says how many in `detail`.
    """
    coverage = SourceCoverage("codex")
    try:
        titles, store = titles_from_index(_path(path), coverage, data)
    except READ_ERRORS as exc:
        return {}, TitleStore(path, "unreadable", detail=type(exc).__name__)
    if coverage.parser_warnings:
        store.status = "partial"
        store.detail = f"{coverage.parser_warnings} records skipped"
    return titles, store
