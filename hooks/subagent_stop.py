#!/usr/bin/env python3
"""SubagentStop hook: tell a finished subagent's kernel that its owner has stopped.

A subagent's auto-keyed kernel (`<base>--sub-<agent_id>`, see `ptc.paths.sub_key`) serves
that one run, but only its idle TTL ever ended it — an hour of holding whatever the run
loaded, observed at 5.2 GB on a machine already swapping. This is the one moment the
plugin learns the run is over. It does not kill: a stopped subagent can be continued by
SendMessage, and its namespace should still be there for a moment. It leaves a
`subagent-stopped` marker in the kernel's directory instead, and the kernel's own watchdog
(`ptc.runtime.bootstrap`) shortens its TTL to a short grace while that marker is newer
than the kernel's last activity — a cell run after it restores the normal TTL by itself.

Stdlib only, and always rc 0 with nothing on stdout: SubagentStop can BLOCK the stop it
fires for, so nothing here may ever be the reason a subagent does not finish.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

#: The id becomes part of a kernel directory name, so this is the path-safety boundary,
#: the same one `hooks/pre_tool_use.py` and `ptc.discovery` hold it to: an id that is not
#: already a plain name matches nothing and nothing is written.
_AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

#: `ptc.paths.SUB_KEY_MARK` — restated because this hook runs before, and without, the venv.
_SUB_KEY_MARK = "--sub-"

STOP_MARKER = "subagent-stopped"


def _mark() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        data = {}
    agent = data.get("agent_id")
    if not agent or not _AGENT_ID.fullmatch(str(agent)):
        return 0
    # The rule `ptc.paths.ptc_home()` and the other two hooks apply.
    raw = os.environ.get("PTC_HOME")
    home = Path(raw).expanduser().resolve() if raw else Path.home() / ".ptc"
    root = home / "kernels"
    # Matched by suffix rather than rebuilt from a base key: the base comes from whichever
    # discovery rung keyed the parent (and is digest-shortened when long), none of which
    # this hook can reproduce, while the agent id is unique and `sub_key` guarantees it
    # ends the key intact.
    suffix = f"{_SUB_KEY_MARK}{agent}"
    try:
        names = os.listdir(root)
    except OSError:
        return 0                     # no kernels at all: this subagent never used ptc
    for name in names:
        if not name.endswith(suffix):
            continue
        kd = root / name
        # Only a directory that already exists, and never through a symlink: a missing
        # directory is a kernel that is gone, and creating one would invent a key.
        if kd.is_symlink() or not kd.is_dir():
            continue
        try:
            fd = os.open(kd / STOP_MARKER,
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps({"agent_id": agent, "stopped_at": time.time()}))
        except OSError:
            continue                 # removed under us by GC: nothing left to mark
    return 0


def main() -> int:
    """Always rc 0, for anything the handling inside does not name."""
    try:
        return _mark()
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
