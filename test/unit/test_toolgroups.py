"""The tool groups of the kernel's `claude` children: learned while those live, reaped when
they die however they die (`ptc/toolgroups.py`).

Real processes throughout, since the subject is the process table. A fake agent is a
Python child that starts its "tools" the way Claude Code does, each in a session of its
own; `is_agent` is pointed at the fake agents, because only the engine is named `claude`.
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from ptc import bgroups, toolgroups
from ptc.kernel import kill_process_tree
from ptc.ownership import proc_start_time

#: The real calls, for cleanups that run while a test's monkeypatch still stands.
_KILLPG = os.killpg

#: An agent: starts each "tool" in a session of its own (Claude Code's `detached`), plus
#: one child in its own group, prints their pids, then waits on stdin to be told more.
#: `nohup` starts a shell that backgrounds a sleep in its group and exits half a second
#: later, the shape a `nohup cmd &` Bash call leaves behind.
_AGENT = r"""
import signal, subprocess, sys, time
if "ignore-term" in sys.argv:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
pids = []
for _ in range(int(sys.argv[1])):
    pids.append(subprocess.Popen(["sleep", "300"], start_new_session=True).pid)
same = subprocess.Popen(["sleep", "300"]).pid
nohup = subprocess.Popen(["/bin/sh", "-c", "sleep 300 >/dev/null 2>&1 & echo $!; sleep 0.5"],
                         start_new_session=True, stdout=subprocess.PIPE, text=True)
orphan = int(nohup.stdout.readline())
print(" ".join(map(str, pids)), same, nohup.pid, orphan, flush=True)
for line in sys.stdin:
    if line.strip() == "spawn":
        print(subprocess.Popen(["sleep", "300"], start_new_session=True).pid, flush=True)
