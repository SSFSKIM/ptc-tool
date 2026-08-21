"""Lossless access to Claude Code session transcripts (the PRO-LONG lever)."""
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .state import STATE


def _munge(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _projects_root() -> Path:
    """Where the engine writes session transcripts.

    Claude Code resolves its config home as `CLAUDE_CONFIG_DIR` or `~/.claude`
    (`Claude Code Src/src/utils/envUtils.ts` `getClaudeConfigHomeDir`) and puts `projects/`
    under it. A session launched with that variable set has no transcript under `~/.claude`
    at all, so hardcoding the home made `history()` raise FileNotFoundError for every one
    of them. The kernel inherits the launching Claude's environment, so reading it here
    reads the same value the engine used.
    """
    home = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(home) if home else Path.home() / ".claude") / "projects"


def _resolve_path(session_id: str, cwd: str | None) -> Path:
    root = _projects_root()
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

    @staticmethod
    def _row_text(row: dict) -> str:
        """One row's plain text — "" for a row that carries none (a tool result, an
        image block). Shared by the per-role projections and by text()."""
        msg = row.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(b.get("text", "") for b in content
                           if isinstance(b, dict) and b.get("type") == "text")
        return ""

    def _texts(self, role: str) -> list:
        out = []
        for row in self.messages:
            if row.get("type") != role:
                continue
            t = self._row_text(row)
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
        """The conversation as text, in the order the JSONL recorded it.

        Projecting all user turns and then all assistant turns (what this did before)
        reads as a plausible conversation while saying who answered what wrongly — the
        one thing a lossless-recall lever must not do.
        """
        out = []
        for row in self.messages:
            if row.get("type") not in ("user", "assistant"):
                continue
            t = self._row_text(row)
            if t:
                out.append(t)
        return "\n".join(out)


def history(session: str | None = None, cwd: str | None = None) -> Transcript:
    from ptc.discovery import read_meta
    meta = read_meta(STATE.config.get("key", ""))
    if session is None:
        session = meta.get("claude_session_id")
        if not session:
            raise RuntimeError("no claude_session_id known for this kernel "
                               "(alias-keyed session) — pass history(session=...) explicitly")
    # meta.json's cwd is the session's real working directory (ensure_kernel writes it
    # from discovery's resolved hook-runfile value); the kernel process's own PTC_CWD is
    # the same value, kept as a fallback for a meta.json that predates the field. NOT
    # STATE.config: the bootstrap payload has no cwd key, which made the direct-path tier
    # dead code in every real kernel and sent every history() call through the glob.
    cwd = cwd or meta.get("cwd") or os.environ.get("PTC_CWD")
    path = _resolve_path(session, cwd)
    messages = []
    for line in path.read_text().splitlines():
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return Transcript(path, messages)
