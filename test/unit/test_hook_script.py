"""The SessionStart hook's run-file contract (spec: session-key resolution #2).

The hook runs before any venv exists, so it is stdlib-only and must never fail a
session start. Its ancestor tree-walk is covered two ways: for real (pytest's own
subprocess chain is `python3 -> ... -> claude` whenever these tests run inside a
Claude Code session, which is exactly the shape the hook faces in production) and
hermetically, against synthetic chains that pin the three decisions the real chain
cannot exhibit on demand — nearest-wins, the hop budget, and the stop at init.
"""
import ast
import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent.parent        # the package IS the plugin root
HOOK = PLUGIN / "hooks" / "session_start.py"


def _load_hook(monkeypatch):
    monkeypatch.syspath_prepend(str(HOOK.parent))
    import importlib
    return importlib.import_module("session_start")


def _comm(pid: int) -> str:
    return subprocess.run(["ps", "-o", "comm=", "-p", str(pid)],
                          capture_output=True, text=True).stdout.strip()


def _stub_tree(monkeypatch, table: dict[int, tuple[int, str]]):
    """Load the hook with a synthetic ancestry: pid -> (its ppid, its own comm).

    The walk seeds itself from os.getpid(), so every table starts at this process.
    """
    m = _load_hook(monkeypatch)
    monkeypatch.setattr(m, "parent_of", lambda pid: table.get(pid))
    return m


def test_walk_returns_nearest_claude_not_outermost(monkeypatch):
    """Two `claude` in one chain (a claude that shelled out to another): the inner wins."""
    inner, outer = 900002, 900004
    m = _stub_tree(monkeypatch, {
        os.getpid(): (900001, "python3"),
        900001: (inner, "sh"),
        inner: (900003, "claude"),
        900003: (outer, "node"),
        outer: (1, "claude"),
    })
    assert m.find_claude_ancestor() == inner


def test_walk_gives_up_past_the_hop_budget(monkeypatch):
    """A `claude` 20 hops up is not found: the 12-hop bound is real, not decorative."""
    pids = [910000 + i for i in range(20)]
    table = {os.getpid(): (pids[0], "python3")}
    table.update({a: (b, "sh") for a, b in zip(pids, pids[1:])})
    table[pids[-1]] = (1, "claude")
    m = _stub_tree(monkeypatch, table)
    assert m.find_claude_ancestor() is None


def test_walk_stops_at_init(monkeypatch):
    """A chain that runs out at pid 1 yields None — the walk never keys on init itself."""
    m = _stub_tree(monkeypatch, {
        os.getpid(): (920001, "sh"),
        920001: (1, "login"),
        1: (0, "claude"),        # bait: reachable only if the `ppid <= 1` stop is dropped
    })
    assert m.find_claude_ancestor() is None


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
    runfile = tmp_path / "run" / f"claude-{os.getpid()}.json"
    f = json.loads(runfile.read_text())
    assert f["session_id"] == "s-2" and f["cwd"] == "/w2"
    assert isinstance(f["written_at"], float)
    # a run-file names the session and its working directory, and it is the channel the
    # adapter keys off: owner-only, like every other piece of PTC state
    assert stat.S_IMODE(runfile.stat().st_mode) == 0o600
    assert stat.S_IMODE(runfile.parent.stat().st_mode) & 0o077 == 0


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


def test_hook_survives_non_utf8_stdin(tmp_path):
    """Decoding happens inside json.load, so this raises before any handler names it."""
    env = {**os.environ, "PTC_HOME": str(tmp_path)}
    r = subprocess.run(["python3", str(HOOK)], input=b"\xff\xfe not utf-8",
                       capture_output=True, env=env, timeout=20)
    assert r.returncode == 0, r.stderr


def test_hook_survives_json_that_is_not_a_dict(tmp_path, monkeypatch):
    """`5` parses fine and then has no .get — rc stays 0 with a live ancestor to write to."""
    m = _load_hook(monkeypatch)
    monkeypatch.setattr(m, "find_claude_ancestor", lambda: os.getpid())
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO("5"))
    assert m.main() == 0


def test_hook_survives_unwritable_ptc_home(tmp_path, monkeypatch):
    """PTC_HOME is a file, so run/ cannot be made — the session still starts."""
    blocked = tmp_path / "blocked"
    blocked.write_text("")
    m = _load_hook(monkeypatch)
    monkeypatch.setattr(m, "find_claude_ancestor", lambda: os.getpid())
    monkeypatch.setenv("PTC_HOME", str(blocked))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id":"s-4","cwd":"/w4"}'))
    assert m.main() == 0


def test_hook_is_stdlib_only():
    """It runs before ~/.ptc/venv exists — a third-party import would break session start."""
    tree = ast.parse(HOOK.read_text())
    for node in ast.walk(tree):
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        for name in names:
            assert name.split(".")[0] in sys.stdlib_module_names, name


def test_hook_expands_a_user_path_in_ptc_home(monkeypatch, tmp_path):
    """The hook builds PTC_HOME a second time, and it built it literally: with
    `PTC_HOME=~/.ptc-alt` the run-file the adapter keys off landed in `<cwd>/~/.ptc-alt/run`
    while the adapter read the expanded home — session discovery lost its only channel."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PTC_HOME", "~/.ptc-alt")
    monkeypatch.chdir(tmp_path)
    m = _load_hook(monkeypatch)
    monkeypatch.setattr(m, "find_claude_ancestor", lambda: os.getpid())
    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"session_id": "sid-tilde", "cwd": str(tmp_path)})))

    assert m.main() == 0

    rd = tmp_path / ".ptc-alt" / "run"
    assert not (tmp_path / "~").exists(), "a literal '~' directory was created"
    written = json.loads((rd / f"claude-{os.getpid()}.json").read_text())
    assert written["session_id"] == "sid-tilde"
