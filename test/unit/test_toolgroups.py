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
    monkeypatch.setattr(toolgroups, "AGENT_COMMS", frozenset({toolgroups.comm(a.tools[0])}))
    assert not {r["pgid"] for r in toolgroups.tool_groups(a.pid)} & set(a.tools)


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


def test_freeze_tree_reads_the_tools_of_a_stopped_kernels_agents(monkeypatch):
    """`kill_process_tree`'s path: a kernel leading its group, an agent in that group."""
    script = (f"import subprocess, sys\n"
              f"a = subprocess.Popen([sys.executable, '-c', {_AGENT!r}, '2'], "
              f"stdout=subprocess.PIPE, stdin=subprocess.PIPE, text=True)\n"
              f"print(a.pid, a.stdout.readline().strip(), flush=True)\n"
              f"import time; time.sleep(300)\n")
    kern = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True,
                            start_new_session=True)
    agent_pid, *rest = map(int, kern.stdout.readline().split())
    tools = rest[:2]
    monkeypatch.setattr(toolgroups, "is_agent", lambda pid: pid == agent_pid)
    try:
        rows = toolgroups.freeze_tree(kern.pid)
        assert {r["pgid"] for r in rows} >= set(tools)
        os.killpg(kern.pid, signal.SIGKILL)
        assert set(toolgroups.reap_rows(rows)) >= set(tools)
        assert _gone_soon(tools)
    finally:
        for p in (*rest, agent_pid):
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
        try:
            os.killpg(kern.pid, signal.SIGKILL)
        except OSError:
            pass
        kern.wait()


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
