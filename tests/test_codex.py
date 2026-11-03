"""Codex reader: rollouts (.jsonl and .jsonl.zst), title stores, session kinds and the shared rules."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import CodexHome, Runner, thread_id, write_jsonl

FIXTURES = Path(__file__).parent / "fixtures"
A, B, C = thread_id(1), thread_id(2), thread_id(3)


def search(run: Runner, codex: CodexHome, *options: str, command: str = "search") -> dict:
    result = run(command, "--source", "codex", "--root", f"codex={codex.root}", "--json", *options)
    assert result.code == 0, result.err
    return result.json


def coverage(report: dict) -> dict:
    return report["coverage"]["sources"][0]


def ids(report: dict) -> list[str]:
    return [s["session_id"] for s in report["sessions"]]


def test_compressed_and_plain_rollouts_are_both_read(run: Runner, codex: CodexHome) -> None:
    codex.rollout(A, [{"text": "plain needle"}])
    codex.rollout(B, [{"text": "compressed needle"}], compress=True)
    codex.rollout(C, [{"text": "archived needle"}], compress=True, archived=True)
    report = search(run, codex, "--query", "needle")
    assert sorted(ids(report)) == sorted([A, B, C])
    assert coverage(report)["files_by_format"] == {"jsonl": 1, "jsonl.zst": 2}
    assert coverage(report)["files_read"] == 3
    archived = {s["session_id"]: s["details"]["archive_status"] for s in report["sessions"]}
    assert archived == {A: "active", B: "active", C: "archived"}


def test_broken_compressed_file_is_unreadable_not_fatal(run: Runner, codex: CodexHome) -> None:
    codex.rollout(A)
    (codex.active / f"rollout-x-{B}.jsonl.zst").write_bytes(b"\x28\xb5\x2f\xfd not really zstd")
    result = run("search", "--source", "codex", "--root", f"codex={codex.root}", "--json")
    assert result.code == 0
    assert coverage(result.json)["unreadable_files"] == 1
    assert ids(result.json) == [A]


def test_other_compression_formats_are_counted_as_unsupported(run: Runner, codex: CodexHome) -> None:
    codex.rollout(A)
    (codex.active / f"rollout-x-{B}.jsonl.gz").write_bytes(b"\x1f\x8b")
    assert coverage(search(run, codex))["unsupported_files"] == 1


def test_middle_hit_and_context_survive_budget(run: Runner, codex: CodexHome) -> None:
    codex.rollout(
        A,
        [{"text": "needle" if i == 5 else f"message {i}", "role": "assistant" if i % 2 else "user"} for i in range(12)],
    )
    session = search(run, codex, "--query", "needle", "--messages-per-role", "1")["sessions"][0]
    assert "needle" in [m["text"] for m in session["messages"]]
    lines = [m["line_number"] for m in session["messages"]]
    assert lines == sorted(lines)
    assert session["omitted_message_count"] > 0


def test_every_hit_survives_soft_budget(run: Runner, codex: CodexHome) -> None:
    codex.rollout(A, [{"text": f"needle {i}"} for i in range(12)])
    assert len(search(run, codex, "--query", "needle", "--messages-per-role", "1")["sessions"][0]["messages"]) == 12


def test_long_query_fits_in_window(run: Runner, codex: CodexHome) -> None:
    query = "n" * 60
    codex.rollout(A, [{"text": "x" * 1000 + query + "y" * 1000}])
    message = search(run, codex, "--query", query, "--max-message-chars", "80")["sessions"][0]["messages"][0]
    assert query in message["text"]
    assert query in message["matched_excerpts"][0]["text"]


def test_phase_turn_message_id_and_multiline_text(run: Runner, codex: CodexHome) -> None:
    codex.rollout(A, [{"role": "assistant", "phase": "final_answer", "text": "done\nverified", "id": "msg_x"}])
    message = search(run, codex)["sessions"][0]["messages"][0]
    assert (message["phase"], message["text"], message["message_id"], message["turn_id"]) == (
        "final_answer",
        "done\nverified",
        "msg_x",
        "0",
    )


def test_hidden_matches_are_reported_without_leaking_text(run: Runner, codex: CodexHome) -> None:
    codex.rollout(A, [{"text": "user text"}, {"role": "assistant", "text": "needle private detail"}])
    report = search(run, codex, "--no-include-assistant", "--query", "needle")
    assert report["sessions"][0]["hidden_matching_message_count"] == 1
    assert "private detail" not in json.dumps(report)
    assert search(run, codex, "--query-user-only", "--query", "needle")["sessions"] == []


def test_query_searches_text_not_metadata(run: Runner, codex: CodexHome) -> None:
    codex.rollout(A, cwd="C:/needle")
    assert search(run, codex, "--query", "needle")["sessions"] == []


def test_literal_regex_and_and_matching(run: Runner, codex: CodexHome) -> None:
    codex.rollout(A, [{"text": "A.B first"}, {"text": "SECOND"}])
    assert len(search(run, codex, "--query", "a.b", "--query", "second")["sessions"]) == 1
    assert search(run, codex, "--query", "A.B", "--query", "absent")["sessions"] == []
    assert len(search(run, codex, "--regex", "--query", r"A.B.*first")["sessions"]) == 1


def test_user_wrappers_are_removed(run: Runner, codex: CodexHome) -> None:
    codex.rollout(
        A,
        [
            {"text": "# AGENTS.md instructions for C:/x\nrules"},
            {"text": "<environment_context>cwd</environment_context>"},
            {"text": "context\n## My request for Codex:\nreal request"},
        ],
    )
    assert [m["text"] for m in search(run, codex)["sessions"][0]["messages"]] == ["real request"]


def test_duplicate_events_and_repeated_turns_preserve_provenance(run: Runner, codex: CodexHome) -> None:
    path = codex.rollout(A, [{"text": "same", "turn": "one"}, {"text": "same", "turn": "two"}])
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "same"}}) + "\n")
    session = search(run, codex)["sessions"][0]
    assert [m["line_number"] for m in session["messages"]] == [3, 5]


def test_forked_rollout_keeps_its_own_identity(run: Runner, codex: CodexHome) -> None:
    parent_meta = {"type": "session_meta", "payload": {"id": A, "timestamp": "2026-08-01T00:00:00Z", "cwd": "C:/p"}}
    codex.rollout(
        B,
        [{"text": "child work"}],
        thread_source="subagent",
        source={"subagent": {"thread_spawn": {"parent_thread_id": A, "depth": 1}}},
        extra=[parent_meta],
    )
    report = search(run, codex, "--include-subagents")
    session = report["sessions"][0]
    assert (session["session_id"], session["kind"], session["parent_session_id"]) == (B, "subagent", A)
    assert search(run, codex)["sessions"] == []
    assert coverage(search(run, codex))["excluded_sessions"] == {"subagent": 1}
    assert ids(search(run, codex, "--include-subagents", "--session-id", A)) == [B]


def test_reviews_are_excluded_by_metadata_source_or_prefix(run: Runner, codex: CodexHome) -> None:
    codex.rollout(A, thread_source="guardian_review")
    codex.rollout(B, thread_source="subagent", source={"subagent": {"other": "guardian"}}, parent=C)
    codex.rollout(C, [{"text": "The following is the Codex agent history wrapped"}])
    assert search(run, codex)["sessions"] == []
    assert coverage(search(run, codex))["excluded_sessions"] == {"review": 3}
    reviews = search(run, codex, "--include-approval-reviews")
    assert sorted(ids(reviews)) == sorted([A, B, C])
    assert {s["kind"] for s in reviews["sessions"]} == {"review"}
    shown = run("show", A, "--root", f"codex={codex.root}", "--json")
    assert shown.code == 0
    assert ids(shown.json) == [A]


def test_rollout_segments_are_separate_files_of_one_thread(run: Runner, codex: CodexHome) -> None:
    codex.rollout(A, [{"text": "first try"}], compress=True)
    codex.rollout(A, [{"text": "second try"}], compress=True, segment=B)
    report = search(run, codex, "--session-id", A)
    assert ids(report) == [A, A]
    assert sorted(s["details"]["rollout_segment"] for s in report["sessions"]) == ["", B]


def test_titles_from_index_and_state_database(run: Runner, codex: CodexHome) -> None:
    codex.rollout(A, [{"text": "first message"}])
    codex.rollout(B, [{"text": "other"}])
    codex.rollout(C, [{"text": "untitled"}])
    codex.index(
        [
            {"id": A, "thread_name": "旧い名前", "updated_at": "2026-11-01T00:00:00Z"},
            {"id": B, "thread_name": "Index only", "updated_at": "2026-11-01T00:00:00Z"},
        ]
    )
    codex.state_db([(A, "Plugin設定の共有", "first message"), (B, None, "other")])
    report = search(run, codex, command="list")
    by_id = {s["session_id"]: s for s in report["sessions"]}
    assert by_id[A]["title"] == "Plugin設定の共有"
    assert [(t["kind"], t["title"]) for t in by_id[A]["titles"]] == [
        ("thread-name", "旧い名前"),
        ("thread-name", "Plugin設定の共有"),
        ("thread-title", "first message"),
    ]
    assert by_id[B]["title"] == "Index only"
    assert by_id[C]["title"] == ""
    stores = {Path(s["path"]).name: s for s in coverage(report)["title_stores"]}
    assert stores["session_index.jsonl"]["status"] == "read"
    assert stores["state_5.sqlite"]["status"] == "read"
    assert ids(search(run, codex, "--title", "ｐｌｕｇｉｎ 設定", command="list")) == [A]
    assert ids(search(run, codex, "--title", "旧い", command="list")) == [A]


def test_title_filter_skips_files_by_name_before_reading(run: Runner, codex: CodexHome) -> None:
    codex.rollout(A, [{"text": "one"}])
    codex.rollout(B, [{"text": "two"}])
    write_jsonl(
        codex.active / "legacy-without-id.jsonl",
        [
            {"type": "session_meta", "payload": {"id": C}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "three"}},
        ],
    )
    codex.state_db([(A, "Release plan", None), (C, "Release notes", None)])
    report = search(run, codex, "--title", "release", command="list")
    assert sorted(ids(report)) == sorted([A, C])
    assert coverage(report)["files_skipped_by_filter"] == 1


def test_unknown_or_missing_title_stores_fall_back(run: Runner, codex: CodexHome) -> None:
    codex.rollout(A)
    codex.index([{"id": A, "thread_name": "From index"}])
    codex.state_db([], columns="CREATE TABLE threads (thread_key TEXT, label TEXT)")
    result = run("list", "--source", "codex", "--root", f"codex={codex.root}", "--json")
    assert result.code == 0
    assert result.json["sessions"][0]["title"] == "From index"
    statuses = {Path(s["path"]).name: s["status"] for s in coverage(result.json)["title_stores"]}
    assert statuses == {"session_index.jsonl": "read", "state_5.sqlite": "unsupported"}
    assert "unknown layout" in result.err


def test_newest_state_database_is_used(run: Runner, codex: CodexHome) -> None:
    codex.rollout(A)
    codex.state_db([(A, "old version", None)], version=4)
    codex.state_db([(A, "new version", None)], version=12)
    assert search(run, codex, command="list")["sessions"][0]["title"] == "new version"


def test_message_date_cwd_and_query_use_same_subset(run: Runner, codex: CodexHome) -> None:
    codex.rollout(
        A,
        [
            {"text": "old needle", "cwd": "C:/old", "timestamp": "2026-09-01T00:00:00Z"},
            {"text": "new topic", "cwd": "C:/new", "timestamp": "2026-11-01T00:00:00Z"},
        ],
    )
    assert search(run, codex, "--cwd-contains", "new")["sessions"] == []
    flags = ["--cwd-field", "message", "--cwd-contains", "new", "--date-field", "message", "--date-from", "2026-11-01"]
    assert len(search(run, codex, *flags)["sessions"]) == 1
    assert search(run, codex, *flags, "--query", "needle")["sessions"] == []
    message = search(run, codex, *flags)["sessions"][0]["messages"][0]
    assert (message["cwd"], message["turn_id"], message["line_number"]) == ("C:/new", "1", 5)


def test_wsl_and_extended_paths_match_windows_paths(run: Runner, codex: CodexHome) -> None:
    codex.rollout(A, cwd="/mnt/c/path/to/example")
    codex.rollout(B, cwd="\\\\?\\D:\\path\\to\\other")
    assert ids(search(run, codex, "--cwd-contains", "C:\\Path\\To\\Example")) == [A]
    assert ids(search(run, codex, "--cwd-contains", "d:/path/to/other")) == [B]


def test_created_updated_and_message_dates(run: Runner, codex: CodexHome) -> None:
    codex.rollout(A)
    assert search(run, codex, "--date-from", "2026-11-01")["sessions"] == []
    for field in ("updated", "message"):
        assert len(search(run, codex, "--date-field", field, "--date-from", "2026-11-01")["sessions"]) == 1


def test_date_boundary_uses_explicit_offset(run: Runner, codex: CodexHome) -> None:
    codex.rollout(A, [{"text": "late", "timestamp": "2026-10-31T15:01:00Z"}])
    flags = ["--date-field", "message", "--date-from", "2026-11-01"]
    assert search(run, codex, *flags)["sessions"] == []
    assert len(search(run, codex, *flags, "--utc-offset=+09:00")["sessions"]) == 1


def test_default_order_is_latest_message_first_with_limit(run: Runner, codex: CodexHome) -> None:
    codex.rollout(A, [{"text": "recent", "timestamp": "2026-11-01T00:00:00Z"}], created="2026-09-01T00:00:00Z")
    codex.rollout(B, [{"text": "older", "timestamp": "2026-10-01T00:00:00Z"}], created="2026-10-01T00:00:00Z")
    report = search(run, codex, "--limit", "1")
    assert ids(report) == [A]
    assert report["coverage"]["matched_sessions"] == 2
    assert ids(search(run, codex, "--sort-by", "created", "--order", "asc", "--limit", "1")) == [A]
    assert ids(search(run, codex, "--sort-by", "created", "--limit", "1")) == [B]


def test_legacy_rollout_is_still_searchable(run: Runner, codex: CodexHome) -> None:
    (codex.active / "legacy.jsonl").write_bytes((FIXTURES / "codex-legacy-session.jsonl").read_bytes())
    session = search(run, codex)["sessions"][0]
    assert session["session_id"] == "legacy-thread-0001"
    assert session["details"]["repository_url"] == "https://example.invalid/example/project.git"
    assert [m["text"] for m in session["messages"]] == [
        "Rebuild this project context.",
        "The project context was reviewed.",
    ]


def test_parser_warnings_and_unknown_logs_are_counted(run: Runner, codex: CodexHome) -> None:
    path = codex.rollout(A)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{invalid\n")
    (codex.active / "unknown.jsonl").write_text("{}\n", encoding="utf-8")
    codex.rollout(B, [])
    report = search(run, codex)
    assert coverage(report)["parser_warnings"] == 1
    assert coverage(report)["files_without_sessions"] == 1
    assert coverage(report)["files_without_messages"] == 1


def test_missing_default_archive_is_not_an_error(run: Runner, codex: CodexHome) -> None:
    codex.archived.rmdir()
    codex.rollout(A)
    assert ids(search(run, codex)) == [A]
