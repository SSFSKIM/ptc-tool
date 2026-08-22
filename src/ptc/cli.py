"""The ptc CLI. Same substrate as the MCP adapter; text or --json output."""
import argparse
import json
import os
import sys

from .client import KernelClient
from .discovery import read_meta
from .discovery import resolve as _resolve
from .kernel import ensure_kernel, kill_kernel, list_kernels, restart_kernel
from .paths import Config
from .shape import render, to_dict
from .venv import ensure_venv, stamp_current, venv_python


def _pick_session(explicit: str | None) -> tuple[str, str | None, object]:
    """Returns (key, notice, resolved). Runs the full discovery chain (explicit ->
    hook-runfile -> env rungs); only when that lands on the degraded adapter-local
    fallback does the CLI add its own extra rung — reuse the newest live kernel, since a
    human at a terminal (unlike the MCP adapter) usually means the session they were just
    using. `resolved` is handed back so a caller that needs the discovered cwd / Claude
    session id (restart) does not have to walk the process tree a second time."""
    r = _resolve(explicit, ppid=None)
    if not r.degraded:
        return r.key, None, r
    live = [k for k in list_kernels() if k["alive"]]
    if live:
        newest = max(live, key=lambda k: k["spawned_at"] or 0)
        return (newest["key"],
                f"[no session given — using newest live kernel: {newest['key']}]", r)
    return r.key, "[no session given and no live kernel — using a fresh local key]", r


#: `ptc exec` on a kernel that is already running (or admitting) a cell executes NOTHING
#: and says so — but it said so with exit status 0, so shell automation, `&&` chains and CI
#: read a dropped command as a command that ran. It is its own code rather than the generic
#: 1: "your code did not run, try again" is a different instruction to the caller than "your
#: code ran and failed", and a script that retries on busy must be able to tell them apart.
EXIT_BUSY = 3