time.sleep(300)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                         text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def _gone_soon(pids, within: float = 3.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not any(_alive(p) for p in pids):
            return True
        time.sleep(0.05)
    return False


class _Agent:
    def __init__(self, tools: int = 2, *flags: str):
        self.proc = subprocess.Popen([sys.executable, "-c", _AGENT, str(tools), *flags],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        *tools_, same, self.nohup_shell, orphan = map(int, self.proc.stdout.readline().split())
        self.tools, self.same, self.orphan = tools_, same, orphan
        self.pid = self.proc.pid

    def spawn(self) -> int:
        self.proc.stdin.write("spawn\n")
        self.proc.stdin.flush()
        return int(self.proc.stdout.readline())

    def cleanup(self) -> None:
        for p in [*self.tools, self.same, self.orphan, self.nohup_shell, self.pid]:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
        self.proc.wait()


@pytest.fixture
def agents(monkeypatch):
    made: list[_Agent] = []

    def make(*a) -> _Agent:
        ag = _Agent(*a)
        made.append(ag)
        return ag

    monkeypatch.setattr(toolgroups, "is_agent",
                        lambda pid: any(a.pid == pid and a.proc.poll() is None for a in made))
    yield make
    for a in made:
        a.cleanup()


def _wait_shell_exit(agent: _Agent) -> None:
    deadline = time.monotonic() + 3
    while _alive(agent.nohup_shell) and time.monotonic() < deadline:
        time.sleep(0.02)


def test_a_tick_records_the_detached_groups_and_a_kill_from_outside_reaps_them(agents, tmp_path):
    a = agents(2)
    t = toolgroups.Tracker(os.getpid(), tmp_path)
    t.tick()
    rows = {r["pgid"]: r for r in t.rows()}
    assert set(a.tools) <= set(rows)
    assert a.same not in rows                     # in someone else's group: not ours to take
    assert all(rows[p]["leader_start"] for p in a.tools)
    on_disk = {r["pgid"] for r in json.loads((tmp_path / toolgroups.FILENAME).read_text())}
    assert on_disk == set(rows)

    os.kill(a.pid, signal.SIGKILL)                # an OOM kill: no ptc code runs at it
    a.proc.wait()
    t.tick()
    assert _gone_soon(a.tools)
    assert t.empty()
    assert not (tmp_path / toolgroups.FILENAME).exists()


def test_a_group_whose_leader_exited_is_still_reaped(agents, tmp_path):
    """The `nohup cmd &` shape: the shell is gone, its sleep is still in the group."""
    a = agents(0)
    t = toolgroups.Tracker(os.getpid(), tmp_path)
    t.tick()                                      # sees the shell while it lived (or not)
    if a.nohup_shell not in {r["pgid"] for r in t.rows()}:
        pytest.skip("the shell exited before the tick read it (the stated residual)")
    _wait_shell_exit(a)
    t.tick()
    row = next(r for r in t.rows() if r["pgid"] == a.nohup_shell)
    assert row["leader_exited"] is True
    os.kill(a.pid, signal.SIGKILL)
    a.proc.wait()
    t.tick()
    assert _gone_soon([a.orphan])


def test_stop_and_kill_reaps_a_tool_started_after_the_last_tick(agents, tmp_path):
    a = agents(1)
    t = toolgroups.Tracker(os.getpid(), tmp_path)
    t.tick()
    late = a.spawn()                              # no tick has seen this one
    t.stop_and_kill(a.pid)
    a.proc.wait()
    assert _gone_soon([late, *a.tools])
    assert a.proc.returncode == -signal.SIGKILL


def test_a_failed_listing_is_not_read_as_the_agent_dying(agents, tmp_path, monkeypatch):
    a = agents(1)
    t = toolgroups.Tracker(os.getpid(), tmp_path)
    t.tick()
    real = toolgroups.children
    monkeypatch.setattr(toolgroups, "children",
                        lambda pid: [] if pid == os.getpid() else real(pid))
    t.tick()
    assert all(_alive(p) for p in a.tools)
    assert not t.empty()


def test_a_child_named_like_an_agent_is_never_taken(agents, monkeypatch):
    """A `claude` the engine started (its own daemon, say) is not a tool group."""
    a = agents(1)
    monkeypatch.setattr(toolgroups, "AGENT_COMMS",
                        frozenset({toolgroups.snapshot(a.tools[0]).comm}))
    assert not {r["pgid"] for r in toolgroups.tool_groups(a.pid)} & set(a.tools)


def test_a_snapshot_is_one_reading_of_one_process():
    s = toolgroups.snapshot(os.getpid())
    assert (s.ppid, s.pgid, s.birth) == (os.getppid(), os.getpgid(0), proc_start_time(os.getpid()))
    assert s.comm
    zombie = subprocess.Popen(["true"])
    deadline = time.monotonic() + 3
    while not subprocess.run(["ps", "-o", "stat=", "-p", str(zombie.pid)], capture_output=True,
                             text=True).stdout.strip().startswith("Z"):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    try:
        assert toolgroups.snapshot(zombie.pid) is None
    finally:
        zombie.wait()


def test_a_pid_that_is_not_the_agents_child_when_read_is_not_recorded(agents, tmp_path,
                                                                       monkeypatch):
    """A tool listed, then gone, its pid handed to an unrelated group leader before the
    read: that leader's group is not the agent's to record (it would be SIGKILLed with
    the agent's death)."""
    a = agents(1)
    stranger = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        real = toolgroups.children
        monkeypatch.setattr(toolgroups, "children",
                            lambda pid: [*real(pid), stranger.pid] if pid == a.pid else real(pid))
        t = toolgroups.Tracker(os.getpid(), tmp_path)
        t.tick()
        rows = {r["pgid"] for r in t.rows()}
        assert set(a.tools) <= rows
        assert stranger.pid not in rows
    finally:
        stranger.kill()
        stranger.wait()


def test_an_agent_pid_that_changes_hands_during_the_listing_gives_no_rows(agents, tmp_path,
                                                                          monkeypatch):
    """The agent died, was reaped, and its pid went to another process mid-listing: the
    children read are that process's, not the agent's."""
    a = agents(2)
    listed = []
    real_children, real_snapshot = toolgroups.children, toolgroups.snapshot

    def children(pid):
        listed.append(pid)
        return real_children(pid)

    def snapshot(pid):
        s = real_snapshot(pid)
        if pid == a.pid and a.pid in listed and s is not None:
            return s._replace(birth="btime=1.000000")
        return s
    monkeypatch.setattr(toolgroups, "children", children)
    monkeypatch.setattr(toolgroups, "snapshot", snapshot)
    t = toolgroups.Tracker(os.getpid(), tmp_path)
    t.tick()
    assert not set(a.tools) & {r["pgid"] for r in t.rows()}


def test_a_new_agent_on_a_recycled_pid_reaps_the_old_ones_tools(agents, tmp_path):
    a = agents(1)
    old_tool = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        t = toolgroups.Tracker(os.getpid(), tmp_path)
        t.agents[a.pid] = {"start": "btime=1.000000", "groups": {old_tool.pid: {
            "pgid": old_tool.pid, "pid": old_tool.pid,
            "leader_start": proc_start_time(old_tool.pid), "started_at": time.time(),
            "pgid_source": "setsid", "agent": a.pid}}}
        t.tick()
        assert _gone_soon([old_tool.pid])
        assert t.agents[a.pid]["start"] == toolgroups.snapshot(a.pid).birth
        assert set(a.tools) <= set(t.agents[a.pid]["groups"])
    finally:
        old_tool.kill()
        old_tool.wait()


def test_a_child_that_cannot_be_read_is_not_recorded(agents, tmp_path, monkeypatch):
    """Its name unread, it may be a `claude` the engine started (its daemon): not taken,
    until a later tick reads it."""
    a = agents(2)
    real = toolgroups.snapshot
    monkeypatch.setattr(toolgroups, "snapshot",
                        lambda pid: None if pid == a.tools[0] else real(pid))
    t = toolgroups.Tracker(os.getpid(), tmp_path)
    t.tick()
    rows = {r["pgid"] for r in t.rows()}
    assert a.tools[0] not in rows
    assert a.tools[1] in rows
    monkeypatch.setattr(toolgroups, "snapshot", real)
    t.tick()
    assert a.tools[0] in {r["pgid"] for r in t.rows()}


def test_bgroups_reap_consumes_the_agent_registry(agents, tmp_path):
    """`ptc kill`, restart and the TTL exit all end in `bgroups.reap(kernel_dir)`."""
    a = agents(2)
    toolgroups.Tracker(os.getpid(), tmp_path).tick()
    os.kill(a.pid, signal.SIGKILL)                # the kernel's group kill took the agent
    a.proc.wait()
    killed = bgroups.reap(tmp_path)
    assert set(a.tools) <= set(killed)
    assert _gone_soon(a.tools)
    assert not (tmp_path / toolgroups.FILENAME).exists()


def test_a_row_whose_leader_is_now_someone_else_is_spared(tmp_path):
    stranger = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        row = {"pgid": stranger.pid, "pid": stranger.pid, "leader_start": "btime=1.000000",
               "pgid_source": "setsid", "agent": 1}
        (tmp_path / toolgroups.FILENAME).write_text(json.dumps([row]))
        assert toolgroups.reap_file(tmp_path) == []
        assert stranger.poll() is None
    finally:
        stranger.kill()
        stranger.wait()


class _Kernel:
    """`kill_process_tree`'s subject: a kernel leading its group, an agent in that group."""

    def __init__(self):
        script = (f"import subprocess, sys\n"
                  f"a = subprocess.Popen([sys.executable, '-c', {_AGENT!r}, '2'], "
                  f"stdout=subprocess.PIPE, stdin=subprocess.PIPE, text=True)\n"
                  f"print(a.pid, a.stdout.readline().strip(), flush=True)\n"
                  f"import time; time.sleep(300)\n")
        self.proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                                     text=True, start_new_session=True)
        self.pid = self.proc.pid
        self.agent, *self.rest = map(int, self.proc.stdout.readline().split())
        self.tools = self.rest[:2]

    def cleanup(self) -> None:
        for p in (*self.rest, self.agent):
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
        try:
            _KILLPG(self.pid, signal.SIGKILL)
        except OSError:
            pass
        self.proc.wait()


@pytest.fixture
def kernel(monkeypatch):
    k = _Kernel()
    monkeypatch.setattr(toolgroups, "is_agent", lambda pid: pid == k.agent)
    yield k
    k.cleanup()


def _stopped(pid: int) -> bool:
    """In state T, after a moment for a SIGCONT to land."""
    deadline = time.monotonic() + 1
    while True:
        stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                              text=True).stdout.strip()
        if not stat.startswith("T") or time.monotonic() > deadline:
            return stat.startswith("T")
        time.sleep(0.05)


