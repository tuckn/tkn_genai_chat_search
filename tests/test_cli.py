"""Command line contract: help, version, output formats, stdout/stderr, exit codes, read-only use."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ClaudeHome, CodexHome, Runner, thread_id

from tkn_genai_chat_search import __version__, cli

A = thread_id(1)


def roots(codex: CodexHome, claude: ClaudeHome) -> list[str]:
    return ["--root", f"codex={codex.root}", "--root", f"claude-code={claude.root}"]


def test_version_and_help_need_no_configuration(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (["--version"], ["--help"], ["search", "--help"], ["config", "init", "--help"]):
        with pytest.raises(SystemExit) as raised:
            cli.main(argv)
        assert raised.value.code == 0
    out = capsys.readouterr().out
    assert f"tkn-genai-chat-search {__version__}" in out
    assert "exit codes:" in out and "--json" in out


def test_package_version_matches_metadata() -> None:
    from importlib.metadata import version

    assert version("tkn-genai-chat-search") == __version__


def test_both_sources_are_searched_by_default(run: Runner, codex: CodexHome, claude: ClaudeHome) -> None:
    codex.rollout(A, [{"text": "codex needle", "timestamp": "2026-11-02T00:00:00Z"}])
    claude.write([claude.message("claude needle")])
    report = run("search", "--query", "needle", "--json", *roots(codex, claude)).json
    assert [s["source"] for s in report["sessions"]] == ["codex", "claude-code"]
    assert [c["source"] for c in report["coverage"]["sources"]] == ["codex", "claude-code"]
    assert report["schema_version"] == 1 and report["command"] == "search"


def test_list_prints_key_value_lines_with_copyable_paths(run: Runner, codex: CodexHome, claude: ClaudeHome) -> None:
    path = codex.rollout(A, [{"text": "line one\nline two"}])
    codex.state_db([(A, "Release plan", None)])
    result = run("list", *roots(codex, claude))
    assert result.code == 0
    lines = result.out.splitlines()
    assert "command=list" in lines
    assert f"sessions[0].session_id={A}" in lines
    assert "sessions[0].title=Release plan" in lines
    assert f"sessions[0].path={path.absolute()}" in lines
    assert "sessions[0].first_user_message=line one line two" in lines
    assert "\x1b[" not in result.out
    assert all("=" in line for line in lines)
    assert result.err.startswith("[INFO] Read ")


def test_search_markdown_quotes_evidence_with_coverage(run: Runner, codex: CodexHome, claude: ClaudeHome) -> None:
    codex.rollout(A, [{"text": "first\nneedle here"}])
    result = run("search", "--query", "needle", *roots(codex, claude))
    assert result.out.startswith("# Chat Search Results\n")
    assert "## Search coverage" in result.out
    assert "files: 1 found (jsonl 1); 1 read" in result.out
    assert "> first\n> needle here" in result.out
    assert 'Match: "needle"' in result.out


def test_zero_matches_is_success_with_coverage(run: Runner, codex: CodexHome, claude: ClaudeHome) -> None:
    codex.rollout(A)
    result = run("search", "--query", "absent", "--json", *roots(codex, claude))
    assert result.code == 0
    assert result.json["sessions"] == []
    assert result.json["coverage"]["sources"][0]["files_read"] == 1


def test_show_reports_missing_ids_with_exit_1(run: Runner, codex: CodexHome, claude: ClaudeHome) -> None:
    codex.rollout(A)
    result = run("show", A, "missing-id", "--json", *roots(codex, claude))
    assert result.code == 1
    assert result.json["status"] == "partial"
    assert result.json["missing_session_ids"] == ["missing-id"]
    assert [s["session_id"] for s in result.json["sessions"]] == [A]
    assert "[ERROR] session not found: missing-id" in result.err
    markdown = run("show", "missing-id", *roots(codex, claude))
    assert markdown.code == 1
    assert "Not found: `missing-id`" in markdown.out


def test_sources_reports_missing_default_roots_without_failing(run: Runner) -> None:
    result = run("sources", "--json")
    assert result.code == 0
    roots_seen = [root for source in result.json["coverage"]["sources"] for root in source["roots"]]
    assert {root["exists"] for root in roots_seen} == {False}
    lines = run("sources").out.splitlines()
    assert "codex.roots[0].exists=false" in lines


def test_no_log_folder_is_a_failure_not_an_empty_result(run: Runner) -> None:
    result = run("search", "--query", "x", "--json")
    assert result.code == 1
    assert result.json["status"] == "failed"
    assert "no chat log folder found" in result.json["error"]
    assert "[ERROR]" in result.err


@pytest.mark.parametrize(
    "options",
    [
        ("search", "--query", ""),
        ("search", "--title", " 　 "),
        ("search", "--regex", "--query", "["),
        ("search", "--limit", "-1"),
        ("search", "--utc-offset=+24:00"),
        ("search", "--date-from", "2026-11-02", "--date-to", "2026-11-01"),
        ("search", "--date-from", "11/02/2026"),
        ("search", "--root", "codex"),
        ("search", "--root", "chatgpt=C:/x"),
        ("search", "--root", "codex=C:/path/that/does/not/exist"),
        ("search", "--source", "chatgpt"),
        ("list", "-q", "-v"),
        ("show",),
        ("config",),
        (),
    ],
)
def test_invalid_arguments_exit_2_in_the_selected_format(
    run: Runner, codex: CodexHome, claude: ClaudeHome, options: tuple[str, ...]
) -> None:
    base = roots(codex, claude) if options and options[0] == "search" and "--root" not in options else []
    result = run(*options, *base)
    assert result.code == 2, result.out
    assert result.out.splitlines()[0].startswith("schema_version=1")
    assert "status=failed" in result.out.splitlines()
    as_json = run(*options, *base, "--json")
    assert as_json.code == 2
    assert as_json.json["exit_code"] == 2
    assert "[ERROR]" in as_json.err


def test_options_can_follow_or_precede_the_command(run: Runner, codex: CodexHome, claude: ClaudeHome) -> None:
    codex.rollout(A)
    before = run("--json", "-q", "list", *roots(codex, claude))
    after = run("list", "--json", "-q", *roots(codex, claude))
    assert before.code == after.code == 0
    assert json.loads(before.out)["sessions"] == json.loads(after.out)["sessions"]
    assert before.err == ""


def test_quiet_and_verbose_change_stderr_only(run: Runner, codex: CodexHome, claude: ClaudeHome) -> None:
    codex.rollout(A)
    normal = run("list", "--json", *roots(codex, claude))
    quiet = run("list", "--json", "-q", *roots(codex, claude))
    verbose = run("list", "--json", "-v", *roots(codex, claude))
    assert normal.out == quiet.out == verbose.out
    assert quiet.err == ""
    assert "[INFO]" in normal.err


def test_installed_entry_point_is_read_only_from_an_unrelated_folder(codex: CodexHome, tmp_path: Path) -> None:
    codex.rollout(A, [{"text": "local evidence"}], compress=True)
    codex.state_db([(A, "Evidence", None)])
    before = {p: p.read_bytes() for p in codex.root.rglob("*") if p.is_file()}
    outside = tmp_path / "outside"
    outside.mkdir()
    environment = {**os.environ, "HOME": str(tmp_path / "home"), "USERPROFILE": str(tmp_path / "home")}
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tkn_genai_chat_search",
            "search",
            "--source",
            "codex",
            "--root",
            f"codex={codex.root}",
            "--query",
            "evidence",
            "--json",
            "-q",
        ],
        cwd=outside,
        env=environment,
        capture_output=True,
        check=True,
    )
    report = json.loads(completed.stdout.decode("utf-8"))
    assert report["sessions"][0]["messages"][0]["text"] == "local evidence"
    assert report["sessions"][0]["title"] == "Evidence"
    assert completed.stderr == b""
    after = {p: p.read_bytes() for p in codex.root.rglob("*") if p.is_file()}
    assert after == before
    assert list(outside.iterdir()) == []
