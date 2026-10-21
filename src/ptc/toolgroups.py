"""The tool process groups of the kernel's `claude` children, learned while they live and
reaped when they die.

Claude Code runs every Bash tool detached: the tool's shell is a direct child of the
`claude` process and leads a process group (and a session) of its own. A `claude` that ends
gracefully kills the tool groups it still tracks; one that is SIGKILLed kills none, and its
tools are reparented to init in groups nothing of ptc's holds. That is the agent host
SIGKILLed by `_SdkHost.close`'s escalation or from outside (an OOM kill, `kill -9`), and a
CLI-route child the same way, or with the kernel's whole group (`ptc kill`, restart).

Once a `claude` is dead its tools cannot be found by parentage, so they are learned while
it lives:

  * A watcher thread in the kernel lists, every `TICK_S`, the kernel's `claude` children
    and each one's children that lead their own process group, and records every such
    group in `<kernel_dir>/agent-pgids.json` in `bgroups`' row format: the pgid with its
    leader's birth stamp, `leader_exited` once the leader is gone while the group lives on.
    A thread, not a task: a cell that blocks the event loop must not blind it.
  * When a recorded `claude` is gone, or its pid is worn by a later `claude` of ours, its
    rows are reaped under `bgroups.signalable`'s rule. The file is what `bgroups.reap`
    consumes on every kernel exit, kill and restart, so a kernel that dies with its
    children loses none of what it had learned.
  * Where ptc's own code does the killing, the `claude` is stopped (SIGSTOP) first and its
    tool groups read then, so a tool started since the last tick is not missed:
    `stop_and_kill` (the host's escalation), `stop_all_and_reap` (the kernel's TTL exit),
    `stop_group` and `tool_groups_under` (`kill_process_tree`, for `ptc kill` and restart).
    No stop is left standing: the kill follows it whatever happens in between, and a
    group `kill_process_tree` stopped but did not kill is continued (SIGCONT).

A row is a licence to SIGKILL a group long after it was read, so it is recorded on proof.
A listing of children is only a list of numbers, each of which may have exited and been
handed to an unrelated process by the time it is read. So every process is read in ONE
observation (`snapshot`: its parent, its group, its name and its birth stamp together), and
a group is recorded only where that observation shows a direct child of the `claude`
leading its own group, under a name that was read and is not `claude`'s; the birth stamp
recorded is the one read with that parentage. The parent's identity must hold across the
listing too: it is read before its children are listed and again after their reads, and
both readings must show the birth recorded for it, or a pid that ended and was reused
mid-listing would hand over a stranger's children. The `claude`s are found the same way,
as children of the kernel. A read that fails records nothing, and the next tick looks
again.

Only groups led by a `claude`'s DIRECT children are taken: a per-user daemon a tool happens
to start (tmux, gpg-agent) sits in a group of its own below the tool's shell. And never one
led by a `claude` (the engine starts its own daemon as a direct child), nor by a process
whose name could not be read, which might be one.

Residual, accepted: a kill ptc does not make reaches only what a tick saw, so a tool whose
shell had lived less than `TICK_S` when its `claude` was killed from outside keeps running,
as does a process that left its tool's group (`setsid`, a double fork). And the stop and
the kill of a `claude` go to the pid its proof was read from a moment before, as
`kill_process_tree`'s own read-then-signal does: no portable handle pins a process across
that gap.

Nothing here raises: every caller is on a path whose job is to end something cleanly.
"""
import ctypes
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple

from . import bgroups
from .ownership import (
    _BIRTH_MARK,
    _EXITED_STATES,
    _darwin_bsdinfo,
    _darwin_stamp,
    _has_exited,
    _libc,
    _proc_stat,
    _start_ticks,
    _stat_fields,
    start_time_matches,
)
from .paths import private_write_text, secure_dir

FILENAME = "agent-pgids.json"
#: How often the watcher reads the `claude` children's tools. A tick costs a few syscalls
#: per `claude` (libproc on macOS, `/proc` on Linux); it is also the residual's window.
TICK_S = 0.25
#: The watcher stops once it has had nothing to watch this long, and `watch()` restarts it.
IDLE_EXIT_S = 15.0
#: The process names taken for a Claude Code engine: the SDK's bundled CLI is `claude`.
AGENT_COMMS = frozenset({"claude"})

_ON_DARWIN = sys.platform == "darwin"
_ON_LINUX = sys.platform.startswith("linux")


# ---- reading the process table -----------------------------------------------------------

