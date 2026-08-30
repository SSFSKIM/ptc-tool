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
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent.parent        # the package IS the plugin root
HOOK = PLUGIN / "hooks" / "session_start.py"
PRE_HOOK = PLUGIN / "hooks" / "pre_tool_use.py"


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


# --- r15 finding 3: the run file names a process incarnation, not just a pid -----------

def test_the_runfile_carries_an_identity_the_package_side_reads_the_same_way(tmp_path,
                                                                            monkeypatch):
    """The hook and `ptc` each read this identity with their own copy of the same rule.

    They have to: the hook runs stdlib-only under system Python before ~/.ptc/venv exists
    and cannot import `ptc`. A comparison across two copies is only meaningful while both
    answer the same thing for the same process, so that agreement is asserted here rather
    than assumed — and with it the whole point of the field, which is that `discovery`
    accepts the run file for the process that is really standing at that pid.
    """
    from ptc.discovery import resolve
    from ptc.ownership import hook_birth_identity

    m = _load_hook(monkeypatch)
    monkeypatch.setattr(m, "find_claude_ancestor", lambda: os.getpid())
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO(
        '{"session_id":"11111111-2222-3333-4444-555555555555","cwd":"/w5"}'))
    assert m.main() == 0

    written = json.loads((tmp_path / "run" / f"claude-{os.getpid()}.json").read_text())
    assert written["claude_birth"], "the run file names a pid with no incarnation behind it"
    assert written["claude_birth"] == hook_birth_identity(os.getpid()), \
        "the hook's identity read and the package's have drifted apart"

    r = resolve(ppid=os.getpid(), env={"PTC_SESSION": "fallback-key"},
                proc_name=lambda pid: "claude")
    assert r.source == "hook-runfile" and r.cwd == "/w5"


# --- the PreToolUse hook: naming the CALLER of an MCP call -----------------------------
#
# A harness subagent's tool calls arrive over the same stdio connection as its parent's, so
# `_meta` alone cannot tell the two apart and both resolve to one kernel. The hook sees what
# the server cannot — `agent_id` — and leaves it beside the call's `tool_use_id` for the
# adapter to pick up. Same discipline as the SessionStart hook: stdlib-only, always rc 0.

def _run_pre_hook(home, payload, *, text=True):
    return subprocess.run(["python3", str(PRE_HOOK)], input=payload, capture_output=True,
                          text=text, env={**os.environ, "PTC_HOME": str(home)}, timeout=20)


def test_pre_hook_records_the_subagent_behind_a_tool_call(tmp_path):
    """The mapping the adapter reads: the caller's agent identity, filed under the call's
    own tool_use_id. Owner-only, like every other PTC state file — this names who is calling
    what, and under the common 022 umask the default would be world-readable."""
    r = _run_pre_hook(tmp_path, json.dumps({
        "tool_use_id": "toolu_abc123", "agent_id": "agent_7", "agent_type": "general-purpose"}))
    assert r.returncode == 0, r.stderr

    f = tmp_path / "run" / "tooluse-toolu_abc123.json"
    written = json.loads(f.read_text())
    assert written["agent_id"] == "agent_7"
    assert written["agent_type"] == "general-purpose"
    assert isinstance(written["written_at"], float)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(f.parent.stat().st_mode) & 0o077 == 0


def test_pre_hook_records_the_directory_the_call_was_made_from(tmp_path):
    """A subagent under worktree isolation works in a checkout of its own, and this is the
    only place that directory is visible — the adapter spawns the subagent's kernel there
    instead of in the parent's cwd. A cwd that is not an absolute path is not a directory
    anyone can spawn in, so it is omitted and the parent's stands."""
    r = _run_pre_hook(tmp_path, json.dumps({
        "tool_use_id": "toolu_cwd", "agent_id": "agent_7", "cwd": "/work/tree-a"}))
    assert r.returncode == 0, r.stderr
    written = json.loads((tmp_path / "run" / "tooluse-toolu_cwd.json").read_text())
    assert written["cwd"] == "/work/tree-a"

    for bad in ("relative/dir", "", 17, None):
        r = _run_pre_hook(tmp_path, json.dumps({
            "tool_use_id": "toolu_bad", "agent_id": "agent_7", "cwd": bad}))
        assert r.returncode == 0, (bad, r.stderr)
        written = json.loads((tmp_path / "run" / "tooluse-toolu_bad.json").read_text())
        assert "cwd" not in written, bad