def test_kill_process_tree_takes_the_tools_of_the_kernels_agents(kernel):
    """`ptc kill` and restart: the group is stopped, its agent's tools read and reaped."""
    kill_process_tree(kernel.pid)
    assert _gone_soon([*kernel.tools, kernel.agent])


def test_kill_process_tree_leaves_nothing_stopped_when_its_kill_fails(kernel, monkeypatch):
    """The group SIGKILL raises, and only the kernel pid is killed: the agent it stopped
    must not stay stopped forever."""
    def killpg(pgid, sig):
        if (pgid, sig) == (kernel.pid, signal.SIGKILL):
            raise PermissionError("refused")
        return _KILLPG(pgid, sig)
    monkeypatch.setattr(os, "killpg", killpg)
    kill_process_tree(kernel.pid)
    assert not _stopped(kernel.agent)


def test_kill_process_tree_leaves_nothing_stopped_when_interrupted(kernel, monkeypatch):
    real = toolgroups.children

    def children(pid):
        if pid == kernel.pid:
            raise KeyboardInterrupt           # mid-enumeration, after the stop
        return real(pid)
    monkeypatch.setattr(toolgroups, "children", children)
    with pytest.raises(KeyboardInterrupt):
        kill_process_tree(kernel.pid)
    assert not _stopped(kernel.pid)
    assert not _stopped(kernel.agent)