def children(pid: int) -> list[int]:
    """The direct children of `pid`, zombies included; [] when they cannot be read."""
    if _ON_DARWIN:
        return _darwin_children(pid)
    if _ON_LINUX:
        return _linux_children(pid)
    return []


def _darwin_children(pid: int) -> list[int]:
    libc = _libc()
    if libc is None:
        return []
    size = 256
    while True:
        buf = (ctypes.c_int * size)()
        try:
            # Returns the number of pids it wrote (darwin 25: 2 for two children).
            n = libc.proc_listchildpids(ctypes.c_int(pid), buf, ctypes.c_int(ctypes.sizeof(buf)))
        except (AttributeError, OSError, ValueError):
            return []
        if n < 0:
            return []
        if n < size:
            return [p for p in buf[:n] if p > 0]
        size *= 4


def _linux_children(pid: int) -> list[int]:
    """`/proc/<pid>/task/*/children` (CONFIG_PROC_CHILDREN), else a scan of every stat."""
    try:
        tids = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return []
    out: list[int] = []
    for tid in tids:
        try:
            out += [int(x) for x in Path(f"/proc/{pid}/task/{tid}/children").read_text().split()]
        except FileNotFoundError:
            return _linux_scan_children(pid)
        except (OSError, ValueError):
            continue
    return out


def _linux_scan_children(pid: int) -> list[int]:
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            stat = Path(f"/proc/{name}/stat").read_text()
        except OSError:
            continue
        rest = stat.rpartition(")")[2].split()
        if len(rest) > 1 and rest[1] == str(pid):
            out.append(int(name))
    return out


class Snapshot(NamedTuple):
    """One process as a single read of the process table saw it."""
    ppid: int
    pgid: int
    comm: str
    #: `ownership.proc_start_time`'s birth stamp for the same process, so that a row's
    #: `leader_start` taken from here is what `bgroups.signalable` compares it with later.
    birth: str


def snapshot(pid: int) -> Snapshot | None:
    """`pid`'s parent, process group, name and birth stamp, every field from ONE read, so
    that all four describe the same process: libproc's `proc_bsdinfo` on macOS,
    `/proc/<pid>/stat` on Linux. None when that read fails, finds no process, or finds one
    that has exited (libproc refuses a zombie; Linux's state field says so)."""
    if _ON_DARWIN:
        info, _ = _darwin_bsdinfo(pid)
        if info is None:
            return None
        return Snapshot(info.pbi_ppid, info.pbi_pgid, info.pbi_comm.decode(errors="replace"),
                        _BIRTH_MARK + _darwin_stamp(info))
    if _ON_LINUX:
        try:
            stat = _proc_stat(pid)
            fields = _stat_fields(stat) if stat else []
            if len(fields) < 20 or fields[0] in _EXITED_STATES:
                return None
            # Field 2, the name, is everything between the first '(' and the last ')'.
            name = stat[stat.index("(") + 1:stat.rindex(")")]
            return Snapshot(int(fields[1]), int(fields[2]), name,
                            _BIRTH_MARK + _start_ticks(stat))
        except ValueError:               # a stat that is not text, or not stat's layout
            return None
    return None


def _held_children(parent: int, born: str | None = None) -> list[tuple[int, Snapshot]]:
    """`parent`'s direct children, each with the snapshot that shows it one, or [] when
    `parent`'s identity did not hold across the read: it is read before the listing and
    again after the children's reads, and both must show one birth (`born`, when given).
    Then its pid did not change hands in between, and a child whose snapshot names it as
    the parent was its child (module docstring)."""
    before = snapshot(parent)
    if before is None or (born is not None and before.birth != born):
        return []
    kids = [(c, s) for c in children(parent)
            if (s := snapshot(c)) is not None and s.ppid == parent]
    after = snapshot(parent)
    if after is None or after.birth != before.birth:
        return []
    return kids


def is_agent(pid: int) -> bool:
    """A live Claude Code engine. A zombie is not one (it has no snapshot): its children
    are already init's."""
    s = snapshot(pid)
    return s is not None and s.comm in AGENT_COMMS


def agents_of(root: int) -> dict[int, str]:
    """`root`'s `claude` children, each with its birth stamp."""
    return {c: s.birth for c, s in _held_children(root) if is_agent(c)}


