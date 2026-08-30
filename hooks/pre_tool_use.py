#!/usr/bin/env python3
"""PreToolUse hook: record which agent is making this ptc tool call.

A harness subagent runs inside the host process, so its MCP calls arrive over the SAME
stdio connection as the main thread's and the server sees nothing that tells the two
apart — they resolve to one session key and contend for one kernel. The hook does see
it: `agent_id` is present (and stable) for a subagent's calls and absent for the main
thread's. It leaves that identity beside the call's own `tool_use_id`, which the server
reads back out of the request `_meta`, and `ptc.discovery` consumes to suffix the key.

Stdlib only, and always rc 0: PreToolUse can DENY the call it fires for, so a failure
here must never be the reason a tool call does not happen.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

#: These ids come from the host, and on this side one becomes a path component while the
#: other becomes part of a kernel directory name on the far side. This is the path-safety
#: boundary for both: an id that is not already a plain name is not sanitized into one —
#: nothing is written, and the call keys exactly as it does without this hook.
_TOOL_USE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _record() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        data = {}
    tuid = data.get("tool_use_id")
    agent = data.get("agent_id")
    # No agent_id is the main thread — the common path, and it costs no I/O to say so.
    if not tuid or not agent:
        return 0
    if not (_TOOL_USE_ID.fullmatch(str(tuid)) and _AGENT_ID.fullmatch(str(agent))):
        return 0
    # The same rule `ptc.paths.ptc_home()`, `bin/ptc-launch` and the SessionStart hook
    # apply: a `~` or a relative PTC_HOME must resolve to one directory across all of
    # them, or this mapping is written where the adapter never looks.
    raw = os.environ.get("PTC_HOME")
    home = Path(raw).expanduser().resolve() if raw else Path.home() / ".ptc"
    rd = home / "run"
    rd.mkdir(parents=True, exist_ok=True)
    # This file names who is calling what, so it is owner-only like every other piece of
    # PTC state — with the common 022 umask the default would be a world-readable 0755
    # directory of 0644 files.
    try:
        os.chmod(rd, 0o700)
    except OSError:
        pass
    tmp = rd / f".tooluse-{tuid}.tmp"
    record = {"agent_id": agent, "agent_type": data.get("agent_type"),
              "written_at": time.time()}
    # A subagent can be working in a directory of its own (worktree isolation), and this is
    # the only place the adapter can learn it — otherwise its kernel spawns in the parent's
    # cwd. Unlike the ids above this is a JSON value, not a path component, so the only
    # question is whether it names a directory anything could be spawned in.
    cwd = data.get("cwd")
    if isinstance(cwd, str) and os.path.isabs(cwd):
        record["cwd"] = cwd
    payload = json.dumps(record)
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(payload)
    tmp.replace(rd / f"tooluse-{tuid}.json")
    return 0


def main() -> int:
    """Always rc 0. The belt below covers what the handling inside does not name: JSON
    that parses to a non-dict, stdin that is not UTF-8, a PTC_HOME that cannot be written."""
    try:
        return _record()
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
