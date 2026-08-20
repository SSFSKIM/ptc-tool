"""S3: does the SessionStart hook's tree-walk key match what the MCP adapter sees?

Three live scenarios against release Claude Code, driven headlessly:
  1 fresh session      2 --resume of that session      3 two concurrent sessions, one cwd

Each scenario runs one tiny turn that execs test/spikes/s3_probe.py inside the kernel and
calls the kernels tool; the probe reports the adapter's own ancestry and what T13's
run-file resolver returns for it. Raw stream-json tool_result blocks are printed verbatim,
and so are the tool_use *names* the model emitted — that is the evidence for how a
plugin-provided server's tools are actually named (`mcp__plugin_ptc_ptc__*` vs
`mcp__ptc__*`), which the spec's install snippet depends on.

Usage:  PTC_LIVE=1 uv run --group dev python test/spikes/s3_hook_discovery.py [1|2|3|4 ...]
        (scenario numbers select stages; default runs 1-3. Stages 2 and 4 need a session
        id: pass it as SESSION=<id> in the environment when running either one alone.
        Stage 4 is off by default because it KILLS the session's kernel: it re-resumes
        with the kernel gone so the adapter must respawn it, and without the env stripped,
        to read the adapter's ancestry under --resume and test keying fallback 4.)

The launching environment is stripped of CLAUDE_CODE_SESSION_ID/CLAUDECODE: without that,
a nested `claude` inherits the OUTER session's id and the adapter reads it as its own
(keying fallback 4 poisoned) — see the report. A user's terminal has neither variable.
"""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN = ROOT / "plugin"
PROBE = ROOT / "test" / "spikes" / "s3_probe.py"
SCRATCH = Path("/tmp/ptc-s3-scratch")
# Plugin-provided servers are namespaced `mcp__plugin_<plugin>_<server>__<tool>`; the
# short `mcp__ptc__*` form only exists for a directly registered server. Naming the tools
# the way an installed plugin exposes them is what makes the emitted tool_use names
# evidence rather than an echo of the prompt.
PROMPT = (f"Call the mcp__plugin_ptc_ptc__exec tool with exactly this code: "
          f"exec(open('{PROBE}').read())\n"
          f"Then call the mcp__plugin_ptc_ptc__kernels tool. Then reply with just: DONE")

CLEAN_ENV = {k: v for k, v in os.environ.items()
             if k not in ("CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}


def run_claude(label: str, extra: list[str], clean: bool = True) -> dict:
    cmd = ["claude", "-p", "--plugin-dir", str(PLUGIN),
           "--permission-mode", "bypassPermissions",
           "--output-format", "stream-json", "--verbose", *extra, PROMPT]
    p = subprocess.run(cmd, cwd=str(SCRATCH), env=CLEAN_ENV if clean else dict(os.environ),
                       capture_output=True, text=True, timeout=600)
    out = {"label": label, "session_id": None, "tool_names": [], "tool_results": [],
           "text": [], "stderr": p.stderr[-2000:], "rc": p.returncode}
    for line in p.stdout.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        out["session_id"] = msg.get("session_id") or out["session_id"]
        for block in (msg.get("message") or {}).get("content", []) or []:
            if block.get("type") == "tool_use":     # assistant turns carry the real name
                out["tool_names"].append(block.get("name"))
            elif block.get("type") == "tool_result":
                out["tool_results"].append(block.get("content"))
            elif block.get("type") == "text":
                out["text"].append(block["text"])
    return out


def snapshot(tag: str) -> None:
    rd = Path.home() / ".ptc" / "run"
    print(f"\n--- {tag}: ~/.ptc/run ---")
    for f in sorted(rd.glob("claude-*.json")) if rd.is_dir() else []:
        pid = f.stem.split("-", 1)[1]
        comm = subprocess.run(["ps", "-o", "comm=", "-p", pid],
                              capture_output=True, text=True).stdout.strip() or "<dead>"
        print(f"{f.name}  pid_comm={comm!r}  {f.read_text()}")
    print(f"--- {tag}: ~/.ptc/kernels ---")
    kr = Path.home() / ".ptc" / "kernels"
    print(sorted(p.name for p in kr.iterdir()) if kr.is_dir() else "(none)")


def show(r: dict) -> None:
    print(f"\n===== {r['label']} (rc={r['rc']}) session_id={r['session_id']} =====")
    print("TOOL_USE_NAMES:", json.dumps(r["tool_names"]))
    for tr in r["tool_results"]:
        print("TOOL_RESULT:", json.dumps(tr)[:2500])
    print("TEXT:", " ".join(r["text"])[:300])
    if r["stderr"].strip():
        print("STDERR:", r["stderr"][-600:])


def main() -> int:
    if os.environ.get("PTC_LIVE") != "1":
        print("set PTC_LIVE=1 to run this spike (it bills real quota)")
        return 1
    assert "ANTHROPIC_API_KEY" not in CLEAN_ENV, "subscription auth only"
    stages = {int(a) for a in sys.argv[1:]} or {1, 2, 3}
    SCRATCH.mkdir(exist_ok=True)
    snapshot("before")
    session = os.environ.get("SESSION")

    if 1 in stages:
        s1 = run_claude("scenario 1: fresh", [])
        show(s1)
        snapshot("after scenario 1")
        session = s1["session_id"]

    if 2 in stages and session:
        s2 = run_claude("scenario 2: resume", ["--resume", session])
        show(s2)
        snapshot("after scenario 2")

    if 3 in stages:
        results: dict[str, dict] = {}
        threads = [threading.Thread(target=lambda i=i: results.__setitem__(
            f"c{i}", run_claude(f"scenario 3: concurrent {i}", []))) for i in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for key in sorted(results):
            show(results[key])
        snapshot("after scenario 3")

    if 4 in stages and session:
        # Stage 4 answers two questions one turn can't be split across:
        #  (a) the adapter's own ancestry under --resume — visible only when the adapter
        #      has to SPAWN the kernel (a kernel adopted from an earlier session is
        #      re-parented to launchd, so the in-kernel probe cannot see the live adapter);
        #  (b) whether a `claude` launched with an inherited CLAUDE_CODE_SESSION_ID
        #      overwrites it for its children, i.e. whether keying fallback 4 can be
        #      poisoned by the outer session when claude is started from a Bash tool.
        subprocess.run([str(ROOT / ".venv" / "bin" / "python"), "-c",
                        f"import sys; sys.path.insert(0, {str(ROOT / 'src')!r});"
                        f"from ptc.kernel import kill_kernel; print(kill_kernel({session!r}))"],
                       check=False)
        s4 = run_claude("scenario 4: resume, kernel killed, env NOT stripped",
                        ["--resume", session], clean=False)
        show(s4)
        snapshot("after scenario 4")
    return 0


if __name__ == "__main__":
    sys.exit(main())
