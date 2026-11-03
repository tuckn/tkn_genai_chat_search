"""Filtering, matching and evidence selection shared by every source."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, timezone
from typing import Any

from .models import Message, RootInfo, Session, SourceCoverage, Title
from .normalize import cwd_contains, format_utc_offset, one_line, parse_timestamp, timestamp_sort_key, title_key
from .readers import Reader, ReadOptions, Root

KIND_OPTIONS = {"subagent": "include_subagents", "review": "include_approval_reviews"}
PREVIEW_CHARS = 160


class SearchError(ValueError):
    """An invalid combination of search options (exit code 2)."""


@dataclass(frozen=True)
class Filters:
    sources: tuple[str, ...]
    queries: tuple[str, ...] = ()
    regex: bool = False
    query_user_only: bool = False
    include_assistant: bool = True
    titles: tuple[str, ...] = ()
    session_ids: tuple[str, ...] = ()
    cwd_contains: tuple[str, ...] = ()
    cwd_field: str = "session"
    date_from: date | None = None
    date_to: date | None = None
    date_field: str = "created"
    utc_offset: timezone = UTC
    include_subagents: bool = False
    include_approval_reviews: bool = False
    sort_by: str = "updated"
    order: str = "desc"
    limit: int = 10
    messages_per_role: int = 8
    context_messages: int = 1
    max_message_chars: int = 1200
    # `show`: a requested ID is returned whatever its kind; parents match only with include_subagents.
    exact_ids: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("exact_ids")
        for key in ("sources", "queries", "titles", "session_ids", "cwd_contains"):
            data[key] = list(data[key])
        data["date_from"] = self.date_from.isoformat() if self.date_from else None
        data["date_to"] = self.date_to.isoformat() if self.date_to else None
        data["utc_offset"] = format_utc_offset(self.utc_offset)
        return data


@dataclass
class Prepared:
    filters: Filters
    patterns: list[tuple[str, re.Pattern[str]]]
    title_keys: list[str]
    ids: frozenset[str]


def prepare(filters: Filters) -> Prepared:
    if filters.date_from and filters.date_to and filters.date_from > filters.date_to:
        raise SearchError("--date-from must be on or before --date-to")
    if any(not query for query in filters.queries):
        raise SearchError("--query must not be empty")
    title_keys = list(dict.fromkeys(title_key(value) for value in filters.titles))
    if any(not key for key in title_keys):
        raise SearchError("--title must contain non-space text")
    if any(not value.strip() for value in filters.session_ids):
        raise SearchError("a session ID must not be empty")
    flags = re.IGNORECASE | re.MULTILINE
    try:
        patterns = [
            (query, re.compile(query if filters.regex else re.escape(query), flags))
            for query in dict.fromkeys(filters.queries)
        ]
    except re.error as exc:
        raise SearchError(f"invalid --query regular expression: {exc}") from None
    ids = frozenset(value.strip().casefold() for value in filters.session_ids)
    return Prepared(filters, patterns, title_keys, ids)


@dataclass
class SessionMatch:
    session: Session
    messages: list[dict[str, Any]]
    matched_queries: list[str]
    matched_titles: list[Title]
    eligible_message_count: int
    omitted_message_count: int
    hidden_matching_message_count: int
    message_count: int
    first_user_message: str = ""

    def to_dict(self, *, with_messages: bool) -> dict[str, Any]:
        data = self.session.summary()
        data["message_count"] = self.message_count
        data["matched_titles"] = [asdict(t) for t in self.matched_titles]
        data["eligible_message_count"] = self.eligible_message_count
        if with_messages:
            data.update(
                matched_queries=self.matched_queries,
                omitted_message_count=self.omitted_message_count,
                hidden_matching_message_count=self.hidden_matching_message_count,
                messages=self.messages,
            )
        else:
            data["first_user_message"] = self.first_user_message
        return data


@dataclass
class ScanResult:
    matches: list[SessionMatch]
    coverages: list[SourceCoverage]
    matched_sessions: int = 0
    found_ids: set[str] = field(default_factory=set)


def scan(plan: Iterable[tuple[str, Reader, list[Root]]], prepared: Prepared) -> ScanResult:
    """Read every planned source, keep matching sessions, then sort and apply the limit."""
    filters = prepared.filters
    options = ReadOptions(
        session_ids=prepared.ids, match_parents=filters.include_subagents, title_keys=tuple(prepared.title_keys)
    )
    matches: list[SessionMatch] = []
    coverages: list[SourceCoverage] = []
    for source, reader, roots in plan:
        coverage = SourceCoverage(source, not_searched=reader.not_searched)
        coverage.roots = [RootInfo(str(root.path.absolute()), root.path.is_dir(), root.origin) for root in roots]
        coverages.append(coverage)
        for session in reader.read(roots, options, coverage):
            coverage.sessions_read += 1
            coverage.sessions_by_kind[session.kind] += 1
            match = evaluate(session, prepared, coverage)
            if match is not None:
                matches.append(match)
    matches.sort(
        key=lambda m: (
            timestamp_sort_key(getattr(m.session, filters.sort_by)),
            m.session.source,
            m.session.session_id,
            m.session.paths,
        ),
        reverse=filters.order == "desc",
    )
    result = ScanResult(matches, coverages, matched_sessions=len(matches))
    result.found_ids = {m.session.session_id.casefold() for m in matches}
    if filters.limit:
        result.matches = matches[: filters.limit]
    return result


def evaluate(session: Session, prepared: Prepared, coverage: SourceCoverage) -> SessionMatch | None:
    filters = prepared.filters
    own_id = session.session_id.casefold()
    parent_id = session.parent_session_id.casefold()
    if prepared.ids:
        wanted = own_id in prepared.ids or (
            parent_id in prepared.ids and (filters.include_subagents or not filters.exact_ids)
        )
        if not wanted:
            return None
    requested = filters.exact_ids and own_id in prepared.ids
    option = KIND_OPTIONS.get(session.kind)
    if option and not requested and not getattr(filters, option):
        coverage.excluded_sessions[session.kind] += 1
        return None
    if session.kind == "main" and not filters.include_subagents:
        kept = [m for m in session.messages if not m.is_sidechain]
        coverage.excluded_sidechain_messages += len(session.messages) - len(kept)
        if not kept:
            return None
        session.messages = kept
    return select(session, prepared)


def date_matches(value: str, filters: Filters) -> bool:
    if not filters.date_from and not filters.date_to:
        return True
    parsed = parse_timestamp(value)
    if parsed is None:
        return False
    day = parsed.astimezone(filters.utc_offset).date()
    return (not filters.date_from or day >= filters.date_from) and (not filters.date_to or day <= filters.date_to)


def cwd_ok(value: str, filters: Filters) -> bool:
    return not filters.cwd_contains or cwd_contains(value, list(filters.cwd_contains))


def match_titles(session: Session, keys: list[str]) -> list[Title] | None:
    """Matching recorded titles when every key matches at least one of them; otherwise None."""
    entries = [(entry, title_key(entry.title)) for entry in session.titles]
    if not all(any(key in normalized for _, normalized in entries) for key in keys):
        return None
    return [entry for entry, normalized in entries if any(key in normalized for key in keys)]


def clip(text: str, limit: int, match: re.Match[str] | None = None) -> dict[str, Any]:
    start, end = 0, len(text)
    if limit and len(text) > limit:
        center = (match.start() + match.end()) // 2 if match else 0
        start = min(max(0, center - limit // 2), len(text) - limit)
        end = start + limit
    return {"text": text[start:end], "text_start": start, "text_end": end, "truncated": start > 0 or end < len(text)}


def select(session: Session, prepared: Prepared) -> SessionMatch | None:
    filters = prepared.filters
    matched_titles = match_titles(session, prepared.title_keys) if prepared.title_keys else []
    if matched_titles is None:
        return None
    if filters.cwd_field == "session" and not cwd_ok(session.cwd, filters):
        return None
    if filters.date_field != "message" and not date_matches(getattr(session, filters.date_field), filters):
        return None
    eligible: list[Message] = [
        m
        for m in session.messages
        if (filters.cwd_field != "message" or cwd_ok(m.cwd, filters))
        and (filters.date_field != "message" or date_matches(m.timestamp, filters))
    ]
    if not eligible:
        return None
    hits: dict[int, list[tuple[str, re.Match[str]]]] = {}
    found: set[str] = set()
    for index, message in enumerate(eligible):
        if filters.query_user_only and message.role != "user":
            continue
        matches = [(q, hit) for q, pattern in prepared.patterns if (hit := pattern.search(message.text)) is not None]
        if matches:
            hits[index] = matches
            found.update(q for q, _ in matches)
    if any(q not in found for q, _ in prepared.patterns):
        return None
    visible = {i for i, m in enumerate(eligible) if filters.include_assistant or m.role == "user"}
    required = set(hits) & visible
    selected = set(required) if filters.messages_per_role else set(visible)
    if filters.messages_per_role:
        # The budget is soft: every match stays, then neighbors, then the head and tail of each role.
        budget = filters.messages_per_role
        reach = filters.context_messages
        neighbors = {i for h in required for i in range(max(0, h - reach), min(len(eligible), h + reach + 1))} & visible
        priority = sorted(neighbors, key=lambda i: (min(abs(i - h) for h in required), i))
        for role in ("user", "assistant"):
            indices = sorted(i for i in visible if eligible[i].role == role)
            head, tail = (budget + 1) // 2, budget // 2
            priority.extend(indices[:head] + (indices[-tail:] if tail else []))
        counts = Counter(eligible[i].role for i in selected)
        for index in priority:
            role = eligible[index].role
            if index not in selected and counts[role] < budget:
                selected.add(index)
                counts[role] += 1
    messages = []
    for index in sorted(selected):
        original = eligible[index]
        found_here = hits.get(index, [])
        result = original.to_dict()
        result.update(clip(original.text, filters.max_message_chars, found_here[0][1] if found_here else None))
        result["matched_queries"] = [q for q, _ in found_here]
        result["matched_excerpts"] = [
            {
                "query": q,
                **clip(original.text, filters.max_message_chars, hit),
                "match_start": hit.start(),
                "match_end": hit.end(),
            }
            for q, hit in found_here
        ]
        messages.append(result)
    first_user = next((m.text for m in eligible if m.role == "user"), "")
    message_count = len(session.messages)
    # Keep only the selected evidence in memory; a full scan may match every session.
    session.messages = []
    return SessionMatch(
        session=session,
        messages=messages,
        matched_queries=[q for q, _ in prepared.patterns if q in found],
        matched_titles=matched_titles,
        eligible_message_count=len(eligible),
        omitted_message_count=len(visible - selected),
        hidden_matching_message_count=len(set(hits) - visible),
        message_count=message_count,
        first_user_message=one_line(first_user, PREVIEW_CHARS),
    )


def report(command: str, prepared: Prepared, result: ScanResult, *, with_messages: bool) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "command": command,
        "status": "success",
        "filters": prepared.filters.to_dict(),
        "coverage": {
            "matched_sessions": result.matched_sessions,
            "returned_sessions": len(result.matches),
            "sources": [coverage.to_dict() for coverage in result.coverages],
        },
        "sessions": [match.to_dict(with_messages=with_messages) for match in result.matches],
    }
