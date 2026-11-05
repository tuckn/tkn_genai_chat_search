"""The common session and message shape that every reader produces."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

SOURCES = ("codex", "claude-code")
SESSION_KINDS = ("main", "subagent", "review")


@dataclass(frozen=True)
class Title:
    kind: str
    title: str
    source_path: str
    line_number: int | None = None


@dataclass(frozen=True)
class Message:
    role: str
    text: str
    timestamp: str
    source_path: str
    line_number: int
    message_id: str = ""
    parent_id: str = ""
    turn_id: str = ""
    session_id: str = ""
    cwd: str = ""
    phase: str = ""
    is_sidechain: bool = False
    agent_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ToolEvent:
    """One tool execution, with its result when the log has one. Only read with `detail=True`.

    `origin` is `call` for a call/result record pair (Claude Code `tool_use`/`tool_result`,
    Codex `function_call`/`custom_tool_call` and their outputs) and `item` for a Codex
    `item_completed` record, which describes one finished execution by itself.
    """

    call_id: str
    name: str
    arguments: Any
    timestamp: str
    line_number: int
    origin: str = "call"
    session_id: str = ""
    turn_id: str = ""
    cwd: str = ""
    is_sidechain: bool = False
    agent_id: str = ""
    status: str = ""
    is_error: bool | None = None
    result_line_number: int | None = None
    result_timestamp: str = ""
    output_chars: int | None = None
    error_preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Session:
    source: str
    session_id: str
    kind: str
    parent_session_id: str
    titles: list[Title]
    cwd: str
    created: str
    updated: str
    paths: list[str]
    messages: list[Message]
    details: dict[str, Any] = field(default_factory=dict)
    # Filled only when a reader is called with detail=True; never part of summary() or search output.
    tool_events: list[ToolEvent] = field(default_factory=list)
    # Per message line: application fields that tell who wrote a turn (e.g. Claude Code turnOrigin).
    message_attributes: dict[int, dict[str, Any]] = field(default_factory=dict)

    @property
    def title(self) -> str:
        """The title an application shows now: the latest of the preferred kind."""
        preferred = TITLE_PREFERENCE.get(self.source, ())
        for kind in preferred:
            values = [t.title for t in self.titles if t.kind == kind]
            if values:
                return values[-1]
        return self.titles[-1].title if self.titles else ""

    def summary(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "session_id": self.session_id,
            "kind": self.kind,
            "parent_session_id": self.parent_session_id,
            "title": self.title,
            "titles": [asdict(t) for t in self.titles],
            "cwd": self.cwd,
            "created": self.created,
            "updated": self.updated,
            "paths": list(self.paths),
            "message_count": len(self.messages),
            "details": dict(self.details),
        }


# Claude Code: a manual rename wins over an AI title. Codex: thread name wins over the stored title.
TITLE_PREFERENCE = {
    "claude-code": ("custom-title", "ai-title"),
    "codex": ("thread-name", "thread-title"),
}


@dataclass
class RootInfo:
    path: str
    exists: bool
    origin: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TitleStore:
    path: str
    status: str
    titles: int = 0
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceCoverage:
    """What one source's scan saw, so that a gap is never silent."""

    source: str
    not_searched: tuple[str, ...] = ()
    roots: list[RootInfo] = field(default_factory=list)
    files_discovered: int = 0
    files_by_format: Counter[str] = field(default_factory=Counter)
    files_read: int = 0
    files_skipped_by_filter: int = 0
    unreadable_files: int = 0
    unreadable_directories: int = 0
    unsupported_files: int = 0
    files_without_sessions: int = 0
    files_without_messages: int = 0
    parser_warnings: int = 0
    sessions_read: int = 0
    sessions_by_kind: Counter[str] = field(default_factory=Counter)
    excluded_files: Counter[str] = field(default_factory=Counter)
    excluded_sessions: Counter[str] = field(default_factory=Counter)
    excluded_sidechain_messages: int = 0
    title_stores: list[TitleStore] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    warned_files: set[str] = field(default_factory=set, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "roots": [root.to_dict() for root in self.roots],
            "files_discovered": self.files_discovered,
            "files_by_format": dict(sorted(self.files_by_format.items())),
            "files_read": self.files_read,
            "files_skipped_by_filter": self.files_skipped_by_filter,
            "unreadable_files": self.unreadable_files,
            "unreadable_directories": self.unreadable_directories,
            "unsupported_files": self.unsupported_files,
            "files_without_sessions": self.files_without_sessions,
            "files_without_messages": self.files_without_messages,
            "parser_warnings": self.parser_warnings,
            "files_with_parser_warnings": len(self.warned_files),
            "sessions_read": self.sessions_read,
            "sessions_by_kind": dict(sorted(self.sessions_by_kind.items())),
            "excluded_files": dict(sorted(self.excluded_files.items())),
            "excluded_sessions": dict(sorted(self.excluded_sessions.items())),
            "excluded_sidechain_messages": self.excluded_sidechain_messages,
            "title_stores": [store.to_dict() for store in self.title_stores],
            "details": _plain(self.details),
            "not_searched": list(self.not_searched),
        }


def _plain(value: Any) -> Any:
    if isinstance(value, Counter):
        return dict(sorted(value.items()))
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value
