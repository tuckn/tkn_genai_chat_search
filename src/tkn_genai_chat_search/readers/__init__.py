"""One reader per chat log format. Each turns its files into the common session shape."""

from __future__ import annotations

from .base import Reader, ReadOptions, Root
from .claude_code import ClaudeCodeReader
from .codex import CodexReader

READERS: dict[str, Reader] = {"codex": CodexReader(), "claude-code": ClaudeCodeReader()}

__all__ = ["READERS", "ReadOptions", "Reader", "Root"]