def test_kill_process_tree_leaves_nothing_stopped_when_interrupted_inside_the_stop(
        kernel, monkeypatch):
    """The interrupt lands after the SIGSTOP was sent, before `stop_group` returns: the
    cleanup must already be armed, or the whole group stays stopped for good."""
    def killpg(pgid, sig):
        _KILLPG(pgid, sig)
        if (pgid, sig) == (kernel.pid, signal.SIGSTOP):
            raise KeyboardInterrupt
    monkeypatch.setattr(os, "killpg", killpg)
    with pytest.raises(KeyboardInterrupt):
        kill_process_tree(kernel.pid)
    assert not _stopped(kernel.pid)
    assert not _stopped(kernel.agent)


@pytest.mark.parametrize("path", ["stop_and_kill", "stop_all_and_reap"])
def test_the_trackers_stops_are_followed_by_their_kills_when_interrupted(
        agents, tmp_path, monkeypatch, path):
    a = agents(1)
    t = toolgroups.Tracker(os.getpid(), tmp_path)
    real = toolgroups.children

    def children(pid):
        if pid == a.pid:
            raise KeyboardInterrupt           # reading the stopped agent's tools
        return real(pid)
    monkeypatch.setattr(toolgroups, "children", children)
    with pytest.raises(KeyboardInterrupt):
        t.stop_and_kill(a.pid) if path == "stop_and_kill" else t.stop_all_and_reap()
    assert not _stopped(a.pid)
    assert a.proc.wait(timeout=3) == -signal.SIGKILL


def test_an_in_group_caller_reaps_the_tools_before_the_group_kill(kernel, monkeypatch):
    """`ptc kill` from a cell: the caller is in the kernel's group, so it does not stop
    the group, and the group kill ends the caller too. The tools must be reaped by then."""
    class CallerDied(BaseException):
        pass
    events = []
    real_getpgid, real_reap = os.getpgid, toolgroups.reap_rows

    def killpg(pgid, sig):
        if (pgid, sig) == (kernel.pid, signal.SIGKILL):
            events.append("group-kill")
            raise CallerDied                  # what the real kill does to this caller
        return _KILLPG(pgid, sig)

    def reap_rows(rows):
        killed = real_reap(rows)
        events.append(("reap", set(killed)))
        return killed
    monkeypatch.setattr(os, "getpgid", lambda pid: kernel.pid if pid == 0 else real_getpgid(pid))
    monkeypatch.setattr(os, "killpg", killpg)
    monkeypatch.setattr(toolgroups, "reap_rows", reap_rows)
    with pytest.raises(CallerDied):
        kill_process_tree(kernel.pid)
    assert [e if e == "group-kill" else e[0] for e in events] == ["reap", "group-kill"]
    assert set(kernel.tools) <= events[0][1]
    assert not _stopped(kernel.agent)         # an in-group caller never stopped it


def test_the_hosts_sigkill_escalation_takes_its_tools(agents, monkeypatch):
    """`_SdkHost.close`: a host that ignores SIGTERM is stopped, read and killed, so its
    tools go with it even though no tick ever saw them."""
    from ptc.runtime import host as host_mod
    a = agents(2, "ignore-term")
    monkeypatch.setattr(host_mod, "_TEARDOWN_S", 0.3)
    monkeypatch.setattr(toolgroups, "_TRACKER", toolgroups.Tracker(os.getpid()))

    class _Proc:
        pid = a.pid

        @property
        def returncode(self):
            return a.proc.poll()

    class _Client:
        class _transport:
            _process = _Proc()

        async def disconnect(self):
            await asyncio.sleep(30)               # stdin EOF and SIGTERM both ignored

    asyncio.run(host_mod._SdkHost(_Client()).close())
    a.proc.wait()
    assert a.proc.returncode == -signal.SIGKILL
    assert _gone_soon(a.tools)
