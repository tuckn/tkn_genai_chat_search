"""Claude Code reader and the shared search rules, ported from the plugin's Claude Code search tests."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import ClaudeHome, Runner


def search(run: Runner, claude: ClaudeHome, *options: str) -> dict:
    result = run("search", "--source", "claude-code", "--root", f"claude-code={claude.root}", "--json", *options)
    assert result.code == 0, result.err
    return result.json


def first(run: Runner, claude: ClaudeHome, *options: str) -> dict:
    return search(run, claude, *options)["sessions"][0]


def coverage(report: dict) -> dict:
    return report["coverage"]["sources"][0]


def test_text_blocks_and_provenance_without_nested_tool_or_thinking_text(run: Runner, claude: ClaudeHome) -> None:
    path = claude.write(
        [
            {"type": "api-request-blob", "message": {"role": "assistant", "content": "hidden request"}},
            claude.message("構成\n```python\nprint(1)\n```"),
            claude.message(
                [
                    {"type": "thinking", "thinking": "hidden reasoning", "text": "hidden thinking text"},
                    {"type": "text", "text": "Answer"},
                    {"type": "tool_use", "input": {"text": "hidden input"}},
                    {"type": "text", "text": "Done"},
                ],
                role="assistant",
                uuid="a1",
                parentUuid="u1",
            ),
            claude.message([{"type": "tool_result", "content": [{"type": "text", "text": "hidden output"}]}], uuid="t"),
        ]
    )
    result = first(run, claude)
    assert result["paths"] == [str(path.absolute())]
    assert [m["line_number"] for m in result["messages"]] == [2, 3]
    assert result["messages"][1]["parent_id"] == "u1"
    assert result["messages"][1]["message_id"] == "a1"
    assert result["messages"][1]["text"] == "Answer\nDone"
    assert "\n```python\n" in result["messages"][0]["text"]
    assert "hidden " not in json.dumps(result)
    assert search(run, claude, "--query", "hidden")["sessions"] == []


def test_exact_duplicate_uuid_only_and_physical_order(run: Runner, claude: ClaudeHome) -> None:
    repeated = claude.message("same", timestamp="2026-11-02T00:00:00Z")
    claude.write(
        [
            repeated,
            repeated,
            claude.message("same", uuid="u2", timestamp="2026-11-01T00:00:00Z"),
            claude.message("changed", uuid="u1"),
            claude.message("no id", uuid=None),
            claude.message("no id", uuid=None),
        ]
    )
    report = search(run, claude)
    session = report["sessions"][0]
    assert [m["line_number"] for m in session["messages"]] == [1, 3, 4, 5, 6]
    assert coverage(report)["details"]["duplicate_messages"] == 1
    assert session["created"] == "2026-11-01T00:00:00Z"
    assert session["updated"] == "2026-11-02T00:00:00Z"


def test_generated_context_compaction_and_reminders_are_not_user_requests(run: Runner, claude: ClaudeHome) -> None:
    claude.write(
        [
            claude.message("meta needle", isMeta=True),
            claude.message("compact needle", uuid="c", isCompactSummary=True),
            {"type": "system", "subtype": "compact_boundary"},
            claude.message("<system-reminder>reminder needle</system-reminder>", uuid="r"),
            claude.message("<system-reminder>context needle</system-reminder>\nordinary request", uuid="u2"),
        ]
    )
    report = search(run, claude)
    assert coverage(report)["details"]["excluded_generated_messages"] == 3
    assert coverage(report)["details"]["ignored_record_types"]["system"] == 1
    assert [m["text"] for m in report["sessions"][0]["messages"]] == ["ordinary request"]
    assert search(run, claude, "--query", "needle")["sessions"] == []


def test_queries_are_and_across_messages_and_do_not_match_metadata(run: Runner, claude: ClaudeHome) -> None:
    claude.write([claude.message("ALPHA"), claude.message("beta", role="assistant", uuid="a1")])
    assert len(search(run, claude, "--query", "alpha", "--query", "beta")["sessions"]) == 1
    assert search(run, claude, "--query", "example-project")["sessions"] == []
    assert search(run, claude, "--query", "alpha", "--query", "absent")["sessions"] == []
    assert search(run, claude, "--query", "beta", "--query-user-only")["sessions"] == []


def test_regex_is_opt_in(run: Runner, claude: ClaudeHome) -> None:
    claude.write([claude.message("alpha\nBETA")])
    assert search(run, claude, "--query", "^beta$")["sessions"] == []
    assert len(search(run, claude, "--query", "^beta$", "--regex")["sessions"]) == 1


def test_hidden_assistant_hits_do_not_leak_in_either_format(run: Runner, claude: ClaudeHome) -> None:
    claude.write([claude.message("public"), claude.message("private needle", role="assistant", uuid="a")])
    report = search(run, claude, "--query", "needle", "--no-include-assistant")
    assert report["sessions"][0]["hidden_matching_message_count"] == 1
    assert [m["role"] for m in report["sessions"][0]["messages"]] == ["user"]
    assert "private needle" not in json.dumps(report)
    markdown = run(
        "search",
        "--root",
        f"claude-code={claude.root}",
        "--source",
        "claude-code",
        "--query",
        "needle",
        "--no-include-assistant",
    ).out
    assert "private needle" not in markdown
    assert "1 matching messages hidden by --no-include-assistant" in markdown


def test_middle_match_and_neighbor_have_priority_over_overview(run: Runner, claude: ClaudeHome) -> None:
    claude.write(
        [
            claude.message("needle" if i == 5 else f"message {i}", uuid=str(i), role="user" if i % 2 else "assistant")
            for i in range(12)
        ]
    )
    result = first(run, claude, "--query", "needle", "--messages-per-role", "1")
    assert [m["line_number"] for m in result["messages"]] == [5, 6]
    assert result["omitted_message_count"] == 10
    markdown = run("search", "--root", f"claude-code={claude.root}", "--source", "claude-code", "--query", "needle").out
    assert "| L6" in markdown


def test_all_matches_survive_soft_budget_and_distant_queries_have_windows(run: Runner, claude: ClaudeHome) -> None:
    claude.write([claude.message("needle " + str(i), uuid=str(i)) for i in range(12)])
    assert len(first(run, claude, "--query", "needle", "--messages-per-role", "1")["messages"]) == 12
    text = "x" * 2000 + "alpha" + "y" * 2000 + "omega"
    claude.write([claude.message(text)])
    message = first(run, claude, "--query", "alpha", "--query", "omega", "--max-message-chars", "80")["messages"][0]
    assert message["truncated"]
    for window in message["matched_excerpts"]:
        assert window["query"] in window["text"]
        assert len(window["text"]) <= 80
        assert text[window["match_start"] : window["match_end"]] == window["query"]


def test_show_returns_every_message_without_shortening(run: Runner, claude: ClaudeHome) -> None:
    claude.write([claude.message("long " + "x" * 1500, uuid=str(i)) for i in range(20)])
    result = run("show", "example", "--root", f"claude-code={claude.root}", "--json")
    assert result.code == 0
    session = result.json["sessions"][0]
    assert len(session["messages"]) == 20
    assert not any(m["truncated"] for m in session["messages"])
    assert session["omitted_message_count"] == 0


def test_titles_and_inherited_session_ids_do_not_reassign_destination(run: Runner, claude: ClaudeHome) -> None:
    claude.write(
        [
            claude.message("inherited", sessionId="old"),
            {"type": "custom-title", "sessionId": "example", "customTitle": "First title"},
            {"type": "custom-title", "sessionId": "old", "customTitle": "Wrong title"},
            {"type": "custom-title", "sessionId": "example", "customTitle": "Recorded title"},
            claude.message("new", uuid="u2"),
        ]
    )
    result = first(run, claude, "--session-id", "example")
    assert result["title"] == "Recorded title"
    assert [(t["title"], t["line_number"]) for t in result["titles"]] == [("First title", 2), ("Recorded title", 4)]
    assert result["details"]["recorded_session_ids"] == ["example", "old"]
    assert result["messages"][0]["session_id"] == "old"
    assert search(run, claude, "--session-id", "old")["sessions"] == []
    assert search(run, claude, "--query", "Recorded title")["sessions"] == []
    assert search(run, claude, "--title", "Wrong title")["sessions"] == []


def test_renaming_back_makes_the_earlier_title_current(run: Runner, claude: ClaudeHome) -> None:
    claude.titled(
        "example",
        [
            ("custom-title", "customTitle", "A"),
            ("custom-title", "customTitle", "B"),
            ("custom-title", "customTitle", "A"),
        ],
    )
    result = first(run, claude)
    assert result["title"] == "A"
    assert [(t["title"], t["line_number"]) for t in result["titles"]] == [("B", 3), ("A", 4)]


def test_title_matches_custom_ai_and_earlier_titles_ignoring_case_width_and_spaces(
    run: Runner, claude: ClaudeHome
) -> None:
    claude.titled(
        "shared",
        [
            ("ai-title", "aiTitle", "Share plugin settings"),
            ("custom-title", "customTitle", "Plugin設定をチームに共有する方法"),
        ],
    )
    claude.titled(
        "renamed",
        [("custom-title", "customTitle", "起動後の重い処理"), ("custom-title", "customTitle", "Plugin起動エラー")],
    )
    claude.titled("side", [("custom-title", "customTitle", "btw: エラーになります…")])
    claude.titled("untitled", [])

    def ids(*options: str) -> list[str]:
        result = run("list", "--root", f"claude-code={claude.root}", "--source", "claude-code", "--json", *options)
        return sorted(s["session_id"] for s in result.json["sessions"])

    assert ids("--title", "PLUGIN設定") == ["shared"]
    assert ids("--title", "Ｐｌｕｇｉｎ　設定を チーム") == ["shared"]
    assert ids("--title", "share plugin") == ["shared"]
    assert ids("--title", "plugin") == ["renamed", "shared"]
    assert ids("--title", "plugin", "--title", "共有") == ["shared"]
    assert ids("--title", "エラーになります...") == ["side"]
    assert ids("--title", "absent") == []
    renamed = first(run, claude, "--title", "起動後 の")
    assert renamed["title"] == "Plugin起動エラー"
    assert [(t["kind"], t["title"], t["line_number"]) for t in renamed["matched_titles"]] == [
        ("custom-title", "起動後の重い処理", 2)
    ]
    shared = first(run, claude, "--title", "share")
    assert shared["title"] == "Plugin設定をチームに共有する方法"
    assert [t["kind"] for t in shared["matched_titles"]] == ["ai-title"]


def test_title_combines_with_query_and_query_still_ignores_titles(run: Runner, claude: ClaudeHome) -> None:
    claude.titled("shared", [("custom-title", "customTitle", "Plugin設定をチームに共有する方法")])
    assert len(search(run, claude, "--title", "plugin", "--query", "shared body")["sessions"]) == 1
    assert search(run, claude, "--title", "plugin", "--query", "absent")["sessions"] == []
    assert search(run, claude, "--query", "Plugin設定")["sessions"] == []
    base = ("search", "--root", f"claude-code={claude.root}", "--source", "claude-code")
    assert "try `list --title`" in run(*base, "--query", "Plugin設定").out
    markdown = run(*base, "--title", "Plugin設定").out
    assert '- matched titles: "Plugin設定をチームに共有する方法" (custom-title, L2)' in markdown
    assert "try `list --title`" not in markdown


def test_nested_and_legacy_agents_are_separate_and_opt_in(run: Runner, claude: ClaudeHome) -> None:
    claude.write()
    claude.write([claude.message("nested", isSidechain=True, agentId="a")], relative="example/subagents/agent-a.jsonl")
    claude.write([claude.message("legacy", isSidechain=True)], relative="agent-b.jsonl")
    report = search(run, claude)
    assert len(report["sessions"]) == 1
    assert coverage(report)["excluded_sessions"] == {"subagent": 2}
    included = search(run, claude, "--include-subagents", "--session-id", "example")
    assert len(included["sessions"]) == 3
    agents = {s["details"]["agent_id"]: s for s in included["sessions"] if s["kind"] == "subagent"}
    assert set(agents) == {"a", "b"}
    assert agents["a"]["parent_session_id"] == "example"
    assert agents["b"]["parent_session_id"] == "example"


def test_session_id_hint_skips_other_files_by_name(run: Runner, claude: ClaudeHome) -> None:
    claude.write()
    claude.write([claude.message("other", sessionId="other")], relative="other.jsonl")
    claude.write([claude.message("nested", isSidechain=True)], relative="example/subagents/agent-a.jsonl")
    report = search(run, claude, "--session-id", "example")
    assert [s["session_id"] for s in report["sessions"]] == ["example"]
    assert coverage(report)["files_skipped_by_filter"] == 2
    shown = run("show", "agent-a", "--root", f"claude-code={claude.root}", "--json").json
    assert [s["kind"] for s in shown["sessions"]] == ["subagent"]
    assert shown["sessions"][0]["messages"][0]["text"] == "nested"


def test_inline_sidechains_are_excluded_and_tagged_when_included(run: Runner, claude: ClaudeHome) -> None:
    claude.write([claude.message("main"), claude.message("side needle", uuid="s", isSidechain=True)])
    assert coverage(search(run, claude))["excluded_sidechain_messages"] == 1
    assert search(run, claude, "--query", "needle")["sessions"] == []
    result = first(run, claude, "--include-subagents", "--query", "needle")
    assert result["messages"][1]["is_sidechain"]


def test_discovery_does_not_search_spilled_tools_memory_or_history(run: Runner, claude: ClaudeHome) -> None:
    claude.write()
    claude.write([claude.message("hidden")], relative="example/tool-results/spilled.jsonl")
    claude.write([claude.message("hidden")], relative="memory/copied.jsonl")
    claude.write([claude.message("hidden")], relative="example/mcp-tasks/copied.jsonl")
    claude.write([claude.message("hidden")], project=claude.projects, relative="history.jsonl")
    report = search(run, claude, "--include-subagents")
    assert coverage(report)["files_read"] == 1
    assert search(run, claude, "--query", "hidden")["sessions"] == []


def test_set_aside_logs_are_counted_not_read(run: Runner, claude: ClaudeHome) -> None:
    claude.write()
    claude.write([claude.message("parked needle")], relative="example.orphaned-123-ab.jsonl")
    report = search(run, claude, "--query", "needle")
    assert report["sessions"] == []
    assert coverage(report)["excluded_files"] == {"set_aside": 1}


def test_message_cwd_and_date_filters_apply_to_the_same_message(run: Runner, claude: ClaudeHome) -> None:
    claude.write(
        [
            claude.message("old needle", cwd="C:/old", timestamp="2026-10-31T14:59:59Z"),
            claude.message("new needle", uuid="u2", cwd="C:\\new", timestamp="2026-10-31T15:00:00Z"),
        ]
    )
    options = ("--date-field", "message", "--date-from", "2026-11-01", "--date-to", "2026-11-01", "--utc-offset=+09:00")
    assert search(run, claude, *options, "--cwd-field", "message", "--cwd-contains", "old")["sessions"] == []
    result = first(run, claude, *options, "--cwd-field", "message", "--cwd-contains", "c:/NEW/")
    assert [m["text"] for m in result["messages"]] == ["new needle"]
    assert search(run, claude, *options, "--query", "old")["sessions"] == []


def test_created_updated_and_ordering_before_limit(run: Runner, claude: ClaudeHome) -> None:
    claude.write(
        [
            claude.message(timestamp="2026-10-01T00:00:00Z"),
            claude.message("recent", uuid="u2", timestamp="2026-11-02T00:00:00Z"),
        ]
    )
    claude.write(
        [claude.message("second", sessionId="second", timestamp="2026-11-01T00:00:00Z")], relative="second.jsonl"
    )
    report = search(run, claude, "--limit", "1")
    assert report["coverage"]["matched_sessions"] == 2
    assert report["sessions"][0]["session_id"] == "example"
    assert first(run, claude, "--sort-by", "created", "--order", "desc", "--limit", "1")["session_id"] == "second"
    assert len(search(run, claude, "--date-field", "updated", "--date-from", "2026-11-02")["sessions"]) == 1
    assert search(run, claude, "--date-from", "2026-11-02")["sessions"] == []


def test_missing_values_stay_missing_and_invalid_dates_do_not_match(run: Runner, claude: ClaudeHome) -> None:
    claude.write(
        [{"type": "user", "message": {"content": "bare"}}, claude.message("invalid", uuid="u2", timestamp="bad")]
    )
    result = first(run, claude)
    for field in ("timestamp", "message_id", "parent_id", "cwd", "session_id"):
        assert result["messages"][0][field] == ""
    assert search(run, claude, "--date-field", "message", "--date-from", "2026-01-01")["sessions"] == []


def test_bad_records_are_counted_without_exposing_raw_data(run: Runner, claude: ClaudeHome) -> None:
    path = claude.write()
    with path.open("a", encoding="utf-8") as stream:
        stream.write('bad private raw record\n[]\n{"type":"user","message":null}\n')
    result = run("search", "--root", f"claude-code={claude.root}", "--source", "claude-code", "--json")
    assert result.code == 0
    report = result.json
    assert coverage(report)["parser_warnings"] == 3
    assert coverage(report)["files_with_parser_warnings"] == 1
    assert "malformed records" in result.err and "--verbose" in result.err
    assert len(report["sessions"][0]["messages"]) == 1
    verbose = run("search", "--root", f"claude-code={claude.root}", "--source", "claude-code", "--json", "-v")
    assert ":L2: invalid JSON record" in verbose.err
    assert "private raw" not in verbose.err


def test_unreadable_file_is_counted_and_does_not_abort_other_results(run: Runner, claude: ClaudeHome) -> None:
    claude.write()
    claude.write(relative="bad.jsonl").write_bytes(b"\xff\xfe\xfd")
    result = run("search", "--root", f"claude-code={claude.root}", "--source", "claude-code", "--json")
    assert coverage(result.json)["unreadable_files"] == 1
    assert len(result.json["sessions"]) == 1
    assert "[WARNING] cannot read" in result.err


def test_bom_and_files_without_supported_messages(run: Runner, claude: ClaudeHome) -> None:
    path = claude.write([claude.message("日本語")])
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    claude.write([{"type": "future-record", "text": "not conversation"}], relative="future.jsonl")
    report = search(run, claude)
    assert report["sessions"][0]["messages"][0]["text"] == "日本語"
    assert coverage(report)["files_without_messages"] == 1


def test_multiple_and_duplicate_roots_keep_distinct_files(run: Runner, claude: ClaudeHome, tmp_path: Path) -> None:
    claude.write()
    second = ClaudeHome(tmp_path / "other-claude")
    second.write()
    result = run(
        "search",
        "--source",
        "claude-code",
        "--json",
        "--root",
        f"claude-code={claude.root}",
        "--root",
        f"claude-code={second.root}",
        "--root",
        f"claude-code={claude.root}",
    )
    report = result.json
    assert coverage(report)["files_read"] == 2
    assert len(report["sessions"]) == 2
    assert report["sessions"][0]["paths"] != report["sessions"][1]["paths"]


def test_environment_default_root(run: Runner, claude: ClaudeHome, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    claude.write()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude.root))
    result = run("list", "--source", "claude-code", "--json")
    assert result.code == 0
    assert len(result.json["sessions"]) == 1
    assert result.json["coverage"]["sources"][0]["roots"][0]["origin"] == "default ($CLAUDE_CONFIG_DIR)"
