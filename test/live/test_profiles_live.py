"""Acceptance 3 of the agent-host spec: the CLI route's prompt floors, measured live.

Requires PTC_LIVE=1. Three one-word haiku replies, each read off the first assistant
message's usage (`input + cache_creation + cache_read`), through the real `llm()` and
`agent.run()` paths in this process — no kernel, since a kernel's child would carry the
ptc MCP server (see the reader case).

The cwd is a fresh, short directory outside git. A child that is not `--bare` describes its
cwd in an environment block, and the floors move with it (CLI 2.1.305, the `llm()` form
without `--bare`): 392 tokens in `/tmp/<short>`, 417 under pytest's long `tmp_path`, 634 in
a linked git worktree, whose description alone is ~240 tokens. The floors are taken where
that block is smallest, and the dependence is stated here rather than hidden in a margin.
"""
import asyncio
import os
import shutil

import pytest

from ptc.runtime.state import STATE

live = pytest.mark.skipif(os.environ.get("PTC_LIVE") != "1", reason="PTC_LIVE=1 required")


@pytest.fixture
def first_prompt_tokens(tmp_path, monkeypatch):
    import tempfile
    cwd = tempfile.mkdtemp(prefix="ptc-", dir="/tmp")
    # Trust model: the key would shadow OAuth and silently flip billing to metered API.
    assert "ANTHROPIC_API_KEY" not in os.environ
    import claude_agent_sdk
    STATE.kernel_dir = tmp_path
    STATE.config = {"key": "profiles-live", "depth": 0, "max_depth": 1,
                    "max_concurrency": 8, "cwd": cwd}
    monkeypatch.delenv("PTC_LAUNCHER", raising=False)
    seen: list[dict] = []
    real = claude_agent_sdk.query

    async def capture(*, prompt, options):
        first = True
        async for m in real(prompt=prompt, options=options):
            if first and type(m).__name__ == "AssistantMessage":
                first = False
                u = m.usage or {}
                seen.append({"tokens": sum(u.get(k, 0) for k in (
                    "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
                    "bare": "bare" in (options.extra_args or {})})
            yield m
    monkeypatch.setattr(claude_agent_sdk, "query", capture)
    yield seen
    shutil.rmtree(cwd, ignore_errors=True)


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

    `--bare` is withheld through the rule itself rather than by removing the credential:
    on a machine that signs in only through the environment (the MacBook's gateway), a
    child without it cannot authenticate at all, so the form would never be measured.
    `test_profiles.py` pins that a credential-free environment selects this form."""
    from ptc.runtime import _llm, claude_backend
    monkeypatch.setattr(claude_backend, "_bare", lambda *a: False)
    assert asyncio.run(_llm.llm("Reply with exactly: OK")).strip() == "OK"
    [m] = first_prompt_tokens
    print("llm, no --bare:", m)
    assert not m["bare"] and m["tokens"] <= 400


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
    assert m["tokens"] <= 3100
