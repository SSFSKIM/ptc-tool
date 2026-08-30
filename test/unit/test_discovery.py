"""Full session discovery chain (spec: session-key resolution).

Priority: explicit -> hook-runfile (process-tree walk) -> env-ptc-session ->
env-claude-session -> adapter-local (degraded). See discovery.py's module
docstring for why this walk cannot share code with the SessionStart hook's
own copy, and for the comm-basename predicate decision.
"""
import json
import os

from ptc.discovery import resolve
from ptc.paths import safe_key


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


def test_a_hex_alias_is_a_kernel_key_not_a_session_id(monkeypatch, tmp_path):
    """"Eight or more hex-or-hyphen characters" is the shape of plenty of ordinary aliases.
    Attaching under one wrote it into meta.json as a Claude session id, and `history()` and
    `agent.fork()` then resumed or searched for a session that never existed instead of
    reporting the limitation an alias-keyed kernel documents."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    for alias in ("deadbeef", "cafe-1234", "abcdef0123456789",
                  "11111111-2222-3333-4444-55555555555",     # a digit short
                  "11111111-2222-3333-4444-5555555555555"):  # a digit long
        r = resolve(explicit=alias, env={})
        assert r.claude_session_id is None, alias
        assert r.key == safe_key(alias)


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


# --- r15 finding 3: a run file belongs to a process incarnation, not to a pid ----------

def test_a_runfile_from_a_recycled_pid_is_not_this_session(monkeypatch, tmp_path):
    """The hook fails OPEN — it must never break a session start — so a new `claude` whose
    SessionStart hook did not run leaves the PREVIOUS session's run file standing under its
    own pid once the OS hands that number round. The walk accepted it on the pid and the
    comm name alone, and the new session attached to the old session's kernel: one Python
    namespace, one cell log, one agent registry for two unrelated sessions. The stamp the
    hook records beside the pid is what tells the two apart, and a file that names a
    different incarnation is not evidence about this one — discovery falls to the next rung
    rather than keying off it.
    """
    from ptc.discovery import _written_for_this_incarnation

    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _write_runfile(tmp_path, os.getpid())
    p = tmp_path / "run" / f"claude-{os.getpid()}.json"
    rf = json.loads(p.read_text())
    rf["claude_birth"] = "the process that held this pid before us"
    p.write_text(json.dumps(rf))
    assert not _written_for_this_incarnation(os.getpid(), rf)

    r = resolve(ppid=os.getpid(), env={"PTC_SESSION": "fallback-key"},
                proc_name=lambda pid: "claude")
    assert r.source == "env-ptc-session" and r.key == "fallback-key"


def test_a_runfile_naming_this_incarnation_is_accepted(monkeypatch, tmp_path):
    """The other side of the same read: a stamp that matches the process really standing
    there is the run file's own proof, and discovery keys off it as it always has."""
    from ptc.ownership import hook_birth_identity

    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _write_runfile(tmp_path, os.getpid())
    p = tmp_path / "run" / f"claude-{os.getpid()}.json"
    rf = json.loads(p.read_text())
    rf["claude_birth"] = hook_birth_identity(os.getpid())
    assert rf["claude_birth"], "this platform gave no stdlib-readable birth identity"
    p.write_text(json.dumps(rf))

    r = resolve(ppid=os.getpid(), env={"PTC_SESSION": "fallback-key"},
                proc_name=lambda pid: "claude")
    assert r.source == "hook-runfile"
    assert r.claude_session_id == "11111111-2222-3333-4444-555555555555"


