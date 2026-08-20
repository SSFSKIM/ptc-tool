"""S3 in-kernel probe. `exec(open(...).read())` this from an mcp__ptc__exec cell.

It runs in the kernel, whose parent is the ptc MCP adapter, whose ancestors are the
`claude` process that launched it — so from here we can see *exactly* what the adapter
sees, without instrumenting the adapter itself. Prints one JSON blob:

  chain            the pid/comm ancestry from this kernel upward
  adapter_pid      the kernel's parent = the MCP adapter process
  claude_pid       the nearest `claude` ancestor of the adapter (T13's own walk)
  resolved         what T13's run-file resolver will return for that pid, verbatim
  actual_key       the key the kernel was actually spawned under (PTC_SESSION)
  run_files        every ~/.ptc/run/claude-*.json currently on disk
"""
import json
import os
import subprocess
from pathlib import Path


def parent_of(pid):
    """(ppid, comm) of pid — the same one-call form the SessionStart hook uses."""
    try:
        out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        parts = out.split(None, 1)
        if len(parts) == 2:
            return int(parts[0]), parts[1]
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def chain_from(pid, depth=12):
    out, cur = [], pid
    for _ in range(depth):
        info = parent_of(cur)
        if info is None:
            break
        ppid, comm = info
        out.append([cur, comm])
        if ppid <= 1:
            break
        cur = ppid
    return out


def claude_ancestor_of(pid):
    """T13's adapter-side walk: nearest ancestor of `pid` whose comm contains claude."""
    cur = pid
    for _ in range(12):
        info = parent_of(cur)
        if info is None:
            return None
        ppid, _comm = info
        if ppid <= 1:
            return None
        up = parent_of(ppid)
        if up is None:
            return None
        if "claude" in os.path.basename(up[1]):
            return ppid
        cur = ppid
    return None


def resolve_via_runfile(claude_pid):
    """T13 fallback #2, written out: read the run-file keyed by the claude pid."""
    if claude_pid is None:
        return None
    f = Path(os.environ.get("PTC_HOME") or (Path.home() / ".ptc")) / "run" / f"claude-{claude_pid}.json"
    try:
        return json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def report():
    adapter = os.getppid()
    claude_pid = claude_ancestor_of(adapter)
    rd = Path(os.environ.get("PTC_HOME") or (Path.home() / ".ptc")) / "run"
    run_files = {}
    if rd.is_dir():
        for f in sorted(rd.glob("claude-*.json")):
            try:
                run_files[f.name] = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError) as e:
                run_files[f.name] = f"<unreadable: {e}>"
    resolved = resolve_via_runfile(claude_pid)
    return {
        "kernel_pid": os.getpid(),
        "adapter_pid": adapter,
        "adapter_comm": (parent_of(adapter) or (None, None))[1],
        "claude_pid": claude_pid,
        "chain": chain_from(os.getpid()),
        "resolved": resolved,
        "resolved_key": (resolved or {}).get("session_id"),
        "actual_key": os.environ.get("PTC_SESSION"),
        "cwd": os.getcwd(),
        "run_files": run_files,
        "env": {k: os.environ.get(k) for k in
                ("CLAUDE_CODE_SESSION_ID", "CLAUDE_PLUGIN_ROOT", "PTC_LAUNCHER",
                 "PTC_CWD", "PTC_HOME")},
    }


print("S3_PROBE " + json.dumps(report()))
