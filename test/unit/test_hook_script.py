"""The SessionStart hook's run-file contract (spec: session-key resolution #2).

The hook runs before any venv exists, so it is stdlib-only and must never fail a
session start. Its ancestor tree-walk is exercised for real here: pytest's own
subprocess chain is `python3 -> ... -> claude` whenever these tests run inside a
Claude Code session, which is exactly the shape the hook faces in production.
"""
import ast
import io
import json
import os
import subprocess
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent.parent / "plugin"
HOOK = PLUGIN / "hooks" / "session_start.py"


def _load_hook(monkeypatch):
    monkeypatch.syspath_prepend(str(HOOK.parent))
    import importlib
    return importlib.import_module("session_start")


def _comm(pid: int) -> str:
    return subprocess.run(["ps", "-o", "comm=", "-p", str(pid)],
                          capture_output=True, text=True).stdout.strip()


def test_hook_never_breaks_session_start(tmp_path):
    """rc 0 always; whatever it writes is keyed to a live `claude` ancestor."""
    env = {**os.environ, "PTC_HOME": str(tmp_path)}
    r = subprocess.run(["python3", str(HOOK)], input='{"session_id": "s-1", "cwd": "/w"}',
                       capture_output=True, text=True, env=env, timeout=20)
    assert r.returncode == 0, r.stderr
    written = list((tmp_path / "run").glob("claude-*.json")) if (tmp_path / "run").is_dir() else []
    for f in written:                       # tree-walk verification when run under `claude`
        pid = int(f.stem.split("-", 1)[1])
        assert "claude" in os.path.basename(_comm(pid)), f"{f} keyed to non-claude pid {pid}"
        assert json.loads(f.read_text())["session_id"] == "s-1"


def test_hook_writes_runfile_for_claude_ancestor(tmp_path, monkeypatch):
    """The run-file contract itself, with the ancestor search stubbed to a live pid."""
    m = _load_hook(monkeypatch)
    monkeypatch.setattr(m, "find_claude_ancestor", lambda: os.getpid())
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id":"s-2","cwd":"/w2"}'))
    assert m.main() == 0
    f = json.loads((tmp_path / "run" / f"claude-{os.getpid()}.json").read_text())
    assert f["session_id"] == "s-2" and f["cwd"] == "/w2"
    assert isinstance(f["written_at"], float)


def test_hook_gcs_dead_pid_runfiles(tmp_path, monkeypatch):
    """Stale files (dead pid) are garbage-collected; live ones are left alone."""
    dead = subprocess.Popen(["true"])
    dead.wait()
    run = tmp_path / "run"
    run.mkdir(parents=True)
    (run / f"claude-{dead.pid}.json").write_text('{"session_id": "gone"}')
    (run / "claude-nonsense.json").write_text("{}")
    m = _load_hook(monkeypatch)
    monkeypatch.setattr(m, "find_claude_ancestor", lambda: os.getpid())
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id":"s-3","cwd":"/w3"}'))
    assert m.main() == 0
    assert not (run / f"claude-{dead.pid}.json").exists()
    assert not (run / "claude-nonsense.json").exists()
    assert (run / f"claude-{os.getpid()}.json").exists()


def test_hook_tolerates_unparseable_stdin(tmp_path):
    env = {**os.environ, "PTC_HOME": str(tmp_path)}
    for payload in ("", "not json", "{}"):
        r = subprocess.run(["python3", str(HOOK)], input=payload,
                           capture_output=True, text=True, env=env, timeout=20)
        assert r.returncode == 0, (payload, r.stderr)
    assert not list(tmp_path.rglob("claude-*.json"))   # no session_id => nothing written


def test_hook_is_stdlib_only():
    """It runs before ~/.ptc/venv exists — a third-party import would break session start."""
    tree = ast.parse(HOOK.read_text())
    for node in ast.walk(tree):
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        for name in names:
            assert name.split(".")[0] in sys.stdlib_module_names, name
