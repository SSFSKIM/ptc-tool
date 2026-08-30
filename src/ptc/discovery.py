"""Session-key discovery (T13) + kernel meta.json (T4).

resolve() picks the session key an MCP call or CLI invocation belongs to, in
priority order: explicit `session=` -> hook-runfile (process-tree walk to the
nearest ancestor written by the SessionStart hook) -> PTC_SESSION env ->
CLAUDE_CODE_SESSION_ID env -> adapter-local (degraded fallback, a fresh key
per adapter process). Hook-runfile outranks the env rungs because
CLAUDE_CODE_SESSION_ID is inherited at process start and can go stale across
a `--resume`, while the run-file is rewritten fresh by the hook every
SessionStart. On top of whichever rung answered sits one overlay: a call whose
`tool_use_id` the PreToolUse hook filed a caller identity against was made by a
harness subagent — those ride their parent's connection and would otherwise
share its kernel — so the resolved key gains a `--sub-<agent_id>` suffix. Every
missing or untrusted piece of that mapping falls back to the base key
unchanged, and an explicit `session=` outranks the overlay entirely.

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


#: The two ids the PreToolUse hook and this module exchange, held to the same shape on
#: both sides. `hooks/pre_tool_use.py` refuses to WRITE anything outside them; this is the
#: reading half, because what arrives here comes from the request `_meta` and from a file,
#: not from the hook's own hand: the call id is spliced into a path and the agent id into a
#: kernel directory name. Neither is sanitized into a legal one — a value that is not
#: already a plain name means "no mapping", which is the behavior with no hook at all.
_TOOL_USE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _consume_tooluse(tool_use_id: str) -> dict | None:
    """Read and DELETE the hook's mapping for one call.

    The mapping describes a single tool call, so it is spent on first read whether or not
    it parsed — a file that is garbage now is garbage on the next call too, and leaving it
    would only grow the directory. Missing, unreadable and unparseable are all one answer.
    """
    if not tool_use_id or not _TOOL_USE_ID.fullmatch(tool_use_id):
        return None
    p = run_dir() / f"tooluse-{tool_use_id}.json"
    try:
        raw = p.read_text()
    except OSError:
        return None
    finally:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def resolve(explicit: str | None = None, ppid: int | None = None, env=None,
            proc_name=_proc_name, proc_parent=_proc_parent, *,
            tool_use_id: str | None = None) -> Resolved:
    """The session key for one call, plus the subagent overlay described in the module
    docstring. `explicit` outranks the overlay and does not consume the mapping: passing
    `session=` is exactly how a subagent asks to SHARE a kernel, and the orphan it leaves
    behind is the SessionStart hook's to collect."""
    base = _base_resolve(explicit, ppid, env, proc_name, proc_parent)
    if explicit or not tool_use_id:
        return base
    mapping = _consume_tooluse(tool_use_id) or {}
    agent_id = mapping.get("agent_id")
    if not agent_id or not _AGENT_ID.fullmatch(str(agent_id)):
        return base
    return Resolved(safe_key(f"{base.key}--sub-{agent_id}"), base.source + "+subagent",
                    base.claude_session_id, base.cwd, base.degraded)


def _base_resolve(explicit: str | None = None, ppid: int | None = None, env=None,
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
