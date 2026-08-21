#!/usr/bin/env python3
"""SessionStart hook: record {session_id, cwd} keyed by the nearest `claude` ancestor pid.

Hooks are launched through a shell, so os.getppid() may be a transient `sh` —
walk the ancestor chain until the command name contains "claude" (spec: keying #2).

Stdlib only: this runs before ~/.ptc/venv exists, and it must never fail a session start.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def parent_of(pid: int) -> tuple[int, str] | None:
    """(ppid, comm) of `pid` — one `ps` call serves both the walk and the name test."""
    try:
        out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        parts = out.split(None, 1)
        if len(parts) == 2:
            return int(parts[0]), parts[1]
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def find_claude_ancestor() -> int | None:
    """Nearest ancestor pid whose comm basename contains "claude", else None.

    One `ps` per hop: the (grandparent, name-of-parent) pair fetched to name this hop's
    candidate is exactly the pair the next hop needs, so it is carried forward, not
    re-queried. Bounded to 12 hops and stopped at init (ppid <= 1).

    The package-side twin of this walk lives at src/ptc/discovery.py (resolve()'s
    process-tree loop). The two cannot share a module: this hook runs stdlib-only under
    system Python, before ~/.ptc/venv exists, so it cannot `import ptc`. Keep the two
    walks' "claude" substring-on-comm-basename predicate in sync if either one changes.
    """
    info = parent_of(os.getpid())
    for _ in range(12):
        if info is None:
            return None
        ppid, _comm = info
        if ppid <= 1:
            return None
        up = parent_of(ppid)          # (grandparent pid, name of ppid itself)
        if up is None:
            return None
        if "claude" in os.path.basename(up[1]):
            return ppid
        info = up
    return None


def _record() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        data = {}
    sid = data.get("session_id")
    cwd = data.get("cwd") or os.getcwd()
    target = find_claude_ancestor()
    if not sid or not target:
        return 0
    rd = Path(os.environ.get("PTC_HOME") or (Path.home() / ".ptc")) / "run"
    rd.mkdir(parents=True, exist_ok=True)
    tmp = rd / f".claude-{target}.tmp"
    tmp.write_text(json.dumps({"session_id": sid, "cwd": cwd, "written_at": time.time()}))
    tmp.replace(rd / f"claude-{target}.json")
    for f in rd.glob("claude-*.json"):        # GC dead-pid files
        try:
            pid = int(f.stem.split("-", 1)[1])
            os.kill(pid, 0)
        except (ValueError, ProcessLookupError):
            f.unlink(missing_ok=True)
        except PermissionError:
            pass
    return 0


def main() -> int:
    """Always rc 0. The belt below is what makes "never fails a session start" true for
    the inputs the specific handling inside does not name: JSON that parses to a non-dict,
    stdin that is not UTF-8, a PTC_HOME that cannot be written."""
    try:
        return _record()
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
