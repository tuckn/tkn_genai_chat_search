"""Codex rollouts: `<codex-home>/sessions` and `archived_sessions`, `.jsonl` and `.jsonl.zst`.

Thread titles live outside the rollouts, in `state_<n>.sqlite` (table `threads`) and
`session_index.jsonl`. Both are opened read-only and are optional.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..models import Message, Session, SourceCoverage, Title, TitleStore
from ..normalize import parse_timestamp, timestamp_sort_key, title_key
from .base import (
    LOGGER,
    READ_ERRORS,
    ReadOptions,
    Root,
    file_format,
    iter_records,
    string,
    unreadable,
    walk_files,
)

LOG_FOLDERS = (("sessions", "active"), ("archived_sessions", "archived"))
# rollout-<time>-<thread-id>[_<segment-id>].jsonl[.zst]; a segment continues the same thread in a new file.
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
ROLLOUT_NAME = re.compile(rf"({_UUID})(?:_({_UUID}))?\.jsonl(?:\.zst)?$", re.IGNORECASE)
STATE_DB = re.compile(r"^state_(\d+)\.sqlite$")
APPROVAL_REVIEW_PREFIX = "The following is the Codex agent history"
REVIEW_THREAD_SOURCES = {"approval_review", "guardian_review"}


class CodexReader:
    source = "codex"
    config_key = "codex"
    not_searched: tuple[str, ...] = (
        "remote and cloud history, other hosts and accounts",
        "tool calls and tool outputs",
        "reasoning, images and other non-text content",
        "developer and system instructions",
    )

    def default_roots(self) -> list[Root]:
        configured = os.environ.get("CODEX_HOME")
        if configured:
            return [Root(Path(configured).expanduser(), "default ($CODEX_HOME)")]
        return [Root(Path.home() / ".codex", "default")]

    def read(self, roots: list[Root], options: ReadOptions, coverage: SourceCoverage) -> Iterator[Session]:
        seen: set[Path] = set()
        for root in roots:
            titles = load_titles(root.path, coverage)
            wanted = titled_ids(titles, options.title_keys) if options.title_keys else None
            folders = [(root.path / name, status) for name, status in LOG_FOLDERS]
            if root.path.is_dir() and not any(folder.is_dir() for folder, _ in folders):
                LOGGER.warning("no sessions folder under %s; use the Codex home folder", root.path)
            for folder, status in folders:
                if not folder.is_dir():
                    continue
                for path in walk_files(folder, coverage):
                    kind = file_format(path)
                    if kind == "other":
                        if ".jsonl." in path.name.casefold():
                            coverage.unsupported_files += 1
                            LOGGER.debug("unsupported log format: %s", path)
                        continue
                    resolved = path.resolve()
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    coverage.files_discovered += 1
                    coverage.files_by_format[kind] += 1
                    if not may_match(path, options, wanted):
                        coverage.files_skipped_by_filter += 1
                        continue
                    coverage.files_read += 1
                    try:
                        session = read_rollout(path, coverage, status, titles)
                    except READ_ERRORS as exc:
                        unreadable(coverage, path, exc)
                        continue
                    if session is not None:
                        yield session


def name_ids(path: Path) -> tuple[str, str]:
    match = ROLLOUT_NAME.search(path.name)
    return (match.group(1).casefold(), (match.group(2) or "").casefold()) if match else ("", "")


def may_match(path: Path, options: ReadOptions, wanted_by_title: set[str] | None) -> bool:
    """Skip a file only when its name proves the thread cannot match; unnamed files are read."""
    thread_id, _segment = name_ids(path)
    if not thread_id:
        return True
    if options.session_ids and not options.match_parents and thread_id not in options.session_ids:
        return False
    return wanted_by_title is None or thread_id in wanted_by_title


def titled_ids(titles: dict[str, list[Title]], keys: tuple[str, ...]) -> set[str]:
    return {
        session_id
        for session_id, entries in titles.items()
        if all(any(key in title_key(t.title) for t in entries) for key in keys)
    }


def load_titles(home: Path, coverage: SourceCoverage) -> dict[str, list[Title]]:
    """Thread names from `session_index.jsonl`, then the newest `state_<n>.sqlite` (which wins)."""
    titles: dict[str, list[Title]] = {}

    def add(session_id: str, kind: str, value: Any, source_path: str, line_number: int | None) -> bool:
        if not isinstance(value, str) or not value.strip() or not session_id:
            return False
        entries = titles.setdefault(session_id.casefold(), [])
        entries[:] = [t for t in entries if not (t.kind == kind and t.title == value)]
        entries.append(Title(kind, value, source_path, line_number))
        return True

    index = home / "session_index.jsonl"
    if index.is_file():
        store = TitleStore(str(index.absolute()), "read")
        try:
            for line_number, row in iter_records(index, coverage):
                value = row.get("thread_name")
                store.titles += add(string(row.get("id")), "thread-name", value, store.path, line_number)
        except READ_ERRORS as exc:
            store.status, store.detail = "unreadable", type(exc).__name__
            LOGGER.warning("cannot read Codex title index %s: %s", index, type(exc).__name__)
        coverage.title_stores.append(store)
    elif home.is_dir():
        coverage.title_stores.append(TitleStore(str(index.absolute()), "missing"))

    database = newest_state_db(home)
    if database is None:
        if home.is_dir():
            coverage.title_stores.append(TitleStore(str((home / "state_<n>.sqlite").absolute()), "missing"))
        return titles
    store = TitleStore(str(database.absolute()), "read")
    try:
        rows, columns = read_thread_titles(database)
    except sqlite3.Error as exc:
        store.status, store.detail = "unreadable", type(exc).__name__
        LOGGER.warning("cannot read Codex titles from %s: %s", database, exc)
        coverage.title_stores.append(store)
        return titles
    if rows is None:
        store.status, store.detail = "unsupported", "no threads table with id and name or title columns"
        LOGGER.warning("Codex title database %s has an unknown layout; titles come from the index only", database)
        coverage.title_stores.append(store)
        return titles
    store.detail = "columns: " + ", ".join(columns)
    for row in rows:
        session_id = row.get("id") or ""
        # The stored title often repeats the first message; the name is what the app shows.
        store.titles += add(session_id, "thread-title", row.get("title"), store.path, None)
        store.titles += add(session_id, "thread-name", row.get("name"), store.path, None)
    for entries in titles.values():
        entries.sort(key=lambda t: (t.kind != "thread-name", t.line_number is None))
    coverage.title_stores.append(store)
    return titles


def newest_state_db(home: Path) -> Path | None:
    try:
        candidates = [
            (int(match.group(1)), path)
            for path in home.iterdir()
            if (match := STATE_DB.match(path.name)) and path.is_file()
        ]
    except OSError:
        return None
    return max(candidates)[1] if candidates else None


def read_thread_titles(database: Path) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Read only `id`, `name` and `title`, checking first that the columns exist."""
    connection = sqlite3.connect(f"{database.absolute().as_uri()}?mode=ro", uri=True, timeout=2)
    try:
        available = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
        columns = [name for name in ("name", "title") if name in available]
        if "id" not in available or not columns:
            return None, []
        query = f"SELECT id, {', '.join(columns)} FROM threads"
        rows = [dict(zip(["id", *columns], values, strict=True)) for values in connection.execute(query)]
        return rows, ["id", *columns]
    finally:
        connection.close()


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            text = item.get("text") or item.get("input_text") or ""
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts)