def test_pre_hook_writes_nothing_for_a_main_thread_call(tmp_path):
    """The main thread's calls carry no agent_id and are the common path: no mapping means
    the adapter resolves exactly as it does today, and the hook costs zero I/O to say so."""
    for payload in ({"tool_use_id": "toolu_abc123"},
                    {"tool_use_id": "toolu_abc123", "agent_id": None},
                    {"tool_use_id": "toolu_abc123", "agent_id": ""}):
        r = _run_pre_hook(tmp_path, json.dumps(payload))
        assert r.returncode == 0, r.stderr
    assert not list(tmp_path.rglob("tooluse-*.json"))


def test_pre_hook_refuses_ids_that_are_not_names(tmp_path):
    """These ids come from the host, and they become a path component on this side and a
    kernel directory name on the other. An id that is not a plain name is not sanitized into
    one — nothing is written at all, and the call keys the way it keys today."""
    for tuid in ("../evil", "toolu/abc", "toolu abc", "", "t" * 129):
        r = _run_pre_hook(tmp_path, json.dumps({"tool_use_id": tuid, "agent_id": "agent_7"}))
        assert r.returncode == 0, (tuid, r.stderr)
    for agent in ("a/../b", "agent 7", "a" * 65):
        r = _run_pre_hook(tmp_path, json.dumps({"tool_use_id": "toolu_ok", "agent_id": agent}))
        assert r.returncode == 0, (agent, r.stderr)
    assert not list(tmp_path.rglob("*.json")), "a dirty id was written somewhere"
    assert not (tmp_path.parent / "evil.json").exists()


def test_pre_hook_never_fails_a_tool_call(tmp_path):
    """PreToolUse can DENY the call it fires for, so this hook's rc is load-bearing: any
    stdin it cannot make sense of must still exit 0 and write nothing."""
    for payload in (b"", b"not json", b"{}", b"5", b"\xff\xfe not utf-8"):
        r = _run_pre_hook(tmp_path, payload, text=False)
        assert r.returncode == 0, (payload, r.stderr)
    assert not list(tmp_path.rglob("tooluse-*.json"))


def test_pre_hook_survives_unwritable_ptc_home(tmp_path):
    """PTC_HOME is a file, so run/ cannot be made — the tool call still goes through."""
    blocked = tmp_path / "blocked"
    blocked.write_text("")
    r = _run_pre_hook(blocked, json.dumps({"tool_use_id": "t1", "agent_id": "a1"}))
    assert r.returncode == 0, r.stderr


def test_pre_hook_expands_a_user_path_in_ptc_home(monkeypatch, tmp_path):
    """The third copy of the PTC_HOME rule (launcher, SessionStart hook, this one): with
    `PTC_HOME=~/.ptc-alt` a literal `~` directory means the adapter reads the expanded home
    and finds no mapping — the correlation silently never happens."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    r = subprocess.run(["python3", str(PRE_HOOK)],
                       input=json.dumps({"tool_use_id": "t2", "agent_id": "a2"}),
                       capture_output=True, text=True, timeout=20,
                       env={**os.environ, "HOME": str(tmp_path), "PTC_HOME": "~/.ptc-alt"})
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "~").exists(), "a literal '~' directory was created"
    written = json.loads((tmp_path / ".ptc-alt" / "run" / "tooluse-t2.json").read_text())
    assert written["agent_id"] == "a2"


def test_session_start_gcs_orphaned_tooluse_mappings(tmp_path, monkeypatch):
    """A mapping is consumed by the adapter call it describes — unless that call never
    reached the adapter (PreToolUse denied it, the adapter died mid-call). Those orphans
    are never read by anyone, so the next SessionStart sweeps the aged ones out; a mapping
    written moments ago may still be racing its own call and is left alone."""
    run = tmp_path / "run"
    run.mkdir(parents=True)
    old, fresh = run / "tooluse-old.json", run / "tooluse-fresh.json"
    for f in (old, fresh):
        f.write_text('{"agent_id": "agent_7"}')
    two_hours_ago = time.time() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))

    m = _load_hook(monkeypatch)
    monkeypatch.setattr(m, "find_claude_ancestor", lambda: os.getpid())
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id":"s-6","cwd":"/w6"}'))
    assert m.main() == 0

    assert not old.exists()
    assert fresh.exists()


def test_pre_hook_is_stdlib_only():
    """It runs on every ptc tool call, before ~/.ptc/venv is guaranteed to exist."""
    tree = ast.parse(PRE_HOOK.read_text())
    for node in ast.walk(tree):
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        for name in names:
            assert name.split(".")[0] in sys.stdlib_module_names, name
