"""Claude Code transcripts: `<config>/projects/<encoded-project>/*.jsonl` and nested subagents."""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..models import Message, Session, SourceCoverage, Title
from ..normalize import parse_timestamp, timestamp_sort_key
from .base import LOGGER, READ_ERRORS, ReadOptions, Root, file_format, iter_records, record_warning, string, unreadable

# Session title records and the field that holds each title.
TITLE_RECORDS = {"custom-title": "customTitle", "ai-title": "aiTitle"}
# Folders inside a project folder that hold copies or tool output, not transcripts.
SKIPPED_PROJECT_FOLDERS = {"memory", "tool-results", "mcp-tasks"}
SET_ASIDE_MARKER = ".orphaned-"
SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>\s*", re.DOTALL)


class ClaudeCodeReader:
    source = "claude-code"
    config_key = "claude_code"
    not_searched: tuple[str, ...] = (
        "claude.ai web and Cowork chats",
        "cloud-only sessions, deleted history and other accounts",
        "tool calls, tool results and request blobs",
        "thinking, images and attachments",
        "generated meta messages, compact summaries and <system-reminder> blocks in user turns",
        "set-aside .orphaned-* transcripts (counted in excluded_files)",
    )

    def default_roots(self) -> list[Root]:
        configured = os.environ.get("CLAUDE_CONFIG_DIR")
        if configured:
            return [Root(Path(configured).expanduser(), "default ($CLAUDE_CONFIG_DIR)")]
        return [Root(Path.home() / ".claude", "default")]

    def read(self, roots: list[Root], options: ReadOptions, coverage: SourceCoverage) -> Iterator[Session]:
        details = coverage.details
        for key in ("excluded_generated_messages", "duplicate_messages", "non_text_messages"):
            details.setdefault(key, 0)
        details.setdefault("ignored_record_types", Counter())
        seen: set[Path] = set()
        for root in roots:
            projects = root.path / "projects"
            if not projects.is_dir():
                if root.path.is_dir():
                    LOGGER.warning("no projects folder under %s; use the Claude Code config folder", root.path)
                continue
            for path in discover(projects, coverage):
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                coverage.files_discovered += 1
                coverage.files_by_format[file_format(path)] += 1
                if SET_ASIDE_MARKER in path.name:
                    coverage.excluded_files["set_aside"] += 1
                    continue
                if not may_match(path, options):
                    coverage.files_skipped_by_filter += 1
                    continue
                coverage.files_read += 1
                try:
                    session = read_transcript(path, coverage)
                except READ_ERRORS as exc:
                    unreadable(coverage, path, exc)
                    continue
                if session is None:
                    coverage.files_without_messages += 1
                    continue
                yield session


def is_subagent_path(path: Path) -> bool:
    return path.parent.name == "subagents" or path.stem.startswith("agent-")


def discover(projects: Path, coverage: SourceCoverage) -> Iterator[Path]:
    """Visit transcript locations only, never spilled tool results or memory folders."""

    def on_error(exc: OSError) -> None:
        coverage.unreadable_directories += 1
        LOGGER.warning("cannot list folder %s: %s", exc.filename, exc.strerror or type(exc).__name__)

    for current, directories, filenames in os.walk(projects, onerror=on_error, followlinks=False):
        folder = Path(current)
        depth = len(folder.relative_to(projects).parts)
        if depth == 1:
            directories[:] = sorted(d for d in directories if d not in SKIPPED_PROJECT_FOLDERS)
        elif depth == 2:
            directories[:] = [d for d in directories if d == "subagents"]
        else:
            directories[:] = sorted(directories) if depth == 0 else []
        if depth == 1 or (depth == 3 and folder.name == "subagents"):
            yield from (folder / name for name in sorted(filenames) if name.endswith(".jsonl"))


def transcript_id(path: Path) -> str:
    return path.stem.split(SET_ASIDE_MARKER, 1)[0]