def tool_groups(agent: int, born: str | None = None) -> list[dict]:
    """A row for every process group led by one of `agent`'s direct children: its tools.
    `born` is the agent's recorded birth stamp, and nothing is returned unless it holds
    across the read. A child in someone else's group is covered by that group's own kill,
    and a `claude`, or a child whose name is empty, is never taken (module docstring)."""
    # The pgid was READ equal to the pid: a detached child leads its group by setsid, which
    # is what `bgroups` lets stand in for a birth stamp once the leader has exited.
    return [{"pgid": c, "pid": c, "leader_start": s.birth, "started_at": time.time(),
             "pgid_source": "setsid", "agent": agent}
            for c, s in _held_children(agent, born)
            if s.pgid == c and s.comm and s.comm not in AGENT_COMMS]


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _gone(pid: int, start) -> bool:
    """Has this `claude` ended? Asked of the pid itself, never read from its absence in a
    listing that may have failed: no process wears it, or only its zombie does, or one
    born at another time does. An identity that cannot be read is not a death."""
    if not bgroups._pid_exists(pid) or _has_exited(pid):
        return True
    return bool(start) and start_time_matches(pid, start) is False


def _note_leader(row: dict) -> None:
    """Mark the row `leader_exited` once no process wears the leader's pid (or only its
    zombie does): from there `bgroups`' existence gate stands in for the birth stamp."""
    if not row.get("leader_exited") and (
            not bgroups._pid_exists(row["pgid"]) or _has_exited(row["pgid"])):
        row["leader_exited"] = True


def reap_rows(rows) -> list[int]:
    """SIGKILL each row's group that `bgroups.signalable` allows. Returns the pgids
    signalled. Never the caller's own group."""
    try:
        own = os.getpgid(0)
    except OSError:
        own = None
    killed = []
    for row in rows:
        pgid = row.get("pgid")
        if not isinstance(pgid, int) or pgid <= 1 or pgid == own:
            continue
        _note_leader(row)
        if not bgroups.signalable(row):
            continue
        try:
            os.killpg(pgid, signal.SIGKILL)
            killed.append(pgid)
        except OSError:
            pass
    return killed


