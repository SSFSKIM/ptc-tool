"""Live checks for the Codex backend. Requires a `codex` CLI; two of the three need auth.

Three tests, deliberately asymmetric in cost:

* `test_the_fakes_hand_transcribed_enums_still_match_the_installed_schema` spends nothing
  and needs no login — `generate-json-schema` reads the binary's own types. It is the
  drift detector for the unit tier: every enum in `fake_codex_appserver.py` is a
  hand-written Python literal, so when codex bumps, a keyless suite that cannot see the
  binary would keep passing against a fake that no longer matches the server. It is gated
  on the binary alone rather than on PTC_LIVE, so it runs wherever `codex` exists.

* `test_isolation_flags_are_accepted_by_the_installed_binary` spends NOTHING. `thread/start`
  starts the thread's MCP servers but calls no model, so the seam that isolates a PTC child
  from the user's `~/.codex` can be verified for free. It guards the failure mode T22 found
  the expensive way: a config override the binary rejects makes `codex app-server` exit
  silently before it speaks a single line, which is indistinguishable from a hung server.
* `test_codex_worker_in_kernel` is the one billed turn (A15), on the user's ChatGPT
  subscription — one short prompt, one short answer, run through a real kernel.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from ptc.client import Completed, KernelClient
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config
from ptc.runtime import codex_backend
from ptc.runtime.agents import AgentOpts

live = pytest.mark.skipif(
    os.environ.get("PTC_LIVE") != "1" or shutil.which("codex") is None,
    reason="PTC_LIVE=1 and a `codex` CLI on PATH required")

#: Schema generation calls no model, so this one only needs the binary.
has_codex = pytest.mark.skipif(shutil.which("codex") is None,
                               reason="a `codex` CLI on PATH required")


def _variants(defn: dict) -> tuple[set, set]:
    """The plain string values and the single-key object variants a decision type accepts.

    Both `oneOf`-of-string-enums (the decision types) and a bare `{"type":"string","enum":
    [...]}` (SandboxMode) reduce through this.
    """
    plain, objects = set(), set()
    for variant in defn.get("oneOf") or [defn]:
        if variant.get("type") == "string":
            plain |= set(variant.get("enum") or ())
        elif variant.get("type") == "object":
            objects |= set(variant.get("required") or variant.get("properties") or ())
    return plain, objects


@has_codex
def test_the_fakes_hand_transcribed_enums_still_match_the_installed_schema(tmp_path):
    """Drift detector. The unit tier proves the client against a fake whose every enum is
    a hand-written literal; nothing keyless can notice when the real server moves."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
    import fake_codex_appserver as fk

    out = tmp_path / "schema"
    subprocess.run(["codex", "app-server", "generate-json-schema", "--out", str(out)],
                   check=True, capture_output=True, timeout=120)

    def load(name: str) -> dict:
        hits = list(out.rglob(name))
        assert hits, f"{name} is gone from the generated schema"
        return json.loads(hits[0].read_text())

    def defn(file: str, name: str) -> dict:
        return load(file)["definitions"][name]

    server_requests = [m for v in load("ServerRequest.json")["oneOf"]
                       for m in v["properties"]["method"]["enum"]]
    assert set(fk.SERVER_REQUESTS) == set(server_requests)
    assert set(fk.CREDENTIAL_REQUESTS) <= set(server_requests)
    # Everything PTC answers with a real result must still be a method the server asks.
    assert set(codex_backend._RESPONDERS) <= set(server_requests)

    for literal, file, name in [
        (fk._SANDBOX_MODES, "ThreadStartParams.json", "SandboxMode"),
        (fk._CMD_DECISIONS, "CommandExecutionRequestApprovalResponse.json",
         "CommandExecutionApprovalDecision"),
        (fk._FILE_DECISIONS, "FileChangeRequestApprovalResponse.json",
         "FileChangeApprovalDecision"),
        (fk._ELICIT_ACTIONS, "McpServerElicitationRequestResponse.json",
         "McpServerElicitationAction"),
    ]:
        plain, _ = _variants(defn(file, name))
        assert literal == plain, f"{name} drifted: fake={sorted(literal)} schema={sorted(plain)}"

    review_plain, review_objects = _variants(
        defn("ExecCommandApprovalResponse.json", "ReviewDecision"))
    assert fk._REVIEW_DECISIONS == review_plain
    assert fk._REVIEW_OBJECT_KEYS == review_objects


async def _mcp_servers(inherit: bool) -> list:
    """Names of the MCP servers a PTC-spawned thread actually starts. No turn, no billing."""
    if inherit:
        os.environ["PTC_CODEX_INHERIT"] = "1"
    else:
        os.environ.pop("PTC_CODEX_INHERIT", None)
    proc = codex_backend.CodexProc(AgentOpts())
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
