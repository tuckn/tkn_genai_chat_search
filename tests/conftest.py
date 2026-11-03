"""Synthetic Codex and Claude Code folders, and a CLI runner isolated from real configuration."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import zstandard

from tkn_genai_chat_search import cli


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No real home, user configuration, current-folder configuration or default logs."""
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "NO_COLOR", "TERM"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(work)
    return tmp_path


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def json(self) -> Any:
        return json.loads(self.out)


class Runner:
    def __init__(self, capsys: pytest.CaptureFixture[str]) -> None:
        self.capsys = capsys

    def __call__(self, *args: str) -> Result:
        self.capsys.readouterr()
        code = cli.main(list(args))
        captured = self.capsys.readouterr()
        return Result(code, captured.out, captured.err)


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Runner:
    return Runner(capsys)


def write_jsonl(path: Path, records: Iterable[Any], *, compress: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode("utf-8")
    if compress:
        data = zstandard.ZstdCompressor().compress(data)
    path.write_bytes(data)
    return path


class ClaudeHome:
    """`<root>/projects/encoded-project/<session>.jsonl` with helpers for records."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.projects = root / "projects"
        self.project = self.projects / "encoded-project"
        self.project.mkdir(parents=True)

    def message(self, text: Any = "request", *, role: str = "user", uuid: str | None = "u1", **meta: Any) -> dict:
        record = {
            "type": role,
            "message": {"role": role, "content": text},
            "uuid": uuid,
            "sessionId": "example",
            "parentUuid": None,
            "isSidechain": False,
            "timestamp": "2026-11-01T00:00:00Z",
            "cwd": "C:/work/example-project",
        }
        record.update(meta)
        return record

    def write(
        self, records: list[dict] | None = None, *, relative: str = "example.jsonl", project: Path | None = None
    ) -> Path:
        return write_jsonl((project or self.project) / relative, records if records is not None else [self.message()])

    def titled(self, name: str, titles: list[tuple[str, str, str]]) -> Path:
        records = [self.message(f"{name} body", sessionId=name)]
        records += [{"type": kind, "sessionId": name, field: value} for kind, field, value in titles]
        return self.write(records, relative=f"{name}.jsonl")


THREAD = "019e0000-0000-7000-8000-{:012d}"


def thread_id(number: int) -> str:
    return THREAD.format(number)


class CodexHome:
    """`<root>/sessions` and `archived_sessions` rollouts, plus optional title stores."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.active = root / "sessions"
        self.archived = root / "archived_sessions"
        self.active.mkdir(parents=True)
        self.archived.mkdir()

    def rollout(
        self,
        session_id: str,
        messages: list[dict] | None = None,
        *,
        archived: bool = False,
        compress: bool = False,
        created: str = "2026-09-01T00:00:00Z",
        cwd: str = "C:/old",
        thread_source: str | None = "user",
        source: Any = "vscode",
        parent: str | None = None,
        segment: str = "",
        extra: list[dict] | None = None,
        name: str | None = None,
    ) -> Path:
        meta: dict[str, Any] = {"id": session_id, "timestamp": created, "cwd": cwd, "source": source}
        if thread_source is not None:
            meta["thread_source"] = thread_source
        if parent:
            meta["parent_thread_id"] = parent
        records: list[dict] = [{"type": "session_meta", "payload": meta}]
        for index, message in enumerate(messages if messages is not None else [{"text": "request"}]):
            turn = message.get("turn", str(index))
            records.append({"type": "turn_context", "payload": {"turn_id": turn, "cwd": message.get("cwd", cwd)}})
            records.append(
                {
                    "type": "response_item",
                    "timestamp": message.get("timestamp", "2026-11-01T02:00:00Z"),
                    "payload": {
                        "type": "message",
                        "id": message.get("id", f"msg_{index}"),
                        "role": message.get("role", "user"),
                        "phase": message.get("phase", ""),
                        "content": [{"type": "input_text", "text": message["text"]}],
                    },
                }
            )
        records += extra or []
        folder = self.archived if archived else self.active / "2026" / "11" / "01"
        stem = name or f"rollout-2026-11-01T00-00-00-{session_id}{'_' + segment if segment else ''}"
        suffix = ".jsonl.zst" if compress else ".jsonl"
        return write_jsonl(folder / f"{stem}{suffix}", records, compress=compress)

    def index(self, rows: list[dict]) -> Path:
        return write_jsonl(self.root / "session_index.jsonl", rows)

    def state_db(self, rows: list[tuple[str, str | None, str | None]], *, version: int = 5, columns: str = "") -> Path:
        path = self.root / f"state_{version}.sqlite"
        connection = sqlite3.connect(path)
        try:
            connection.execute(columns or "CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, title TEXT, cwd TEXT)")
            if not columns:
                connection.executemany("INSERT INTO threads (id, name, title) VALUES (?, ?, ?)", rows)
            connection.commit()
        finally:
            connection.close()
        return path


@pytest.fixture
def claude(isolated: Path) -> ClaudeHome:
    return ClaudeHome(isolated / "claude")


@pytest.fixture
def codex(isolated: Path) -> CodexHome:
    return CodexHome(isolated / "codex")
