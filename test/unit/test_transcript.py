import json
import os
import time

import pytest

from ptc.discovery import write_meta
from ptc.runtime import transcript
from ptc.runtime.state import STATE

ROWS = [
    {"type": "user", "message": {"role": "user", "content": "first question"}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "an answer"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": "file1\nfile2"}]}},
]


def _fake_home(tmp_path, monkeypatch, sid="s-42", cwd="/my/proj"):
    munged = "".join(c if c.isalnum() else "-" for c in cwd)
    d = tmp_path / ".claude" / "projects" / munged
    d.mkdir(parents=True)
    (d / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in ROWS))
    monkeypatch.setenv("HOME", str(tmp_path))
    return d / f"{sid}.jsonl"


def test_history_resolves_and_projects(tmp_path, monkeypatch):
    p = _fake_home(tmp_path, monkeypatch)
    h = transcript.history("s-42", cwd="/my/proj")
    assert h.path == p and len(h.messages) == 3
    assert h.user() == ["first question"]
    assert h.assistant() == ["an answer"]
    assert h.tool_calls()[0]["name"] == "Bash"
    assert h.tool_calls("Grep") == []
    assert len(h.search(r"file\d")) == 1
    assert "an answer" in h.text()


def test_history_glob_fallback(tmp_path, monkeypatch):
    _fake_home(tmp_path, monkeypatch, cwd="/other/place")
    h = transcript.history("s-42", cwd="/wrong/cwd")     # munge misses; glob finds
    assert len(h.messages) == 3


def _decoy(tmp_path, sid="s-42", dirname="-decoy-project"):
    """A same-session-id transcript in another project dir, stamped newest.

    The glob fallback breaks ties on mtime, so it resolves to this file; the direct
    munged-path hit never looks at it. That asymmetry is what lets a test tell the two
    resolution tiers apart.
    """
    d = tmp_path / ".claude" / "projects" / dirname
    d.mkdir(parents=True)
    p = d / f"{sid}.jsonl"
    p.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "decoy"}}))
    future = time.time() + 120
    os.utime(p, (future, future))
    return p


def test_history_default_call_resolves_via_meta_cwd(tmp_path, monkeypatch):
    """The default call shape — `history()`, the one the kernel binds and SKILL.md
    documents — must take the direct munged-path branch.

    The kernel's own `STATE.config` never carries a `cwd` key (run_bootstrap's payload
    is key/kernel_dir/idle_hours/max_concurrency/depth/max_depth), so sourcing cwd from
    there made the direct path dead code in every real kernel. meta.json does carry the
    session's real cwd, written by ensure_kernel from discovery's resolved value, and
    history() already reads that same dict for the session id.
    """
    real = _fake_home(tmp_path, monkeypatch, cwd="/my/proj")
    _decoy(tmp_path)
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "p"))
    monkeypatch.delenv("PTC_CWD", raising=False)
    write_meta("k1", kernel_key="k1", claude_session_id="s-42", cwd="/my/proj")
    monkeypatch.setattr(STATE, "config", {"key": "k1"})   # exactly what bootstrap installs

    h = transcript.history()

    assert h.path == real
    assert h.user() == ["first question"]


def test_history_default_call_falls_back_to_ptc_cwd_env(tmp_path, monkeypatch):
    """When meta carries no cwd (an older kernel dir, or a degraded discovery rung), the
    kernel process's own PTC_CWD is the next-best direct-path source before globbing."""
    real = _fake_home(tmp_path, monkeypatch, cwd="/my/proj")
    _decoy(tmp_path)
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "p"))
    monkeypatch.setenv("PTC_CWD", "/my/proj")
    write_meta("k1", kernel_key="k1", claude_session_id="s-42")
    monkeypatch.setattr(STATE, "config", {"key": "k1"})

    assert transcript.history().path == real


def test_history_default_needs_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "p"))
    STATE.config = {"key": "nometa"}
    with pytest.raises(RuntimeError, match="no claude_session_id known"):
        transcript.history()
