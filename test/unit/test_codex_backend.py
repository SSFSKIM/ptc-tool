"""Unit tests for the codex app-server backend, against a fake that is as strict as 0.146.0.

The point of the strict fake is that these tests fail the same way the real server fails.
So several of them are guard tests on the FAKE itself (a fake that accepts anything proves
nothing about the client), and one deliberately sabotages the responder table to show that
a blanket `{"decision": "accept"}` — the shape eight of the ten server→client methods
reject — is caught here rather than live.
"""
import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from ptc.runtime import codex_backend
from ptc.runtime.agents import AgentOpts, _Agent
from ptc.runtime.state import STATE

FAKE = Path(__file__).parent / "fake_codex_appserver.py"
FAKE_CMD = f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE))}"


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """Point the backend at the fake and record every message it receives.

    The trace path rides on the command line, not the environment: the backend builds its
    child's environment from an allowlist, so an env-configured fake would see nothing.
    """
    trace = tmp_path / "wire.jsonl"
    monkeypatch.setenv("CODEX_BIN", f"{FAKE_CMD} --trace {shlex.quote(str(trace))}")
    monkeypatch.delenv("PTC_CODEX_INHERIT", raising=False)
    return trace


def read_trace(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def sent(trace: Path, method: str) -> dict:
    return next(m for m in read_trace(trace) if m.get("method") == method)


def opts(**kw) -> AgentOpts:
    return AgentOpts(provider="codex", **kw)


# -- the happy paths ------------------------------------------------------
def test_run_once_roundtrip(fake):
    r = asyncio.run(codex_backend.run_once("hello codex", opts()))
    assert r.text == "echo:hello codex"
    assert r.session_id == "th-1"
    assert r.duration_ms >= 0


def test_session_send_reuses_the_thread(fake):
    async def flow():
        s = await codex_backend.open_session("first", opts())
        r1 = await s.wait_result()
        r2 = await s.send("second")
        msgs = s.messages()
        await s.close()
        return r1, r2, msgs

    r1, r2, msgs = asyncio.run(flow())
    assert (r1.text, r2.text) == ("echo:first", "echo:second")
    assert r1.session_id == r2.session_id == "th-1"
    # The per-turn record carries the evidence a live run needs: what the server asked us,
    # what notifications the thread produced, and what the turn cost.
    assert len(msgs) == 2 and msgs[0]["status"] == "completed"
    assert msgs[0]["usage"]["inputTokens"] == 42
    assert msgs[0]["notifications"]["item/completed"] == 2      # userMessage + agentMessage


def test_resume_uses_thread_resume_and_keeps_the_id(fake):
    async def flow():
        s = await codex_backend.open_session(None, opts(), resume="th-existing")
        r = await s.wait_result()
        out = await s.send("after resume")
        await s.close()
        return r, out

    r, out = asyncio.run(flow())
    assert r.text == "" and r.session_id == "th-existing"
    assert out.text == "echo:after resume"
    assert sent(fake, "thread/resume")["params"]["threadId"] == "th-existing"


def test_fork_is_refused(fake):
    with pytest.raises(NotImplementedError, match="use provider='claude'"):
        asyncio.run(codex_backend.run_once("x", opts(), fork=True))


# -- the wire shapes S4 pinned --------------------------------------------
def test_handshake_is_two_messages_with_a_versioned_clientinfo(fake):
    asyncio.run(codex_backend.run_once("hi", opts()))
    msgs = read_trace(fake)
    methods = [m.get("method") for m in msgs]
    assert methods[0] == "initialize" and methods[1] == "initialized"
    assert methods.index("initialized") < methods.index("thread/start")
    ci = msgs[0]["params"]["clientInfo"]
    assert ci["name"] == "ptc" and ci["version"]
    assert "id" not in msgs[1], "`initialized` is a notification, not a request"


def test_thread_start_carries_s4s_promoted_params(fake):
    asyncio.run(codex_backend.run_once("hi", opts(model="gpt-5.1-codex", effort="low")))
    p = sent(fake, "thread/start")["params"]
    assert p["approvalPolicy"] == "never"
    # kebab-case SandboxMode, NOT the camelCase SandboxPolicy tag responses use.
    assert p["sandbox"] == "read-only"
    assert p["model"] == "gpt-5.1-codex"
    assert p["cwd"]
    # `effort` is a turn/start field; on thread/start 0.146.0 ignores it silently, so a
    # misplaced copy would be an invisible no-op rather than an error.
    assert "effort" not in p


def test_effort_and_output_schema_ride_on_turn_start(fake):
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    asyncio.run(codex_backend.run_once("hi", opts(effort="high", output_schema=schema)))
    p = sent(fake, "turn/start")["params"]
    assert p["input"] == [{"type": "text", "text": "hi"}]
    assert p["effort"] == "high"
    assert p["outputSchema"] == schema


def test_structured_output_is_the_final_text_only_when_it_really_is_json(fake):
    schema = {"type": "object"}
    r = asyncio.run(codex_backend.run_once('{"ok": 1}', opts(output_schema=schema)))
    assert r.text == 'echo:{"ok": 1}' and r.structured is None   # nothing is invented
    assert codex_backend._structured('{"ok": 1}', opts(output_schema=schema)) == {"ok": 1}
    assert codex_backend._structured('{"ok": 1}', opts()) is None  # no schema, no parsing


def test_a_line_past_asyncios_default_limit_still_arrives(fake):
    """Regression: the first live run died on asyncio's 64 KiB StreamReader limit."""
    r = asyncio.run(codex_backend.run_once("HUGE", opts()))
    assert len(r.text) > 100_000 and r.text.startswith("echo:H")


def test_isolation_flags_are_on_by_default(monkeypatch):
    monkeypatch.delenv("CODEX_BIN", raising=False)
    monkeypatch.delenv("PTC_CODEX_INHERIT", raising=False)
    assert codex_backend._argv() == ["codex", "app-server",
                                     "--disable", "hooks", "--disable", "plugins"]


def test_ptc_codex_inherit_turns_the_isolation_flags_off(monkeypatch):
    monkeypatch.delenv("CODEX_BIN", raising=False)
    monkeypatch.setenv("PTC_CODEX_INHERIT", "1")
    assert codex_backend._argv() == ["codex", "app-server"]


def test_a_multi_token_codex_bin_keeps_its_own_args_and_still_gets_isolated(monkeypatch):
    """`_argv()` appends the isolation flags rather than special-casing a one-token
    CODEX_BIN, so the arg PLACEMENT on a real multi-token command line is the case worth
    pinning — with the flags on, which is the shipped default."""
    monkeypatch.setenv("CODEX_BIN", "/opt/codex app-server --listen stdio://")
    monkeypatch.delenv("PTC_CODEX_INHERIT", raising=False)
    assert codex_backend._argv() == ["/opt/codex", "app-server", "--listen", "stdio://",
                                     "--disable", "hooks", "--disable", "plugins"]
    monkeypatch.setenv("PTC_CODEX_INHERIT", "1")
    assert codex_backend._argv() == ["/opt/codex", "app-server", "--listen", "stdio://"]


# -- the option set AgentOpts promises both providers ----------------------
def test_system_rides_as_developer_instructions_on_the_thread(fake):
    """`baseInstructions` would REPLACE codex's own core prompt; `developerInstructions`
    is injected as a separate message. A caller's system prompt is the second kind."""
    asyncio.run(codex_backend.run_once("hi", opts(system="be terse")))
    p = sent(fake, "thread/start")["params"]
    assert p["developerInstructions"] == "be terse"
    assert "baseInstructions" not in p


def test_system_survives_a_resume_too(fake):
    async def flow():
        s = await codex_backend.open_session(None, opts(system="be terse"),
                                             resume="th-existing")
        await s.close()

    asyncio.run(flow())
    assert sent(fake, "thread/resume")["params"]["developerInstructions"] == "be terse"


@pytest.mark.parametrize("kw", [{"allowed_tools": ["Bash"]}, {"max_turns": 3},
                                {"permission_mode": "acceptEdits"}])
def test_options_this_backend_cannot_honour_are_refused_not_dropped(fake, kw):
    """An option accepted and then silently discarded is a no-op no caller can see — the
    same failure T22 refused to ship for a misplaced `effort`."""
    name = next(iter(kw))
    with pytest.raises(TypeError, match=name):
        asyncio.run(codex_backend.run_once("hi", opts(**kw)))


def test_the_default_permission_mode_is_not_treated_as_a_request(fake):
    """`permission_mode` has a default every caller carries whether they meant it or not,
    so only an explicit non-default value may raise."""
    assert asyncio.run(codex_backend.run_once("hi", opts())).text == "echo:hi"


# -- the child environment -------------------------------------------------
def _secretish(name: str, value: str) -> bool:
    """The spec's redaction predicate (Trust model + T18 Decision Log), restated as the
    assertion this test makes about the environment the codex child is handed."""
    return (any(w in name.upper() for w in ("KEY", "TOKEN", "BEARER", "SECRET"))
            or value.startswith("sk-ant-"))


def test_the_codex_child_env_is_built_not_inherited(fake, monkeypatch, tmp_path):
    """The kernel's environment comes verbatim from the MCP adapter and carries Claude-side
    credentials. Another vendor's binary — and every MCP server it starts inside PTC's
    thread — must not see them, and PTC's own guards must be rewritten on the way."""
    monkeypatch.setenv("CLAUDE_ROUTINE_BEARER", "sk-ant-oat01-not-a-real-token")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-secret")
    monkeypatch.setattr(STATE, "config", {"key": "parent-key", "depth": 0, "max_depth": 1})
    seen: dict = {}
    real = asyncio.create_subprocess_exec

    async def spy(*argv, **kw):
        seen.update(kw["env"])
        return await real(*argv, **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    # The turn still completes, so the built environment is genuinely enough to run on.
    assert asyncio.run(codex_backend.run_once("hi", opts(cwd=str(tmp_path)))).text == "echo:hi"

    assert seen["PATH"] == os.environ["PATH"] and seen["HOME"] == os.environ["HOME"]
    assert not [n for n in seen if n.startswith(("CLAUDE_", "ANTHROPIC_"))]
    assert not [n for n, v in seen.items() if _secretish(n, v)]
    # PTC's guards travel rewritten, exactly as they do for a claude child: a codex child
    # inheriting the parent's key would attach to the parent's kernel at the parent's depth.
    assert seen["PTC_SESSION"].startswith("parent-key-a") and seen["PTC_SESSION"] != "parent-key"
    assert seen["PTC_DEPTH"] == "1" and seen["PTC_MAX_DEPTH"] == "1"
    assert seen["PTC_CWD"] == str(tmp_path)


# -- the server→client responder ------------------------------------------
@pytest.mark.parametrize("method", codex_backend._RESPONDERS)
def test_every_serviceable_request_gets_a_reply_the_server_accepts(fake, method):
    r = asyncio.run(codex_backend.run_once(f"REQ:{method}", opts()))
    assert r.text.startswith("echo:")      # the fake only completes on a valid reply


@pytest.mark.parametrize("method", ["attestation/generate",
                                    "account/chatgptAuthTokens/refresh"])
def test_credential_requests_get_a_json_rpc_error_not_an_empty_result(fake, method):
    r = asyncio.run(codex_backend.run_once(f"REQ:{method}", opts()))
    assert r.text.startswith("echo:")
    reply = next(m for m in read_trace(fake) if "method" not in m and m.get("id", 0) > 900)
    assert "error" in reply and "result" not in reply
    assert reply["error"]["code"] == -32601


def test_blanket_accept_is_caught_by_the_fake(fake, monkeypatch):
    """Sabotage: the bug this whole responder table exists to prevent must fail here."""
    monkeypatch.setattr(codex_backend, "_RESPONDERS",
                        {m: (lambda p: {"decision": "accept"})
                         for m in codex_backend._RESPONDERS})
    with pytest.raises(RuntimeError, match="bad ReviewDecision"):
        asyncio.run(codex_backend.run_once("REQ:execCommandApproval", opts()))


def test_permission_escalation_is_denied_not_echoed(fake):
    asyncio.run(codex_backend.run_once("REQ:item/permissions/requestApproval", opts()))
    reply = next(m for m in read_trace(fake) if "method" not in m and m.get("id", 0) > 900)
    assert reply["result"] == {"permissions": {}, "scope": "turn"}


def test_server_requests_are_recorded_on_the_session(fake):
    async def flow():
        s = await codex_backend.open_session("REQ:mcpServer/elicitation/request", opts())
        await s.wait_result()
        msgs = s.messages()
        await s.close()
        return msgs

    msgs = asyncio.run(flow())
    assert msgs[0]["server_requests"] == ["mcpServer/elicitation/request"]


# -- interrupt, teardown, failure -----------------------------------------
def test_interrupt_carries_the_turn_id_and_ends_the_turn(fake):
    async def flow():
        s = await codex_backend.open_session("SLOW one", opts())
        turn = asyncio.ensure_future(s.wait_result())
        for _ in range(200):               # wait for the turn/start response, not a guess
            if s._proc.turn_id:
                break
            await asyncio.sleep(0.01)
        await s.interrupt()
        r = await asyncio.wait_for(turn, 10)
        msgs = s.messages()
        await s.close()
        return r, msgs

    r, msgs = asyncio.run(flow())
    assert r.text == ""                    # interrupted before any agentMessage landed
    assert msgs[0]["status"] == "interrupted"
    p = sent(fake, "turn/interrupt")["params"]
    assert p["threadId"] == "th-1" and p["turnId"] == "tu-1"


def test_close_leaves_no_codex_process_behind(fake):
    async def flow():
        s = await codex_backend.open_session("hi", opts())
        await s.wait_result()
        child = s._proc._proc
        await s.close()
        return child.pid, child.returncode

    pid, returncode = asyncio.run(flow())
    assert returncode is not None, "close() must wait for the child, not just signal it"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)                    # reaped: signalling it must fail


def test_close_mid_turn_settles_the_pending_turn(fake):
    """close() cancels the stdout reader, so after it nothing will ever answer the turn.

    Unless close() settles the pending futures itself, `wait_result()` sits out the whole
    30-minute `_TURN_S` budget with nobody left to end it.
    """
    async def flow():
        s = await codex_backend.open_session("SLOW one", opts())
        turn = asyncio.ensure_future(s.wait_result())
        for _ in range(200):               # wait for the turn/start response, not a guess
            if s._proc.turn_id:
                break
            await asyncio.sleep(0.01)
        await s.close()
        with pytest.raises(RuntimeError, match="closed"):
            await asyncio.wait_for(turn, 5)

    asyncio.run(flow())


def test_close_mid_turn_releases_the_concurrency_permit(fake, tmp_path):
    """The consequence one layer up: `AgentHandle.close()` while `result()` is outstanding
    must not strand the turn holding its `max_concurrency` permit — eight stranded permits
    wedge the whole agent namespace for half an hour."""
    STATE.kernel_dir = tmp_path
    conf = {"key": "k", "depth": 0, "max_depth": 1, "max_concurrency": 1}
    STATE.config = conf
    a = _Agent(conf, {"codex": codex_backend})

    async def flow():
        h = a.spawn("SLOW one", provider="codex", name="slow")
        for _ in range(300):
            if h._session is not None and h._session._proc.turn_id:
                break
            await asyncio.sleep(0.01)
        await h.close()
        with pytest.raises(RuntimeError, match="closed"):
            await asyncio.wait_for(h.result(), 10)
        # The one permit is free again: under max_concurrency=1 a second agent can only
        # run if the stranded turn really did release it.
        h2 = a.spawn("second", provider="codex", name="quick")
        r = await asyncio.wait_for(h2.result(), 10)
        await h2.close()
        return r

    assert asyncio.run(flow()).text == "echo:second"


def test_a_server_that_dies_surfaces_an_error_instead_of_hanging(monkeypatch):
    monkeypatch.setenv("CODEX_BIN",
                       f"{shlex.quote(sys.executable)} -c 'import sys; sys.exit(3)'")
    with pytest.raises((RuntimeError, OSError)):
        asyncio.run(asyncio.wait_for(codex_backend.run_once("hi", opts()), 20))


# -- guard tests on the fake ----------------------------------------------
def _raw(*messages, argv=()):
    """Drive the fake by hand and collect what it writes back."""
    p = subprocess.run([sys.executable, str(FAKE), *argv],
                       input="".join(json.dumps(m) + "\n" for m in messages),
                       capture_output=True, text=True, timeout=30)
    return [json.loads(line) for line in p.stdout.splitlines() if line.strip()]


def test_fake_requires_client_info_version():
    out = _raw({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "x"}}})
    assert out[0]["error"]["code"] == -32602


def test_fake_requires_the_initialized_notification_first():
    out = _raw({"id": 1, "method": "initialize",
                "params": {"clientInfo": {"name": "x", "version": "1"}}},
               {"id": 2, "method": "thread/start", "params": {"cwd": "."}})
    assert out[1]["error"]["message"] == "Not initialized"


def test_fake_refuses_the_camel_case_sandbox_policy_tag():
    out = _raw({"id": 1, "method": "initialize",
                "params": {"clientInfo": {"name": "x", "version": "1"}}},
               {"method": "initialized"},
               {"id": 2, "method": "thread/start",
                "params": {"cwd": ".", "sandbox": "workspaceWrite"}})
    assert "unknown variant `workspaceWrite`" in out[1]["error"]["message"]


def test_fake_requires_a_turn_id_on_interrupt():
    out = _raw({"id": 1, "method": "initialize",
                "params": {"clientInfo": {"name": "x", "version": "1"}}},
               {"method": "initialized"},
               {"id": 2, "method": "turn/interrupt", "params": {"threadId": "th-1"}})
    assert out[1]["error"]["code"] == -32602
