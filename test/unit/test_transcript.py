import json

import pytest

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


def test_history_default_needs_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "p"))
    STATE.config = {"key": "nometa"}
    with pytest.raises(RuntimeError, match="no claude_session_id known"):
        transcript.history()