def clean_user_text(text: str) -> str:
    """Remove the instruction and environment wrappers Codex adds around a user request."""
    for marker in ("## My request for Codex:", "## My request for Codex"):
        if marker in text:
            return text.split(marker, 1)[1].strip()
    if text.lstrip().startswith("# AGENTS.md instructions for"):
        return ""
    if "<INSTRUCTIONS>" in text and "</INSTRUCTIONS>" in text:
        text = text.split("</INSTRUCTIONS>", 1)[-1]
    if "<environment_context>" in text:
        text = text.split("<environment_context>", 1)[0]
    return text.strip()


def normalize_message_text(value: str) -> str:
    return " ".join(value.split())


def read_rollout(
    path: Path, coverage: SourceCoverage, archive_status: str, titles: dict[str, list[Title]]
) -> Session | None:
    meta: dict[str, Any] | None = None
    messages: list[Message] = []
    seen: set[tuple[str, str, str]] = set()
    turn_id = ""
    turn_cwd = ""
    source_path = str(path.absolute())

    def append(line_number: int, role: str, text: str, timestamp: str, phase: str, message_id: str, turn: str) -> None:
        if role == "user":
            text = clean_user_text(text)
        text = text.strip()
        if not text:
            return
        # Codex writes the same message as a response item and as an event; keep the first.
        key = (role, turn, normalize_message_text(text))
        if key in seen:
            return
        seen.add(key)
        messages.append(
            Message(
                role=role,
                text=text,
                timestamp=timestamp,
                source_path=source_path,
                line_number=line_number,
                message_id=message_id,
                turn_id=turn,
                cwd=turn_cwd or string((meta or {}).get("cwd")),
                phase=phase,
            )
        )

    for line_number, obj in iter_records(path, coverage):
        event_type = obj.get("type")
        raw_payload = obj.get("payload")
        payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
        timestamp = string(obj.get("timestamp"))
        if meta is None and not event_type and obj.get("id") and obj.get("timestamp") and "instructions" in obj:
            # Legacy rollouts put the session metadata directly in the first object.
            meta = obj
            turn_cwd = string(obj.get("cwd"))
            continue
        if event_type == "session_meta":
            # A forked rollout repeats its parent's metadata later; the first record is this thread.
            if meta is None:
                meta = payload
                turn_cwd = string(payload.get("cwd"))
            continue
        if event_type == "turn_context":
            turn_id = string(payload.get("turn_id"))
            turn_cwd = string(payload.get("cwd")) or string((meta or {}).get("cwd"))
            continue
        if event_type == "response_item":
            role = payload.get("role")
            if role in {"user", "assistant"} and payload.get("type", "message") == "message":
                passthrough = payload.get("internal_chat_message_metadata_passthrough")
                turn = string(passthrough.get("turn_id")) if isinstance(passthrough, dict) else ""
                append(
                    line_number,
                    str(role),
                    content_to_text(payload.get("content")),
                    timestamp,
                    string(payload.get("phase")) or string(payload.get("channel")),
                    string(payload.get("id")),
                    turn or turn_id,
                )
            continue
        if event_type == "message":
            role = obj.get("role")
            if role in {"user", "assistant"}:
                append(
                    line_number,
                    str(role),
                    content_to_text(obj.get("content")),
                    timestamp or string((meta or {}).get("timestamp")),
                    "",
                    string(obj.get("id")),
                    turn_id,
                )
            continue
        if event_type == "event_msg":
            payload_type = payload.get("type")
            if payload_type == "user_message":
                append(line_number, "user", string(payload.get("message")), timestamp, "", "", turn_id)
            elif payload_type == "agent_message":
                text = string(payload.get("message"))
                append(line_number, "assistant", text, timestamp, string(payload.get("phase")), "", turn_id)

    if meta is None:
        coverage.files_without_sessions += 1
        return None
    if not messages:
        coverage.files_without_messages += 1
        return None
    session_id = string(meta.get("id")) or string(meta.get("session_id"))
    kind, parent_id, app_source = classify(meta, messages)
    created = string(meta.get("timestamp"))
    dated = [value for value in [created, *(m.timestamp for m in messages)] if parse_timestamp(value) is not None]
    _thread, segment = name_ids(path)
    raw_git = meta.get("git")
    git: dict[str, Any] = raw_git if isinstance(raw_git, dict) else {}
    return Session(
        source="codex",
        session_id=session_id,
        kind=kind,
        parent_session_id=parent_id,
        titles=list(titles.get(session_id.casefold(), [])),
        cwd=string(meta.get("cwd")),
        created=created,
        updated=max(dated, key=timestamp_sort_key) if dated else "",
        paths=[source_path],
        messages=messages,
        details={
            "archive_status": archive_status,
            "rollout_segment": segment,
            "originator": string(meta.get("originator")),
            "app_source": app_source,
            "thread_source": string(meta.get("thread_source")),
            "repository_url": string(git.get("repository_url")),
        },
    )


def classify(meta: dict[str, Any], messages: list[Message]) -> tuple[str, str, str]:
    """Return (kind, parent thread ID, app source as text) from the session metadata."""
    raw_source = meta.get("source")
    source: Any = raw_source
    if isinstance(raw_source, str) and raw_source.startswith("{"):
        try:
            source = json.loads(raw_source)
        except ValueError:
            source = raw_source
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    parent = string(meta.get("parent_thread_id")) or (
        string(spawn.get("parent_thread_id")) if isinstance(spawn, dict) else ""
    )
    thread_source = string(meta.get("thread_source")).casefold()
    if (
        thread_source in REVIEW_THREAD_SOURCES
        or (isinstance(subagent, dict) and subagent.get("other") == "guardian")
        or any(m.role == "user" and m.text.lstrip().startswith(APPROVAL_REVIEW_PREFIX) for m in messages)
    ):
        kind = "review"
    elif thread_source == "subagent" or subagent is not None:
        kind = "subagent"
    else:
        kind = "main"
    app_source = source if isinstance(source, str) else json.dumps(source, ensure_ascii=False, sort_keys=True)
    return kind, parent, app_source if source is not None else ""
