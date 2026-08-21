"""Live checks for the Codex backend. Requires PTC_LIVE=1 and a logged-in `codex` CLI.

Two tests, deliberately asymmetric in cost:

* `test_isolation_flags_are_accepted_by_the_installed_binary` spends NOTHING. `thread/start`
  starts the thread's MCP servers but calls no model, so the seam that isolates a PTC child
  from the user's `~/.codex` can be verified for free. It guards the failure mode T22 found
  the expensive way: a config override the binary rejects makes `codex app-server` exit
  silently before it speaks a single line, which is indistinguishable from a hung server.
* `test_codex_worker_in_kernel` is the one billed turn (A15), on the user's ChatGPT
  subscription — one short prompt, one short answer, run through a real kernel.
"""
import asyncio
import os
import shutil
import textwrap

import pytest

from ptc.client import Completed, KernelClient
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config
from ptc.runtime import codex_backend
from ptc.runtime.agents import AgentOpts

live = pytest.mark.skipif(
    os.environ.get("PTC_LIVE") != "1" or shutil.which("codex") is None,
    reason="PTC_LIVE=1 and a `codex` CLI on PATH required")


async def _mcp_servers(inherit: bool) -> list:
    """Names of the MCP servers a PTC-spawned thread actually starts. No turn, no billing."""
    if inherit:
        os.environ["PTC_CODEX_INHERIT"] = "1"
    else:
        os.environ.pop("PTC_CODEX_INHERIT", None)
    proc = codex_backend.CodexProc()
    try:
        await proc.start()
        await proc.request("thread/start", codex_backend._thread_params(AgentOpts()))
        await asyncio.sleep(8)                      # let the servers finish handshaking
        status = await proc.request("mcpServerStatus/list", {})
        return sorted(d["name"] for d in (status.get("data") or []))
    finally:
        os.environ.pop("PTC_CODEX_INHERIT", None)
        await proc.close()


@live
def test_isolation_flags_are_accepted_by_the_installed_binary():
    inherited = asyncio.run(_mcp_servers(inherit=True))
    isolated = asyncio.run(_mcp_servers(inherit=False))
    print("INHERITED:", inherited)
    print("ISOLATED:", isolated)
    # The flags must not kill the server (a rejected override exits it silently) and must
    # never widen the surface a PTC child inherits.
    assert set(isolated) <= set(inherited)


@live
def test_codex_worker_in_kernel(ptc_home):
    # Trust model: an API key would shadow the CLI's own subscription auth.
    assert "OPENAI_API_KEY" not in os.environ
    ensure_kernel("cx1", cwd=str(ptc_home))
    out = KernelClient("cx1").exec_cell(textwrap.dedent("""
        h = agent.spawn("Reply with exactly: CODEX-DONE", provider="codex", name="cx")
        r = await h.result()
        print("CODEX:", r.text.strip()[:60])
        print("THREAD:", bool(r.session_id))
        m = h.messages()[0]
        print("STATUS:", m["status"], "SERVER_REQUESTS:", m["server_requests"])
        n = m["notifications"]
        print("MCP_STARTUPS:", n.get("mcpServer/startupStatus/updated", 0),
              "HOOKS:", n.get("hook/started", 0))
        print("USAGE:", m["usage"])
        await h.close()
    """), timeout_s=600, config=Config.from_env())
    print(out.output)      # the cell prints inside the kernel; echo it for the run log
    assert isinstance(out, Completed), out
    assert "CODEX-DONE" in out.output
    assert "STATUS: completed" in out.output
    kill_kernel("cx1")