_EXIT_CODES = ("exit codes: 0 success · 1 the cell raised, the wait found no such cell, or "
               f"kill found nothing to kill · {EXIT_BUSY} the kernel was busy and the code "
               "was NOT run (nothing is ever queued — wait, interrupt, or retry)")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ptc", epilog=_EXIT_CODES)
    sub = p.add_subparsers(dest="cmd", required=True)

    def com(name, **kw):
        sp = sub.add_parser(name, **kw)
        sp.add_argument("-s", "--session", default=None)
        # No default: an omitted -t must fall through to PTC_YIELD_S (Config.from_env),
        # which a hardcoded 300.0 substituted here would overwrite on every invocation.
        sp.add_argument("-t", "--timeout", type=float, default=None,
                        help="seconds to wait before yielding (default: PTC_YIELD_S, else 300)")
        sp.add_argument("--json", action="store_true")
        return sp

    com("setup")
    sp = com("exec"); sp.add_argument("code")
    sp = com("wait"); sp.add_argument("cell_id", type=int); sp.add_argument("--since", type=int, default=-1)
    com("interrupt"); com("restart"); com("list"); com("doctor")
    sp = com("kill"); sp.add_argument("--all", action="store_true",
                                      help="kill every known kernel, not just one")
    a = p.parse_args(argv)

    if a.cmd == "setup":
        venv = ensure_venv()
        if a.json:
            print(json.dumps({"venv": str(venv)}))
        else:
            print("ptc venv ready:", venv)
        return 0
    if a.cmd == "list":
        rows = list_kernels()
        if a.json:
            print(json.dumps({"kernels": rows}))
            return 0
        for r in rows:
            print(f"{r['key']}  pid={r['pid']}  alive={r['alive']}  cwd={r['cwd']}")
        return 0
    if a.cmd == "doctor":
        # Inspection only: doctor REPORTS what setup would do and provisions nothing.
        # Calling ensure_venv() here made the diagnostic command mutate the very state it
        # was asked about — it could rebuild the venv (network, minutes) as a side effect
        # of someone asking why the venv looked wrong.
        import shutil
        ready = stamp_current()
        report = {"venv": str(venv_python()), "venv_ready": ready,
                  "setup_would": "nothing (venv is current)" if ready else
                                 "provision the venv (run: ptc setup)",
                  "uv": shutil.which("uv"),
                  "claude": shutil.which("claude"), "codex": shutil.which("codex"),
                  "PTC_SESSION": os.environ.get("PTC_SESSION"),
                  "CLAUDE_CODE_SESSION_ID": os.environ.get("CLAUDE_CODE_SESSION_ID")}
        # doctor's human form is already JSON — indented, for a person reading a terminal;
        # `--json` is the same document in the one-line machine form.
        print(json.dumps(report) if a.json else json.dumps(report, indent=2))
        return 0
    if a.cmd == "kill" and a.all:
        # ownership-verified per key: kill_kernel only signals an owner whose pid AND
        # start time still match, so a recycled pid is never killed
        killed = [r["key"] for r in list_kernels() if kill_kernel(r["key"])]
        if a.json:
            print(json.dumps({"all": True, "killed": killed}))
            return 0
        for k in killed:
            print(f"[killed {k}]")
        if not killed:
            print("(no kernels to kill)")
        return 0

    key, notice, resolved = _pick_session(a.session)
    if notice:
        print(notice, file=sys.stderr)
    if a.cmd == "kill":
        killed = kill_kernel(key)
        if a.json:
            print(json.dumps({"key": key, "killed": killed}))
        return 0 if killed else 1
    if a.cmd == "restart":
        # The respawn must land where the kernel already lived and keep the Claude session
        # id it was keyed to: passing neither respawned the kernel in whatever directory
        # the CLI happened to run from and blanked meta.json's claude_session_id, which is
        # what history() and fork() read.
        meta = read_meta(key)
        work = meta.get("cwd") or resolved.cwd
        info = restart_kernel(key, cwd=work,
                              claude_session_id=(meta.get("claude_session_id")
                                                 or resolved.claude_session_id))
        if a.json:
            print(json.dumps({"key": key, "pid": info.pid, "cwd": work,
                              "namespace_lost": True}))
        else:
            print(f"[kernel {key} restarted — namespace lost]")
        return 0
    if a.cmd == "interrupt":
        KernelClient(key).interrupt()
        if a.json:
            print(json.dumps({"key": key, "interrupted": True}))
        else:
            print(f"[interrupt sent to {key}]")
        return 0

    cfg = Config.from_env()
    if a.timeout is not None:
        cfg.yield_s = a.timeout
    if a.cmd == "exec":
        code = sys.stdin.read() if a.code == "-" else a.code
        # The discovered cwd and Claude session id travel WITH the spawn: dropping them
        # here left meta.json without the session id, and history()/agent.fork() read it
        # back from there (the restart path above learned this first).
        info = ensure_kernel(key, cwd=resolved.cwd,
                             claude_session_id=resolved.claude_session_id, config=cfg)
        outcome = KernelClient(key).exec_cell(code, timeout_s=cfg.yield_s, config=cfg)
    else:  # wait
        outcome = KernelClient(key).wait_cell(a.cell_id, timeout_s=cfg.yield_s, since=a.since)
        info = None
    if a.json:
        print(json.dumps(to_dict(outcome, key)))
    else:
        if info is not None and info.expired_notice:
            print(f"[previous kernel expired: {info.expired_notice.strip()}]")
        print(render(outcome, key, cfg).text)
    from .client import Busy, Completed, NotFound
    if isinstance(outcome, Busy):
        return EXIT_BUSY  # nothing was submitted and nothing was queued: this is not success
    if isinstance(outcome, NotFound):
        return 1          # a wait on an id this kernel never ran is a failed wait
    # A Running cell IS a submitted cell — the caller's yield budget ran out, not the work.
    return 0 if not isinstance(outcome, Completed) or outcome.record.status != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
