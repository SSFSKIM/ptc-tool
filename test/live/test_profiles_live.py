"""Acceptance 3 of the agent-host spec: the CLI route's prompt floors, measured live.

Requires PTC_LIVE=1. Four one-word haiku replies (the fourth is the `full` row of the skill's
floors table), each read off the first assistant
message's usage (`input + cache_creation + cache_read`), through the real `llm()` and
`agent.run()` paths in this process. `PTC_LAUNCHER` is set to this checkout's launcher, as
it is in every real kernel, so a child that would get the ptc MCP server does: the reader's
floor is the one a ptc cell sees.

The cwd is a directory under pytest's `tmp_path`, outside git (the spec author's ruling: the
limits carry ~10 % margin so the criterion checks how the prompt is made up, not how long
the path is). A child that is not `--bare` describes its cwd in an environment block, and
the floors move with it (CLI 2.1.305, the `llm()` form without `--bare`): 392 tokens in
`/tmp/<short>`, 417 under `tmp_path`, 634 in a linked git worktree, whose description alone
is ~240 tokens; the reader is 3,068 in `/tmp/<short>` and 3,302 in that worktree.
`CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1`, which every non-`full` profile sets, does not
remove those worktree lines (both worktree figures are with it).
"""
import asyncio
import os
from pathlib import Path

import pytest

from ptc.runtime.state import STATE

live = pytest.mark.skipif(os.environ.get("PTC_LIVE") != "1", reason="PTC_LIVE=1 required")


@pytest.fixture
def first_prompt_tokens(tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    cwd = str(cwd)
    # Trust model: the key would shadow OAuth and silently flip billing to metered API.
    assert "ANTHROPIC_API_KEY" not in os.environ
    import claude_agent_sdk
    STATE.kernel_dir = tmp_path
    STATE.config = {"key": "profiles-live", "depth": 0, "max_depth": 1,
                    "max_concurrency": 8, "cwd": cwd}
    launcher = Path(__file__).resolve().parents[2] / "bin" / "ptc-launch"
    monkeypatch.setenv("PTC_LAUNCHER", str(launcher))
    seen: list[dict] = []
    real = claude_agent_sdk.query

    async def capture(*, prompt, options):
        first = True
        async for m in real(prompt=prompt, options=options):
            if first and type(m).__name__ == "AssistantMessage":
                first = False
                u = m.usage
                assert u and "input_tokens" in u, f"no usage on the first reply: {m!r}"
                tokens = sum(u.get(k) or 0 for k in (
                    "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                assert tokens > 0, f"a prompt of no tokens is no measurement: {u!r}"
                seen.append({"tokens": tokens, "bare": "bare" in (options.extra_args or {}),
                             "ptc_server": bool(options.mcp_servers)})
            yield m
    monkeypatch.setattr(claude_agent_sdk, "query", capture)
    yield seen


@live
def test_llm_floor_with_credentials_runs_bare(first_prompt_tokens):
    if not (os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
        pytest.skip("no credential in the environment: --bare cannot sign in here")
    from ptc.runtime import _llm
    assert asyncio.run(_llm.llm("Reply with exactly: OK")).strip() == "OK"
    [m] = first_prompt_tokens
    print("llm, --bare:", m)
    assert m["bare"] and m["tokens"] <= 100


@live
def test_llm_floor_without_bare(first_prompt_tokens, monkeypatch):
    """The form the rule picks when the child's environment has no credential.

    `--bare` is withheld through the rule itself, with the credential left in place, rather
    than by removing the credential: on a machine that signs in only through the
    environment (the MacBook's gateway; its keychain login reads "OAuth session expired"),
    a child without it cannot authenticate at all, so the form would never be measured.
    The prompt is the same either way. `test_profiles.py` pins that a credential-free
    environment selects this form."""
    from ptc.runtime import _llm, claude_backend
    monkeypatch.setattr(claude_backend, "_bare", lambda *a: False)
    assert asyncio.run(_llm.llm("Reply with exactly: OK")).strip() == "OK"
    [m] = first_prompt_tokens
    print("llm, no --bare:", m)
    assert not m["bare"] and m["tokens"] <= 450


@live
def test_reader_floor(first_prompt_tokens):
    from ptc.runtime import claude_backend
    from ptc.runtime.agents import _Agent
    a = _Agent(STATE.config, {"claude": claude_backend})
    r = asyncio.run(a.run("Reply with exactly: OK", profile="reader", model="haiku",
                          max_turns=1))
    assert r.text.strip() == "OK"
    [m] = first_prompt_tokens
    print("reader:", m)
    assert not m["ptc_server"], "an explicit tool list that does not name ptc gets no server"
    assert m["tokens"] <= 3400


@live
def test_full_floor(first_prompt_tokens):
    """The `full` row of the skill's floors table: `agent.run`'s default child, which keeps
    Claude Code's whole tool set and, in a kernel, the ptc MCP server."""
    from ptc.runtime import claude_backend
    from ptc.runtime.agents import _Agent
    a = _Agent(STATE.config, {"claude": claude_backend})
    r = asyncio.run(a.run("Reply with exactly: OK", model="haiku", max_turns=1))
    assert r.text.strip() == "OK" and r.via == "cli"
    [m] = first_prompt_tokens
    print("full:", m)
    assert m["ptc_server"], "the default tool set keeps the ptc server"
