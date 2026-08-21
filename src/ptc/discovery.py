"""Session-key discovery (T13) + kernel meta.json (T4).

resolve() picks the session key an MCP call or CLI invocation belongs to, in
priority order: explicit `session=` -> hook-runfile (process-tree walk to the
nearest ancestor written by the SessionStart hook) -> PTC_SESSION env ->
CLAUDE_CODE_SESSION_ID env -> adapter-local (degraded fallback, a fresh key
per adapter process). Hook-runfile outranks the env rungs because
CLAUDE_CODE_SESSION_ID is inherited at process start and can go stale across
a `--resume`, while the run-file is rewritten fresh by the hook every
SessionStart.

This module's process-tree walk (_proc_name/_proc_parent/the walk loop in
resolve) is a deliberate duplicate of find_claude_ancestor() in
plugin/hooks/session_start.py, not a shared import: the hook runs under
system Python, before ~/.ptc/venv exists and independent of this package, so
it must stay stdlib-only and cannot import `ptc`. Both walks use the same
predicate — "claude" as a substring of `ps -o comm=`'s basename — so they
resolve the same ancestor for the same process tree; see the spec Decision
Log for the substring-vs-exact-match call and its wrapper-launcher caveat.
"""
import json
import os as _os
import re
import subprocess
from dataclasses import dataclass

from .paths import kernel_dir, private_write_text, run_dir, safe_key, secure_dir

# 8+ hex/hyphen chars: good enough to tell a Claude session UUID apart from a
# human-chosen PTC_SESSION key (which is not itself a Claude session id).
_UUIDISH = re.compile(r"^[0-9a-fA-F-]{8,}$")


@dataclass
class Resolved:
    key: str
    source: str
    claude_session_id: str | None
    cwd: str | None
    degraded: bool


def _proc_name(pid: int) -> str:
    try:
        return subprocess.run(["ps", "-o", "comm=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _proc_parent(pid: int) -> int | None:
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return int(out) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _runfile_for(pid: int) -> dict | None:
    p = run_dir() / f"claude-{pid}.json"
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def resolve(explicit: str | None = None, ppid: int | None = None, env=None,
            proc_name=_proc_name, proc_parent=_proc_parent) -> Resolved:
    env = _os.environ if env is None else env
    if explicit:
        sid = explicit if _UUIDISH.match(explicit) else None
        return Resolved(safe_key(explicit), "explicit", sid, None, False)
    pid = ppid if ppid is not None else _os.getppid()
    for _ in range(12):
        if pid is None or pid <= 1:
            break
        if "claude" in _os.path.basename(proc_name(pid) or ""):
            rf = _runfile_for(pid)
            if rf and rf.get("session_id"):
                return Resolved(safe_key(rf["session_id"]), "hook-runfile",
                                rf["session_id"], rf.get("cwd"), False)
            break
        pid = proc_parent(pid)
    v = env.get("PTC_SESSION")
    if v:
        return Resolved(safe_key(v), "env-ptc-session", None, env.get("PTC_CWD"), False)
    v = env.get("CLAUDE_CODE_SESSION_ID")
    if v:
        return Resolved(safe_key(v), "env-claude-session", v, None, False)
    return Resolved(f"adapter-{_os.getpid()}", "adapter-local", None, None, True)


def write_meta(key: str, **fields) -> None:
    d = secure_dir(kernel_dir(key))
    merged = read_meta(key)
    merged.update(fields)
    private_write_text(d / "meta.json", json.dumps(merged), tmp=d / "meta.json.tmp")


def read_meta(key: str) -> dict:
    try:
        return json.loads((kernel_dir(key) / "meta.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