def test_a_runfile_written_before_the_stamp_existed_is_still_accepted(monkeypatch,
                                                                      tmp_path):
    """Migration by absence, with no version field: a file an older PTC wrote carries no
    stamp, and rejecting it would strand every session that had not restarted yet. The hook
    rewrites the file on every SessionStart, so the window closes on its own."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _write_runfile(tmp_path, os.getpid())          # no claude_birth, as of today's files

    r = resolve(ppid=os.getpid(), env={"PTC_SESSION": "fallback-key"},
                proc_name=lambda pid: "claude")
    assert r.source == "hook-runfile"


# --- the subagent overlay: a caller of its own gets a kernel of its own ----------------
#
# Harness subagents call the server over their parent's stdio connection, so every rung
# below resolves them to the parent's key and they contend for one kernel. The PreToolUse
# hook leaves `tool_use_id -> agent_id` beside the run files; the adapter passes the id it
# read from `_meta` and this overlay suffixes whatever the base rungs resolved. Fail-open
# throughout: no mapping, or any mapping this side will not trust, is today's behavior.

def _write_mapping(home, tuid, agent_id, agent_type="general-purpose", **extra):
    rd = home / "run"
    rd.mkdir(parents=True, exist_ok=True)
    p = rd / f"tooluse-{tuid}.json"
    p.write_text(json.dumps({"agent_id": agent_id, "agent_type": agent_type,
                             "written_at": 1, **extra}))
    return p


def _base_env_resolve(env=None, **kw):
    """The env-ptc-session rung, which gives a base key that is the same on every run."""
    return resolve(ppid=1, env=env or {"PTC_SESSION": "base-key"},
                   proc_name=lambda pid: "", proc_parent=lambda pid: None, **kw)


def test_a_mapped_call_is_keyed_to_the_agent_that_made_it(monkeypatch, tmp_path):
    """Without this, the subagent's exec landed in the parent's kernel: one namespace and
    one cell log for two callers, each seeing the other's cells. The mapping is CONSUMED —
    it describes one call, and the id it is filed under is reused by nobody."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    mapping = _write_mapping(tmp_path, "toolu_1", "agent_7")

    r = _base_env_resolve(tool_use_id="toolu_1")

    assert r.key == "base-key--sub-agent_7"
    assert r.source == "env-ptc-session+subagent"
    assert r.claude_session_id is None and not r.degraded
    assert not mapping.exists(), "the mapping outlived the call it described"


def test_an_unmapped_call_resolves_exactly_as_it_did_before(monkeypatch, tmp_path):
    """The main thread writes no mapping, so its key must be byte-identical to the one the
    same call produced before the overlay existed — including when no id is passed at all."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    plain = _base_env_resolve()
    unmapped = _base_env_resolve(tool_use_id="toolu_never_written")

    assert plain.key == unmapped.key == "base-key"
    assert plain.source == unmapped.source == "env-ptc-session"


def test_an_explicit_session_outranks_the_overlay_and_leaves_the_mapping(monkeypatch,
                                                                        tmp_path):
    """`session=` is how a subagent DELIBERATELY shares its parent's kernel, so the overlay
    must not undo it. The mapping stays behind untouched — expiry is the GC's job, and this
    path has no business spending a mapping it did not use."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    mapping = _write_mapping(tmp_path, "toolu_1", "agent_7")

    r = resolve(explicit="shared-key", env={}, tool_use_id="toolu_1")

    assert r.key == "shared-key" and r.source == "explicit"
    assert mapping.exists()


def test_two_agents_on_one_session_get_two_kernels_under_one_prefix(monkeypatch, tmp_path):
    """The whole point: sibling subagents must not collide with each other either, while
    the shared prefix keeps their kernels legible as belonging to this session."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _write_mapping(tmp_path, "toolu_1", "agent_7")
    _write_mapping(tmp_path, "toolu_2", "agent_8")

    a = _base_env_resolve(tool_use_id="toolu_1")
    b = _base_env_resolve(tool_use_id="toolu_2")

    assert a.key != b.key
    assert a.key.startswith("base-key--sub-") and b.key.startswith("base-key--sub-")


def test_a_tool_use_id_that_is_not_a_name_reads_nothing(monkeypatch, tmp_path):
    """The id arrives from `_meta` and is spliced into a filename. A traversal attempt is
    not sanitized into a lookup — it resolves to the base key and touches nothing."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    decoy = tmp_path / "run" / "evil.json"
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_text("{}")
    keep = _write_mapping(tmp_path, "toolu_1", "agent_7")

    for hostile in ("../evil", "run/evil", "toolu 1", "t" * 129, ""):
        r = _base_env_resolve(tool_use_id=hostile)
        assert r.key == "base-key" and r.source == "env-ptc-session", hostile

    assert decoy.exists() and keep.exists()