def may_match(path: Path, options: ReadOptions) -> bool:
    if not options.session_ids:
        return True
    if transcript_id(path).casefold() in options.session_ids:
        return True
    if not options.match_parents:
        return False
    if path.parent.name == "subagents":
        return path.parent.parent.name.casefold() in options.session_ids
    # A legacy agent-*.jsonl names its parent only inside the file.
    return is_subagent_path(path)


def content_text(content: Any) -> str:
    """Top-level text blocks only; tool, thinking and image blocks are not conversation text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
        )
    return ""


def read_transcript(path: Path, coverage: SourceCoverage) -> Session | None:
    details = coverage.details
    ignored: Counter[str] = details["ignored_record_types"]
    messages: list[Message] = []
    seen: set[tuple[str, str, str]] = set()
    recorded_ids: set[str] = set()
    titles: list[Title] = []
    session_id = transcript_id(path)
    subagent_file = is_subagent_path(path)
    agent_id = path.stem.removeprefix("agent-") if subagent_file else ""
    parent_id = path.parent.parent.name if path.parent.name == "subagents" else ""
    source_path = str(path.absolute())
    for line_number, row in iter_records(path, coverage):
        kind = string(row.get("type"))
        if kind in TITLE_RECORDS and row.get("sessionId") == session_id:
            value = string(row.get(TITLE_RECORDS[kind]))
            if value:
                # Keep each value once, at its latest line, so the current title is the last one.
                titles = [t for t in titles if not (t.kind == kind and t.title == value)]
                titles.append(Title(kind, value, source_path, line_number))
        if kind not in {"user", "assistant"}:
            ignored[kind or "(missing type)"] += 1
            continue
        recorded = string(row.get("sessionId"))
        if recorded:
            recorded_ids.add(recorded)
        if not parent_id and subagent_file:
            parent_id = recorded
        if row.get("isMeta") or row.get("isCompactSummary"):
            details["excluded_generated_messages"] += 1
            continue
        message = row.get("message")
        if not isinstance(message, dict) or message.get("role", kind) != kind:
            record_warning(coverage, path, line_number, "unsupported message envelope")
            continue
        text = content_text(message.get("content"))
        if not text.strip():
            details["non_text_messages"] += 1
            continue
        if kind == "user" and SYSTEM_REMINDER.search(text):
            # Claude Code adds context to the user turn; it is not what the user wrote.
            text = SYSTEM_REMINDER.sub("", text).strip()
            if not text:
                details["excluded_generated_messages"] += 1
                continue
        uuid = string(row.get("uuid"))
        key = (kind, uuid, text)
        if uuid and key in seen:
            details["duplicate_messages"] += 1
            continue
        seen.add(key)
        messages.append(
            Message(
                role=kind,
                text=text,
                timestamp=string(row.get("timestamp")),
                source_path=source_path,
                line_number=line_number,
                message_id=uuid,
                parent_id=string(row.get("parentUuid")),
                session_id=recorded,
                cwd=string(row.get("cwd")),
                is_sidechain=bool(row.get("isSidechain")),
                agent_id=string(row.get("agentId")) or agent_id,
            )
        )
    if not messages:
        return None
    primary = [m for m in messages if not m.is_sidechain] or messages
    dated = [m.timestamp for m in primary if parse_timestamp(m.timestamp) is not None]
    kind = "subagent" if subagent_file or all(m.is_sidechain for m in messages) else "main"
    return Session(
        source="claude-code",
        session_id=session_id,
        kind=kind,
        parent_session_id=parent_id,
        titles=titles,
        cwd=next((m.cwd for m in primary if m.cwd), ""),
        created=min(dated, key=timestamp_sort_key) if dated else "",
        updated=max(dated, key=timestamp_sort_key) if dated else "",
        paths=[source_path],
        messages=messages,
        details={"agent_id": agent_id, "recorded_session_ids": sorted(recorded_ids)},
    )
