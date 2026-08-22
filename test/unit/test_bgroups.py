"""The background-bash-group registry: crash-safe writes, tolerant reaps.

The registry is read by a process that never spawned the groups it names (a CLI, the MCP
adapter) and possibly long after they exited, so every entry is advisory: gone is normal,
malformed is possible, and the reaper's own group must never be in the blast radius.
"""
import json
import os
import signal
import subprocess
import time

from ptc import bgroups
from ptc.ownership import proc_start_time


def _dead_pgid() -> int:
    """A pgid that certainly no longer exists: its own session leader, already reaped."""
    p = subprocess.Popen(["true"], start_new_session=True)
    p.wait()
    return p.pid


def test_write_is_atomic_and_read_round_trips(tmp_path):
    bgroups.write(tmp_path, [{"pgid": 4242, "pid": 4242, "cmd": "sleep 1"}])
    assert bgroups.read(tmp_path) == [{"pgid": 4242, "pid": 4242, "cmd": "sleep 1"}]
    assert json.loads(bgroups.path_for(tmp_path).read_text())[0]["pgid"] == 4242
    assert not list(tmp_path.glob("*.tmp")), "the temp file must be renamed, not left"


def test_reap_tolerates_stale_and_malformed_entries(tmp_path):
    bgroups.write(tmp_path, [{"pgid": _dead_pgid()}, {"pgid": "nonsense"},
                             {"pgid": 0}, {"not": "a pgid"}])

    assert bgroups.reap(tmp_path) == []                 # nothing live to signal
    assert not bgroups.path_for(tmp_path).exists()      # and the file is consumed


def test_reap_never_signals_the_reapers_own_group(tmp_path):
    """A recycled pgid that lands on the caller would make this reap suicidal — it runs
    inside the CLI, the adapter, and the kernel's own watchdog.

    The row carries a verified identity on purpose: an unverifiable one is skipped before
    this guard is ever consulted, which would make the test pass without exercising it."""
    me = os.getpgid(0)
    bgroups.write(tmp_path, [{"pgid": me, "pid": me, "leader_start": proc_start_time(me)}])

    assert bgroups.reap(tmp_path) == []
    assert os.getpid() == os.getpid()                   # still here to say so


def test_reap_of_a_missing_file_is_a_no_op(tmp_path):
    assert bgroups.reap(tmp_path) == []
    assert bgroups.read(tmp_path) == []


def _live_group() -> subprocess.Popen:
    """A live process group of our own: start_new_session makes the child its own
    session and group leader, so its pid IS the pgid."""
    return subprocess.Popen(["sleep", "30"], start_new_session=True)


def test_reap_never_signals_a_group_whose_leader_identity_does_not_match(tmp_path):
    """PGID reuse is the hazard the identity exists for: a recorded group that exited
    leaves its row behind, and the OS may hand that number to unrelated same-user work.
    A row whose leader start time does not match the live leader is dropped, unsignalled.
    """
    p = _live_group()
    try:
        bgroups.write(tmp_path, [{"pgid": p.pid, "pid": p.pid,
                                  "leader_start": "Thu Jan  1 00:00:00 1970"}])
        assert bgroups.reap(tmp_path) == []
        time.sleep(0.3)
        assert p.poll() is None, "an unrelated process group was killed"
    finally:
        p.kill()
        p.wait()


def test_reap_still_signals_a_group_whose_leader_identity_matches(tmp_path):
    """The verification must not defeat the reap: our own group is still killed."""
    p = _live_group()
    try:
        bgroups.write(tmp_path, [{"pgid": p.pid, "pid": p.pid,
                                  "leader_start": proc_start_time(p.pid)}])
        assert bgroups.reap(tmp_path) == [p.pid]
        assert p.wait(timeout=5) is not None
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()


# --- r6 finding 3: a group nobody could identify is never signalled -------------------

def test_reap_never_signals_a_row_whose_leader_was_never_identified(tmp_path):
    """A row written without an identity reads as "not recycled" to `_recycled`, which is
    the same answer a VERIFIED row gets — so once its pgid is handed out again, the reap
    every kill/restart/TTL runs SIGKILLs whatever same-user group inherited the number.
    An unidentified row may be dropped; it may never be signalled."""
    p = _live_group()
    try:
        bgroups.write(tmp_path, [{"pgid": p.pid, "pid": p.pid, "leader_start": None,
                                  "unverifiable": True}])
        assert bgroups.reap(tmp_path) == []
        time.sleep(0.3)
        assert p.poll() is None, "an unidentifiable group was signalled anyway"
        assert not bgroups.path_for(tmp_path).exists(), "the row was not dropped"
    finally:
        p.kill()
        p.wait()


def test_a_row_that_lost_its_leader_after_registration_is_still_signalled(tmp_path):
    """The other half of the rule, and the one the daemonizing-background case depends on
    (r4): a row that WAS identified when it was written keeps its signal-eligibility when
    its leader later exits. Only never-identified rows are quarantined."""
    p = _live_group()
    try:
        bgroups.write(tmp_path, [{"pgid": p.pid, "pid": p.pid,
                                  "leader_start": proc_start_time(p.pid),
                                  "leader_exited": True}])
        assert bgroups.reap(tmp_path) == [p.pid]
        assert p.wait(timeout=5) is not None
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()


# --- r10 finding 2: an identity that cannot be READ is not a licence to signal ---------