def test_a_mapping_naming_an_unusable_agent_is_dropped_not_sanitized(monkeypatch, tmp_path):
    """The agent id becomes part of a kernel DIRECTORY name. A value this side will not take
    at face value falls back to the base key rather than being mapped into some near-miss
    name — but the file is still consumed, because it is spent either way."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    for tuid, agent in (("toolu_1", "a/../b"), ("toolu_2", "agent 8"), ("toolu_3", "a" * 65),
                        ("toolu_4", ""), ("toolu_5", None)):
        mapping = _write_mapping(tmp_path, tuid, agent)
        r = _base_env_resolve(tool_use_id=tuid)
        assert r.key == "base-key" and r.source == "env-ptc-session", agent
        assert not mapping.exists(), agent


def test_a_garbled_mapping_is_consumed_and_ignored(monkeypatch, tmp_path):
    """A file that does not parse is garbage either way: the call keys as it would with no
    mapping at all, and the file does not stay around to be re-read on the next call."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    rd = tmp_path / "run"
    rd.mkdir(parents=True)
    p = rd / "tooluse-toolu_1.json"
    p.write_text("{not json")

    r = _base_env_resolve(tool_use_id="toolu_1")

    assert r.key == "base-key" and r.source == "env-ptc-session"
    assert not p.exists()


def test_a_degraded_base_still_carries_the_suffix_and_stays_degraded(monkeypatch, tmp_path):
    """The overlay sits on top of whatever rung answered, adapter-local included — that is
    the rung a caller with no session id of its own lands on, and it needs the separation
    most. `degraded` is the base's fact about how the SESSION was named; a suffix does not
    repair it."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _write_mapping(tmp_path, "toolu_1", "agent_7")

    r = resolve(ppid=1, env={}, proc_name=lambda pid: "", proc_parent=lambda pid: None,
                tool_use_id="toolu_1")

    assert r.degraded and r.source == "adapter-local+subagent"
    assert r.key.startswith(f"adapter-{os.getpid()}-") and r.key.endswith("--sub-agent_7")


def test_a_subagent_kernel_is_spawned_in_the_subagents_own_directory(monkeypatch, tmp_path):
    """A subagent can be working somewhere else entirely — worktree isolation gives it its
    own checkout — and the hook records the directory the call was made from. Without it the
    kernel starts in the PARENT's cwd and every relative path the subagent writes lands in
    the wrong tree."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _write_mapping(tmp_path, "toolu_1", "agent_7", cwd="/work/tree-a")

    r = _base_env_resolve(env={"PTC_SESSION": "base-key", "PTC_CWD": "/parent/dir"},
                          tool_use_id="toolu_1")

    assert r.cwd == "/work/tree-a"


def test_a_mapping_with_no_usable_cwd_keeps_the_bases(monkeypatch, tmp_path):
    """The cwd is one more thing the hook may not have had, and it comes off a file. Absent
    or not an absolute path, the base rung's answer stands — the same fail-open the agent id
    gets, and the behavior of every caller that shares its parent's directory."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    env = {"PTC_SESSION": "base-key", "PTC_CWD": "/parent/dir"}
    for tuid, cwd in (("toolu_1", None), ("toolu_2", "relative/dir"), ("toolu_3", 17),
                      ("toolu_4", "")):
        _write_mapping(tmp_path, tuid, "agent_7", **({} if cwd is None else {"cwd": cwd}))
        r = _base_env_resolve(env=env, tool_use_id=tuid)
        assert r.cwd == "/parent/dir", cwd


def test_the_subagent_key_is_a_legal_kernel_directory_name(monkeypatch, tmp_path):
    """Everything downstream (kernel_dir, the recursive kill on restart) requires a key that
    is a single safe name, and the suffix is built from a host-supplied id."""
    from ptc.paths import kernel_dir

    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _write_mapping(tmp_path, "toolu_1", "agent_7")
    r = _base_env_resolve(tool_use_id="toolu_1")

    assert safe_key(r.key) == r.key and kernel_dir(r.key).name == r.key
