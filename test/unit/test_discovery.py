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


def test_hook_runfile_wins_over_both_env_rungs_when_all_present(monkeypatch, tmp_path):
    """Race all three non-explicit rungs at once: a valid runfile via ppid AND both env
    vars populated. hook-runfile must win — this would fail under any reordering that
    checked env before (or instead of) completing the walk."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _write_runfile(tmp_path, 777)
    r = resolve(ppid=777, env={"PTC_SESSION": "childkey-1", "CLAUDE_CODE_SESSION_ID": "abc-123"},
                proc_name=lambda pid: "claude")
    assert r.source == "hook-runfile"
    assert r.key == "11111111-2222-3333-4444-555555555555"


def test_env_ptc_session_wins_over_env_claude_session_when_both_present(monkeypatch, tmp_path):
    """No runfile, but both env vars populated in the same call: env-ptc-session must win
    — this would fail under a swap of the PTC_SESSION/CLAUDE_CODE_SESSION_ID checks."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    r = resolve(ppid=999999, env={"PTC_SESSION": "childkey-1", "CLAUDE_CODE_SESSION_ID": "abc-123"},
                proc_name=lambda pid: "", proc_parent=lambda pid: None)
    assert r.source == "env-ptc-session" and r.key == "childkey-1"


def test_adapter_local_key_is_stable_in_a_process_but_more_than_a_pid(monkeypatch, tmp_path):
    """A detached kernel outlives its adapter by up to the TTL, so a key that is only the
    adapter's pid is a name the OS can hand out again: the next adapter to draw that pid
    attached to the previous client's namespace instead of getting the fresh adapter-local
    kernel this rung promises. The key must still be FIXED within one process — two
    resolve() calls from the same adapter are the same session."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    a = resolve(ppid=1, env={}, proc_name=lambda pid: "", proc_parent=lambda pid: None)
    b = resolve(ppid=1, env={}, proc_name=lambda pid: "", proc_parent=lambda pid: None)

    assert a.degraded and a.source == "adapter-local"
    assert a.key == b.key, "two calls in one adapter must resolve to one kernel"
    assert a.key != f"adapter-{os.getpid()}", "the bare pid is all a later adapter can reuse"
    assert a.key.startswith(f"adapter-{os.getpid()}-")
    # kernel_dir() refuses anything that is not a single safe name under the kernels root
    from ptc.paths import kernel_dir, safe_key
    assert safe_key(a.key) == a.key and kernel_dir(a.key).name == a.key


def test_the_part_of_an_adapter_key_that_is_not_the_pid_varies(tmp_path):
    """The half a same-process test cannot see: two adapter processes differ in the
    component that ISN'T the pid, so the key still names one adapter after the OS has
    handed that pid to another. (Two live processes always have different pids — only this
    component still distinguishes them once one has exited and its number come round.)"""
    import subprocess
    import sys
    src = ("import ptc.discovery as d; "
           "print(d.resolve(ppid=1, env={}, proc_name=lambda p: '', "
           "proc_parent=lambda p: None).key)")
    env = {**os.environ, "PTC_HOME": str(tmp_path)}
    keys = [subprocess.run([sys.executable, "-c", src], capture_output=True, text=True,
                           env=env, check=True).stdout.strip() for _ in range(2)]
    tails = set()
    for k in keys:
        prefix, pid, tail = k.split("-", 2)
        assert (prefix, pid.isdigit()) == ("adapter", True), k
        tails.add(tail)
    assert len(tails) == 2, f"the component that is not the pid is constant: {keys}"


def test_resolve_defaults_env_to_os_environ(monkeypatch, tmp_path):
    """env=None falls back to the real process environment (documented default)."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.delenv("PTC_SESSION", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "real-env-abc")
    r = resolve(ppid=1, proc_name=lambda pid: "", proc_parent=lambda pid: None)
    assert r.source == "env-claude-session" and r.claude_session_id == "real-env-abc"