def test_reap_never_signals_a_group_whose_identity_cannot_be_read(tmp_path, monkeypatch):
    """`start_time_matches` has three answers and only one of them is "still ours". None
    means the identity could not be read AT ALL — a `ps` that timed out under load, an
    EPERM on a live process that now leads this recycled pgid — and testing it with
    `is False` folded that unknown into "not recycled", so the reap SIGKILLed a group
    nothing had identified. An unreadable leader fails closed."""
    p = _live_group()
    try:
        bgroups.write(tmp_path, [{"pgid": p.pid, "pid": p.pid,
                                  "leader_start": proc_start_time(p.pid)}])
        monkeypatch.setattr(bgroups, "start_time_matches", lambda pid, rec: None)

        assert bgroups.reap(tmp_path) == []
        time.sleep(0.3)
        assert p.poll() is None, "a group whose identity could not be read was signalled"
    finally:
        p.kill()
        p.wait()


def test_an_orphaned_group_whose_leader_is_gone_is_still_reaped(tmp_path):
    """The exception the fail-closed rule must not swallow, and the reason `_retire` keeps
    the row at all: a daemonizing command leaves its descendants in the leader's group and
    then the leader exits, so its identity is unreadable forever after. POSIX will not
    recycle that pid while the group still has members, so there is no stranger to protect
    here — the unknown is the RECORDED, expected state, and `leader_exited` is what says
    so. Real processes, not a stubbed identity: the leader is really reaped first."""
    p = subprocess.Popen(["/bin/sh", "-c", "sleep 30 & exit 0"], start_new_session=True)
    p.wait()                                  # the leader is gone; the sleep is not
    try:
        os.killpg(p.pid, 0)                   # ESRCH here means the fixture, not the rule
        bgroups.write(tmp_path, [{"pgid": p.pid, "pid": p.pid,
                                  "leader_start": "btime=1.000000",
                                  "leader_exited": True}])

        assert bgroups.reap(tmp_path) == [p.pid], "the orphaned group was left running"
    finally:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass


# --- r11 finding 2: `leader_exited` is not a blank cheque ------------------------------

def test_a_leader_exited_row_whose_pgid_now_names_a_LIVE_process_is_not_signalled(
        tmp_path, monkeypatch):
    """The `leader_exited` exemption was fail-OPEN on an unreadable identity, and there is
    one state where that is wrong: the group emptied, its pgid was handed out again, and the
    NEW leader's identity read transiently fails. `None` then meant "signal it" and the reap
    SIGKILLed an unrelated same-user group.

    Existence is the gate, because on a `leader_exited` row it decides the question by
    itself: the recorded leader is DEAD, so a live process wearing that pid can only be a
    recycle. No process with that pid is the retained-orphan case — POSIX will not hand the
    number out while it is still a live group's id — and there the row keeps its exemption.
    """
    p = _live_group()
    try:
        bgroups.write(tmp_path, [{"pgid": p.pid, "pid": p.pid,
                                  "leader_start": proc_start_time(p.pid),
                                  "leader_exited": True}])
        monkeypatch.setattr(bgroups, "start_time_matches", lambda pid, rec: None)

        assert bgroups.reap(tmp_path) == []
        time.sleep(0.3)
        assert p.poll() is None, "a recycled pgid's new group was SIGKILLed"
    finally:
        p.kill()
        p.wait()


# --- r11 finding 3: a pgid known by construction is not an unidentified one ------------

def test_a_leader_exited_row_with_a_construction_known_pgid_is_signalable():
    """`unverifiable()` quarantines a row with no `leader_start`, and it must keep doing so
    for a LEGACY bare row — nothing there says the number was ever right. A row written
    after the leader exited inside registration is different in kind: `start_new_session`
    had already made the pgid the pid, so the number is right by construction and only the
    birth stamp is missing. The marker is what separates the two."""
    setsid_row = {"pgid": 4242, "pid": 4242, "leader_start": None,
                  "leader_exited": True, "pgid_source": "setsid"}
    legacy_bare = {"pgid": 4242, "pid": 4242, "leader_exited": True}

    assert not bgroups.unverifiable(setsid_row)
    assert bgroups.unverifiable(legacy_bare), "a bare row must stay quarantined"


def test_registration_marks_a_group_it_could_not_identify(tmp_path, monkeypatch):
    """`ps` failing at registration used to persist `leader_start=None` silently. The read
    is retried once, and what still cannot be resolved is written MARKED — the row stays
    (kill() and _retire still need it) but the reaper will not signal it."""
    import asyncio

    from ptc.runtime import shell
    from ptc.runtime.state import STATE

    monkeypatch.setattr(STATE, "kernel_dir", tmp_path)
    shell._LIVE.clear()
    calls = []
    monkeypatch.setattr(shell, "proc_start_time", lambda pid: calls.append(pid) or None)
    try:
        async def flow():
            h = await shell.bash("sleep 300", background=True)
            rows = bgroups.read(tmp_path)
            h.kill()
            await h.wait()
            return rows

        rows = asyncio.run(flow())
        assert len(rows) == 1 and rows[0]["unverifiable"] is True, rows
        assert len(calls) == 2, f"the identity read must be retried once, got {calls}"
        assert bgroups.unverifiable(rows[0])
    finally:
        shell._LIVE.clear()
