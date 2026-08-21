"""Lossless access to Claude Code session transcripts (the PRO-LONG lever)."""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .state import STATE


def _munge(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _resolve_path(session_id: str, cwd: str | None) -> Path:
    root = Path.home() / ".claude" / "projects"
    if cwd:
        p = root / _munge(cwd) / f"{session_id}.jsonl"
        if p.exists():
            return p
    hits = list(root.glob(f"*/{session_id}.jsonl"))
    if hits:
        return max(hits, key=lambda p: p.stat().st_mtime)
    raise FileNotFoundError(f"no transcript found for session {session_id!r} under {root}")


@dataclass
class Transcript:
    path: Path
    messages: list = field(default_factory=list)

    def _texts(self, role: str) -> list:
        out = []
        for row in self.messages:
            if row.get("type") != role:
                continue
            content = row.get("message", {}).get("content")
            if isinstance(content, str):
                out.append(content)
            elif isinstance(content, list):
                t = "".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
                if t:
                    out.append(t)
        return out

    def user(self) -> list:
        return self._texts("user")

    def assistant(self) -> list:
        return self._texts("assistant")

    def tool_calls(self, name: str | None = None) -> list:
        out = []
        for row in self.messages:
            content = row.get("message", {}).get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    if name is None or b.get("name") == name:
                        out.append(b)
        return out

    def search(self, pattern: str) -> list:
        rx = re.compile(pattern)
        return [row for row in self.messages if rx.search(json.dumps(row))]

    def text(self) -> str:
        return "\n".join(self.user() + self.assistant())


def history(session: str | None = None, cwd: str | None = None) -> Transcript:
    if session is None:
        from ptc.discovery import read_meta
        session = read_meta(STATE.config.get("key", "")).get("claude_session_id")
        if not session:
            raise RuntimeError("no claude_session_id known for this kernel "
                               "(alias-keyed session) — pass history(session=...) explicitly")
    cwd = cwd or STATE.config.get("cwd")
    path = _resolve_path(session, cwd)
    messages = []
    for line in path.read_text().splitlines():
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return Transcript(path, messages)
