"""Library reads from bytes: tool executions and turn origins, without changing the search output."""

from __future__ import annotations

import json
from typing import Any

import zstandard

from tkn_genai_chat_search.library import (
    claude_code_transcript_kind,
    codex_rollout_status,
    read_claude_code,
    read_codex,
    read_codex_titles,
)

SESSION = "11111111-2222-3333-4444-555555555555"
THREAD = "0199aaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee"


def lines(*records: dict[str, Any]) -> bytes:
    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records).encode()


def claude_record(kind: str, content: Any, **fields: Any) -> dict[str, Any]:
    return {
        "type": kind,
        "sessionId": SESSION,
        "cwd": "C:\\path\\to\\repo",
        "timestamp": "2026-11-04T05:00:00Z",
        "uuid": fields.pop("uuid", kind),
        "message": {"role": kind, "content": content},
        **fields,
    }


def test_claude_code_tools_are_paired_and_turn_origin_is_kept() -> None:
    data = lines(
        claude_record("user", "Please read the note", uuid="u1", turnOrigin="human", promptSource="typed"),
        claude_record(
            "assistant",
            [
                {"type": "text", "text": "Reading"},
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "C:\\path\\to\\note.md"}},
                {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "rg missing"}},
            ],
            uuid="a1",
        ),
        claude_record(
            "user",
            [
                {"type": "tool_result", "tool_use_id": "t1", "content": "line 1\nline 2"},
                {"type": "tool_result", "tool_use_id": "t2", "is_error": True, "content": "Exit code 1"},
            ],
            uuid="u2",
        ),
        claude_record("user", "Task from another session", uuid="u3", turnOrigin="sdk"),
        {"type": "custom-title", "sessionId": SESSION, "customTitle": "Read the note"},
    )
    result = read_claude_code(data, f"projects/C--path-to-repo/{SESSION}.jsonl")
    session = result.session
    assert session is not None and session.session_id == SESSION and session.kind == "main"
    assert session.paths == [f"projects/C--path-to-repo/{SESSION}.jsonl"]
    assert [event.name for event in session.tool_events] == ["Read", "Bash"]
    read, bash = session.tool_events
    assert read.arguments == {"file_path": "C:\\path\\to\\note.md"} and read.is_error is False
    assert read.result_line_number == 3 and read.output_chars == len("line 1\nline 2")
    assert bash.is_error is True and bash.error_preview == "Exit code 1"
    users = [m for m in session.messages if m.role == "user"]
    assert [m.text for m in users] == ["Please read the note", "Task from another session"]
    assert session.message_attributes[1]["turnOrigin"] == "human"
    assert session.message_attributes[4]["turnOrigin"] == "sdk"
    assert "tool_events" not in session.summary() and session.title == "Read the note"


def test_detail_is_off_for_the_search_shape() -> None:
    data = lines(
        claude_record("user", "hello", uuid="u1", turnOrigin="human"),
        claude_record("assistant", [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}], uuid="a1"),
    )
    session = read_claude_code(data, f"projects/x/{SESSION}.jsonl", detail=False).session
    assert session is not None and session.tool_events == [] and session.message_attributes == {}


def test_claude_code_paths() -> None:
    assert claude_code_transcript_kind(f"projects/x/{SESSION}.jsonl") == "main"
    assert claude_code_transcript_kind(f"projects/x/{SESSION}/subagents/agent-a1.jsonl") == "subagent"
    assert claude_code_transcript_kind(f"projects/x/{SESSION}/tool-results/r.txt") is None
    assert claude_code_transcript_kind(f"projects/x/{SESSION}.orphaned-1.jsonl") is None
    assert claude_code_transcript_kind("history.jsonl") is None


