"""Full session discovery chain (spec: session-key resolution).

Priority: explicit -> hook-runfile (process-tree walk) -> env-ptc-session ->
env-claude-session -> adapter-local (degraded). See discovery.py's module
docstring for why this walk cannot share code with the SessionStart hook's
own copy, and for the comm-basename predicate decision.
"""
import json
import os

from ptc.discovery import resolve


def _write_runfile(home, pid, sid="11111111-2222-3333-4444-555555555555", cwd="/proj"):
    rd = home / "run"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / f"claude-{pid}.json").write_text(json.dumps(
        {"session_id": sid, "cwd": cwd, "written_at": 1}))


def test_explicit_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    r = resolve(explicit="my-key", env={"PTC_SESSION": "other"})
    assert r.key == "my-key" and r.source == "explicit" and not r.degraded


def test_runfile_via_ppid(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _write_runfile(tmp_path, 777)
    r = resolve(ppid=777, env={}, proc_name=lambda pid: "claude")
    assert r.source == "hook-runfile"
    assert r.claude_session_id == "11111111-2222-3333-4444-555555555555"
    assert r.key == r.claude_session_id and r.cwd == "/proj" and not r.degraded


def test_runfile_ignored_when_ppid_not_claude_and_walks_up(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _write_runfile(tmp_path, 900)
    # ppid 800 is a shell whose parent 900 is claude
    parents = {800: 900}
    r = resolve(ppid=800, env={},
                proc_name=lambda pid: "claude" if pid == 900 else "zsh",
                proc_parent=lambda pid: parents.get(pid))
    assert r.source == "hook-runfile" and r.key == "11111111-2222-3333-4444-555555555555"


def test_env_chain_and_degraded(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    r = resolve(env={"PTC_SESSION": "childkey-1"})
    assert r.source == "env-ptc-session" and r.claude_session_id is None
    r2 = resolve(env={"CLAUDE_CODE_SESSION_ID": "abc-123"})
    assert r2.source == "env-claude-session" and r2.claude_session_id == "abc-123"
    r3 = resolve(env={})
    assert r3.source == "adapter-local" and r3.degraded and r3.key.startswith("adapter-")


def test_hop_budget_gives_up(monkeypatch, tmp_path):
    """A claude 20 hops up (past the 12-hop budget) is never reached — falls to env rungs."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    pids = [1000 + i for i in range(20)]
    parents = dict(zip(pids, pids[1:]))
    _write_runfile(tmp_path, pids[-1])
    r = resolve(ppid=pids[0], env={"PTC_SESSION": "fallback-key"},
                proc_name=lambda pid: "claude" if pid == pids[-1] else "zsh",
                proc_parent=lambda pid: parents.get(pid))
    assert r.source == "env-ptc-session" and r.key == "fallback-key"


def test_walk_stops_at_init(monkeypatch, tmp_path):
    """ppid <= 1 halts the walk without matching init as a claude ancestor."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    r = resolve(ppid=1, env={},
                proc_name=lambda pid: "claude",
                proc_parent=lambda pid: None)
    assert r.source == "adapter-local" and r.degraded


def test_wrapper_comm_not_matched_by_substring(monkeypatch, tmp_path):
    """A wrapper whose comm is plain 'node' (no 'claude' substring) is not mistaken for
    the real ancestor — the walk keeps climbing past it and falls to env rungs when
    nothing above it matches either. Named limitation: see the spec Decision Log."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    parents = {500: 400, 400: None}
    r = resolve(ppid=500, env={"PTC_SESSION": "fallback-key"},
                proc_name=lambda pid: "node",
                proc_parent=lambda pid: parents.get(pid))
    assert r.source == "env-ptc-session"


def test_explicit_uuidish_sets_claude_session_id(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    sid = "11111111-2222-3333-4444-555555555555"
    r = resolve(explicit=sid, env={})
    assert r.source == "explicit" and r.claude_session_id == sid


def test_explicit_non_uuidish_has_no_claude_session_id(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    r = resolve(explicit="my-friendly-key", env={})
    assert r.source == "explicit" and r.claude_session_id is None


def test_resolve_defaults_env_to_os_environ(monkeypatch, tmp_path):
    """env=None falls back to the real process environment (documented default)."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.delenv("PTC_SESSION", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "real-env-abc")
    r = resolve(ppid=1, proc_name=lambda pid: "", proc_parent=lambda pid: None)
    assert r.source == "env-claude-session" and r.claude_session_id == "real-env-abc"
