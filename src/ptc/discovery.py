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
hooks/session_start.py, not a shared import: the hook runs under
system Python, before ~/.ptc/venv exists and independent of this package, so
it must stay stdlib-only and cannot import `ptc`. Both walks use the same
predicate — "claude" as a substring of `ps -o comm=`'s basename — so they
resolve the same ancestor for the same process tree; see the spec Decision
Log for the substring-vs-exact-match call and its wrapper-launcher caveat.

The same two-copy structure, and the same obligation to keep the copies in
step, extends to the process IDENTITY the two sides exchange through the run
file: the hook writes it with its own stdlib reading (birth_identity) and
this module re-reads it with ownership.hook_birth_identity, which exists to
answer exactly what that copy answers.
"""
import json
import os as _os
import re
import secrets
import subprocess
from dataclasses import dataclass

from .ownership import hook_birth_identity
from .paths import kernel_dir, private_write_text, run_dir, safe_key, secure_dir

#: One nonce per adapter PROCESS, drawn once at import.
#:
#: The adapter-local rung is the only key not derived from something the client told us, and
#: its kernel outlives the adapter that made it by up to the idle TTL — so a bare pid is not
#: enough of a name. The OS recycles pids, and a later adapter that drew the same one
#: attached to the previous client's namespace instead of getting the fresh adapter-local
#: kernel this rung documents. Fixed for the life of the process (two resolve() calls in one
#: adapter must agree), distinct across processes.
_ADAPTER_NONCE = secrets.token_hex(4)

#: A Claude session id is a UUID, and only the UUID shape is read as one. "Eight or more
#: hex-or-hyphen characters" also matched a perfectly ordinary kernel alias — `deadbeef`,
#: `cafe-1234` — and an attach under that alias then wrote it into meta.json as a Claude
#: session id, sending `history()` and `agent.fork()` to resume a session that never
#: existed instead of reporting the alias-keyed limitation they document.
_UUIDISH = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


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


def _written_for_this_incarnation(pid: int, rf: dict) -> bool:
    """Is this run file about the process standing at `pid` NOW, or about its predecessor?

    A run file is named by pid, and the OS hands pids out again. The hook fails OPEN by
    contract — a session start is never broken over it — so a NEW claude whose hook did not
    run leaves the PREVIOUS session's file in place under its own pid, and the walk accepts
    it: the new session attaches to the old session's kernel and inherits its namespace, its
    cell log and its agents. Only the process's birth stamp separates the two, and the hook
    records it beside the pid when it writes the file.

    Two states are NOT rejections. A file with no stamp was written by an older PTC (run
    files are rewritten on every SessionStart, so that window ages out on its own), and a
    reading that FAILS is an unknown rather than evidence — the same rule identity gets
    everywhere else in this package. Only a stamp that is present on both sides and differs
    is a mismatch, and that is a pid this session must not key off.
    """
    recorded = rf.get("claude_birth")
    if not recorded:
        return True
    current = hook_birth_identity(pid)
    return current is None or current == recorded


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
            if (rf and rf.get("session_id")
                    and _written_for_this_incarnation(pid, rf)):
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
    return Resolved(safe_key(f"adapter-{_os.getpid()}-{_ADAPTER_NONCE}"),
                    "adapter-local", None, None, True)


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
