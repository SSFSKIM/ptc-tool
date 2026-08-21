"""The ptc CLI. Same substrate as the MCP adapter; text or --json output."""
import argparse
import json
import os
import sys

from .client import KernelClient
from .discovery import resolve as _resolve
from .kernel import ensure_kernel, kill_kernel, list_kernels, restart_kernel
from .paths import Config
from .shape import render, to_dict
from .venv import ensure_venv


def _pick_session(explicit: str | None) -> tuple[str, str | None]:
    """Returns (key, notice). Runs the full discovery chain (explicit -> hook-runfile ->
    env rungs); only when that lands on the degraded adapter-local fallback does the CLI
    add its own extra rung — reuse the newest live kernel, since a human at a terminal
    (unlike the MCP adapter) usually means the session they were just using."""
    r = _resolve(explicit, ppid=None)
    if not r.degraded:
        return r.key, None
    live = [k for k in list_kernels() if k["alive"]]
    if live:
        newest = max(live, key=lambda k: k["spawned_at"] or 0)
        return newest["key"], f"[no session given — using newest live kernel: {newest['key']}]"
    return r.key, "[no session given and no live kernel — using a fresh local key]"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ptc")
    sub = p.add_subparsers(dest="cmd", required=True)

    def com(name, **kw):
        sp = sub.add_parser(name, **kw)
        sp.add_argument("-s", "--session", default=None)
        sp.add_argument("-t", "--timeout", type=float, default=300.0)
        sp.add_argument("--json", action="store_true")
        return sp

    com("setup")
    sp = com("exec"); sp.add_argument("code")
    sp = com("wait"); sp.add_argument("cell_id", type=int); sp.add_argument("--since", type=int, default=-1)
    com("interrupt"); com("restart"); com("list"); com("kill"); com("doctor")
    a = p.parse_args(argv)

    if a.cmd == "setup":
        ensure_venv()
        print("ptc venv ready:", ensure_venv())
        return 0
    if a.cmd == "list":
        for r in list_kernels():
            print(f"{r['key']}  pid={r['pid']}  alive={r['alive']}  cwd={r['cwd']}")
        return 0
    if a.cmd == "doctor":
        import shutil
        print(json.dumps({"venv": str(ensure_venv()), "uv": shutil.which("uv"),
                          "claude": shutil.which("claude"), "codex": shutil.which("codex"),
                          "PTC_SESSION": os.environ.get("PTC_SESSION"),
                          "CLAUDE_CODE_SESSION_ID": os.environ.get("CLAUDE_CODE_SESSION_ID")}, indent=2))
        return 0

    key, notice = _pick_session(a.session)
    if notice:
        print(notice, file=sys.stderr)
    if a.cmd == "kill":
        return 0 if kill_kernel(key) else 1
    if a.cmd == "restart":
        restart_kernel(key)
        print(f"[kernel {key} restarted — namespace lost]")
        return 0
    if a.cmd == "interrupt":
        KernelClient(key).interrupt()
        print(f"[interrupt sent to {key}]")
        return 0

    cfg = Config.from_env()
    cfg.yield_s = a.timeout
    if a.cmd == "exec":
        code = sys.stdin.read() if a.code == "-" else a.code
        info = ensure_kernel(key, config=cfg)
        outcome = KernelClient(key).exec_cell(code, timeout_s=a.timeout, config=cfg)
    else:  # wait
        outcome = KernelClient(key).wait_cell(a.cell_id, timeout_s=a.timeout, since=a.since)
        info = None
    if a.json:
        print(json.dumps(to_dict(outcome, key)))
    else:
        if info is not None and info.expired_notice:
            print(f"[previous kernel expired: {info.expired_notice.strip()}]")
        print(render(outcome, key, cfg).text)
    from .client import Completed
    return 0 if not isinstance(outcome, Completed) or outcome.record.status != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