def codex_rollout() -> bytes:
    payload_meta = {"id": THREAD, "timestamp": "2026-11-04T00:00:00Z", "cwd": "C:\\path\\to\\repo"}
    return lines(
        {"type": "session_meta", "timestamp": "2026-11-04T00:00:00Z", "payload": payload_meta},
        {"type": "turn_context", "payload": {"turn_id": "turn-1", "cwd": "C:\\path\\to\\repo"}},
        {
            "type": "response_item",
            "timestamp": "2026-11-04T00:00:01Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Run the tests"}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-11-04T00:00:02Z",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "call_id": "c1",
                "arguments": json.dumps({"command": ["pytest"]}),
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-11-04T00:00:03Z",
            "payload": {
                "type": "function_call_output",
                "call_id": "c1",
                "output": json.dumps({"output": "1 failed", "metadata": {"exit_code": 1}}),
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-11-04T00:00:04Z",
            "payload": {
                "type": "item_completed",
                "turn_id": "turn-1",
                "item": {
                    "type": "CommandExecution",
                    "id": "exec-1",
                    "command": ["pwsh", "-Command", "Get-Content a.md"],
                    "cwd": "C:\\path\\to\\repo",
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": "text",
                },
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-11-04T00:00:05Z",
            "payload": {"type": "item_completed", "item": {"type": "AgentMessage", "id": "m1"}},
        },
    )


def test_codex_calls_outputs_and_completed_items() -> None:
    data = zstandard.ZstdCompressor().compress(codex_rollout())
    path = f"archived_sessions/rollout-2026-11-04T00-00-00-{THREAD}.jsonl.zst"
    titles, store = read_codex_titles(lines({"id": THREAD, "thread_name": "Run tests"}))
    result = read_codex(data, path, titles=titles)
    session = result.session
    assert session is not None and session.title == "Run tests" and store.titles == 1
    assert session.details["archive_status"] == "archived"
    call, item = session.tool_events
    assert call.name == "shell" and call.arguments == {"command": ["pytest"]} and call.origin == "call"
    assert call.is_error is True and call.result_line_number == 5 and call.turn_id == "turn-1"
    assert item.origin == "item" and item.name == "CommandExecution" and item.is_error is False
    assert item.arguments["command"][-1] == "Get-Content a.md" and item.turn_id == "turn-1"
    assert [m.text for m in session.messages] == ["Run the tests"]


def test_codex_paths() -> None:
    assert codex_rollout_status("sessions/2026/11/04/rollout-x.jsonl") == "active"
    assert codex_rollout_status("archived_sessions/rollout-x.jsonl.zst") == "archived"
    assert codex_rollout_status("session_index.jsonl") is None
    assert codex_rollout_status("sessions/2026/11/04/rollout-x.json") is None


def test_tool_only_subagent_transcript_keeps_its_executions() -> None:
    data = lines(
        claude_record(
            "assistant",
            [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a"}}],
            uuid="a1",
            isSidechain=True,
        ),
    )
    path = f"projects/x/{SESSION}/subagents/agent-a1.jsonl"
    session = read_claude_code(data, path).session
    assert session is not None and session.kind == "subagent" and session.parent_session_id == SESSION
    assert [event.name for event in session.tool_events] == ["Read"] and session.messages == []
    assert read_claude_code(data, path, detail=False).session is None


def test_tool_only_codex_rollout_and_completion_events_keep_results() -> None:
    meta = {"id": THREAD, "timestamp": "2026-11-04T00:00:00Z", "cwd": "C:\\path\\to\\repo"}
    data = lines(
        {"type": "session_meta", "timestamp": "2026-11-04T00:00:00Z", "payload": meta},
        {
            "type": "response_item",
            "timestamp": "2026-11-04T00:00:01Z",
            "payload": {"type": "function_call", "name": "shell", "call_id": "c1", "arguments": "{}"},
        },
        {
            "type": "event_msg",
            "timestamp": "2026-11-04T00:00:02Z",
            "payload": {
                "type": "exec_command_end",
                "call_id": "c1",
                "exit_code": 2,
                "stderr": "boom",
                "aggregated_output": "out",
            },
        },
    )
    path = f"sessions/2026/11/04/rollout-2026-11-04T00-00-00-{THREAD}.jsonl"
    result = read_codex(data, path)
    assert result.session is not None and result.session.messages == []
    (event,) = result.session.tool_events
    assert event.is_error is True and event.result_line_number == 3 and event.output_chars == 3
    assert event.result_timestamp == "2026-11-04T00:00:02Z" and event.error_preview == "boom"
    assert read_codex(data, path, detail=False).session is None


def test_single_log_coverage_and_damaged_title_index() -> None:
    data = lines(claude_record("user", "hello", uuid="u1", turnOrigin="human"))
    coverage = read_claude_code(data, f"projects/x/{SESSION}.jsonl").coverage_details
    assert coverage["files_discovered"] == coverage["files_read"] == coverage["sessions_read"] == 1
    assert coverage["sessions_by_kind"] == {"main": 1} and coverage["files_by_format"] == {"jsonl": 1}
    empty = read_claude_code(b"", f"projects/x/{SESSION}.jsonl").coverage_details
    assert empty["files_without_messages"] == 1 and empty["sessions_read"] == 0
    titles, store = read_codex_titles(b'not json\n{"id": "a", "thread_name": "Kept"}\n')
    assert titles["a"][0].title == "Kept" and store.status == "partial" and store.detail == "1 records skipped"


def test_legacy_agent_file_next_to_transcripts_is_a_subagent() -> None:
    assert claude_code_transcript_kind("projects/x/agent-a1.jsonl") == "subagent"


def test_codex_results_from_items_structured_outputs_and_status_lines() -> None:
    meta = {"id": THREAD, "timestamp": "2026-11-04T00:00:00Z", "cwd": "C:\\path"}
    data = lines(
        {"type": "session_meta", "timestamp": "2026-11-04T00:00:00Z", "payload": meta},
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "x1", "input": "run"},
        },
        {
            "type": "event_msg",
            "timestamp": "2026-11-04T00:00:01Z",
            "payload": {
                "type": "item_completed",
                "item": {"type": "CommandExecution", "id": "x1", "status": "failed", "exit_code": 1},
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call", "name": "shell", "call_id": "x2", "arguments": "{}"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "x2",
                "output": {"content": "boom", "metadata": {"exit_code": 1}},
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call", "name": "read", "call_id": "x3", "arguments": "{}"},
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "x3", "output": "Use Exit code: 1 in the fixture"},
        },
    )
    session = read_codex(data, f"sessions/rollout-{THREAD}.jsonl").session
    assert session is not None
    by_id = {event.call_id: event for event in session.tool_events}
    assert len(by_id) == 3
    assert by_id["x1"].is_error is True and by_id["x1"].result_line_number == 3
    assert by_id["x2"].is_error is True and by_id["x2"].error_preview == "boom"
    assert by_id["x3"].is_error is None


def test_tool_only_sidechain_transcript_is_a_subagent() -> None:
    data = lines(
        claude_record(
            "assistant", [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}], uuid="a1", isSidechain=True
        ),
    )
    session = read_claude_code(data, f"projects/x/{SESSION}.jsonl").session
    assert session is not None and session.kind == "subagent"