def reap_file(kernel_dir) -> list[int]:
    """Reap a kernel's recorded rows and drop the file (`bgroups.reap`, on every kernel
    exit, kill and restart). Returns the pgids signalled."""
    p = Path(kernel_dir) / FILENAME
    try:
        rows = json.loads(p.read_text())
    except (OSError, ValueError):
        rows = []
    killed = reap_rows([r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else [])
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass
    return killed


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def stop_group(pgid: int) -> bool:
    """SIGSTOP the process group `pgid`, for a caller about to SIGKILL it
    (`kill_process_tree`): once it is stopped no `claude` in it can start another tool, so
    `tool_groups_under` reads them exactly. True when this call stopped the group. The caller
    owes it a `resume_group` should the kill not follow, and must arm that before calling:
    an exception can land after the SIGSTOP and before this returns. A caller inside that
    group does not stop it (it would stop itself before the kill)."""
    try:
        if os.getpgid(0) == pgid:
            return False
        os.killpg(pgid, signal.SIGSTOP)
    except OSError:
        return False
    return True


def resume_group(pgid: int) -> None:
    """Undo `stop_group`: SIGCONT the group."""
    try:
        os.killpg(pgid, signal.SIGCONT)
    except OSError:
        pass


def tool_groups_under(root: int) -> list[dict]:
    """The tool groups of `root`'s `claude` children, exact once `stop_group` has stopped
    them; for a caller inside the group, which does not stop it, as current as the read."""
    rows = []
    try:
        for agent, born in agents_of(root).items():
            rows += tool_groups(agent, born)
    except Exception:                    # noqa: BLE001 — the kill must follow regardless
        pass
    return rows


# ---- the kernel's watcher ----------------------------------------------------------------

class Tracker:
    """The rows of one process's `claude` children, kept current by `tick()`."""

    def __init__(self, root: int, kernel_dir=None):
        self.root = root
        self.kernel_dir = Path(kernel_dir) if kernel_dir is not None else None
        self.lock = threading.RLock()
        #: agent pid -> {"start": its birth stamp, "groups": {pgid: row}}
        self.agents: dict[int, dict] = {}

    def empty(self) -> bool:
        with self.lock:
            return not self.agents

    def rows(self) -> list[dict]:
        with self.lock:
            return [r for a in self.agents.values() for r in a["groups"].values()]

    def tick(self) -> None:
        try:
            live = agents_of(self.root)
            with self.lock:
                changed = False
                for pid, entry in list(self.agents.items()):
                    if pid not in live and _gone(pid, entry["start"]):
                        self._reap_agent(pid)
                        changed = True
                for pid, born in live.items():
                    changed |= self._refresh(pid, born)
                if changed:
                    self._persist()
        except Exception:                # noqa: BLE001 — a bad tick must not end the watcher
            pass

    def _refresh(self, agent: int, born: str) -> bool:
        """Drop rows whose group emptied, mark exited leaders, add new tool groups. An entry
        recorded under another birth stamp is an earlier `claude` whose pid this one wears
        now: that one is dead, so its rows are reaped first."""
        changed = False
        entry = self.agents.get(agent)
        if entry is not None and entry["start"] != born:
            self._reap_agent(agent)
            entry, changed = None, True
        if entry is None:
            entry = self.agents[agent] = {"start": born, "groups": {}}
        groups = entry["groups"]
        for pgid, row in list(groups.items()):
            if not _group_alive(pgid):
                del groups[pgid]
                changed = True
                continue
            was = row.get("leader_exited")
            _note_leader(row)
            changed |= row.get("leader_exited") != was
        for row in tool_groups(agent, born):
            if row["pgid"] not in groups:
                groups[row["pgid"]] = row
                changed = True
        return changed

    def _reap_agent(self, agent: int) -> None:
        entry = self.agents.pop(agent, None)
        if entry is not None:
            reap_rows(entry["groups"].values())

    def stop_and_kill(self, agent: int) -> None:
        """SIGKILL a `claude` this process spawned and the tools it was running: stop it,
        read its tool groups (exact, since it can start no other), kill it, reap them. The
        kill follows the stop whatever comes between. `agent` must be an unreaped child,
        so that its pid is still its own."""
        try:
            _signal(agent, signal.SIGSTOP)
            with self.lock:
                s = snapshot(agent)
                if s is not None and s.ppid == self.root:
                    self._refresh(agent, s.birth)
        finally:
            _signal(agent, signal.SIGKILL)
        with self.lock:
            self._reap_agent(agent)
            self._persist()

    def stop_all_and_reap(self) -> None:
        """The kernel is exiting: stop every `claude` child, read their tool groups, reap
        them, and SIGKILL the stopped children whatever comes between (the group kill that
        follows would too; this does not leave them stopped where it does not run)."""
        agents = agents_of(self.root)
        try:
            for a in agents:
                _signal(a, signal.SIGSTOP)
            with self.lock:
                for a, born in agents.items():
                    self._refresh(a, born)
                for a in list(self.agents):
                    self._reap_agent(a)
                self._persist()
        finally:
            for a in agents:
                _signal(a, signal.SIGKILL)

    def _persist(self) -> None:
        if self.kernel_dir is None:
            return
        p = self.kernel_dir / FILENAME
        try:
            rows = self.rows()
            if rows:
                secure_dir(p.parent)
                private_write_text(p, json.dumps(rows))
            else:
                p.unlink(missing_ok=True)
        except OSError:
            pass


_LOCK = threading.Lock()
_TRACKER: Tracker | None = None
_THREAD: threading.Thread | None = None
_WANTED_UNTIL = 0.0


def tracker() -> Tracker:
    """The kernel's one tracker: its own `claude` children, persisted under its kernel dir
    (not at all outside a bootstrapped kernel, whose dir is still the placeholder)."""
    global _TRACKER
    from .runtime.state import STATE
    kd = None if str(STATE.kernel_dir) in (".", "") else Path(STATE.kernel_dir)
    with _LOCK:
        if _TRACKER is None or _TRACKER.kernel_dir != kd:
            _TRACKER = Tracker(os.getpid(), kd)
        return _TRACKER


def watch() -> None:
    """Make sure the watcher runs: called as a `claude` is about to start. Idempotent."""
    global _THREAD, _WANTED_UNTIL
    t = tracker()
    if t.kernel_dir is None:
        return                           # no kernel to watch for (a test, a script)
    with _LOCK:
        _WANTED_UNTIL = time.monotonic() + IDLE_EXIT_S
        if _THREAD is not None and _THREAD.is_alive():
            return
        _THREAD = threading.Thread(target=_run, daemon=True, name="ptc-toolgroups")
        _THREAD.start()


def _run() -> None:
    global _THREAD, _WANTED_UNTIL
    while True:
        t = tracker()
        t.tick()
        with _LOCK:
            if not t.empty():
                _WANTED_UNTIL = time.monotonic() + IDLE_EXIT_S
            elif time.monotonic() > _WANTED_UNTIL:
                _THREAD = None
                return
        time.sleep(TICK_S)


def _reset() -> None:
    """Test-only: forget the tracker; a running watcher takes up the next one."""
    global _TRACKER
    with _LOCK:
        _TRACKER = None
