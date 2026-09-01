"""Omitted arguments fall through to the environment (r2 review finding 8).

`timeout_s` / `max_output_chars` (MCP) and `-t` (CLI) had their defaults substituted in
the signature, so the handler could no longer tell "the caller omitted this" from "the
caller asked for 300", and PTC_YIELD_S / PTC_MAX_OUTPUT_CHARS were overwritten on every
normal call. Keyless: the kernel layer is faked — what is under test is which value
reaches it.
"""
import asyncio
from types import SimpleNamespace

import pytest

import ptc.cli as cli
import ptc.mcp as mcp_mod
from ptc.client import Busy, Running
from ptc.discovery import Resolved


@pytest.fixture
def seen(monkeypatch, tmp_path):
    """Capture what the kernel layer is called with, from either adapter."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    captured: dict = {}

    def fake_ensure(key, **kw):
        captured["ensure"] = kw
        return SimpleNamespace(expired_notice=None, build_note=None)

    def fake_exec(self, code, timeout_s, config):
        captured.update(timeout_s=timeout_s, cap=config.max_output_chars)
        # any outcome would do here; Busy is the cheapest to build, which is why the CLI
        # cases below expect the busy exit code rather than success
        return Busy(None, "lock-held")

    for mod in (cli, mcp_mod):
        monkeypatch.setattr(mod, "ensure_kernel", fake_ensure)
        monkeypatch.setattr(mod.KernelClient, "exec_cell", fake_exec, raising=False)
    return captured


def test_mcp_omitted_arguments_take_the_environment(seen, monkeypatch):
    monkeypatch.setenv("PTC_YIELD_S", "7")
    monkeypatch.setenv("PTC_MAX_OUTPUT_CHARS", "444")

    asyncio.run(mcp_mod.exec_tool(code="1", session="e1"))
    assert (seen["timeout_s"], seen["cap"]) == (7.0, 444)


def test_mcp_explicit_arguments_still_win(seen, monkeypatch):
    monkeypatch.setenv("PTC_YIELD_S", "7")
    monkeypatch.setenv("PTC_MAX_OUTPUT_CHARS", "444")

    asyncio.run(mcp_mod.exec_tool(code="1", session="e1", timeout_s=3, max_output_chars=99))
    assert (seen["timeout_s"], seen["cap"]) == (3.0, 99)


def test_cli_omitted_timeout_takes_the_environment(seen, monkeypatch, capsys):
    monkeypatch.setenv("PTC_YIELD_S", "11")
    monkeypatch.setattr(cli, "_pick_session",
                        lambda explicit: ("k7", None, Resolved("k7", "explicit", None, None, False)))

    assert cli.main(["exec", "1"]) == cli.EXIT_BUSY
    assert seen["timeout_s"] == 11.0

    assert cli.main(["exec", "-t", "2", "1"]) == cli.EXIT_BUSY
    assert seen["timeout_s"] == 2.0, "an explicit -t must still win"


@pytest.fixture
def waited(monkeypatch, tmp_path):
    """Capture what `wait_cell` is called with, from either adapter."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    captured: dict = {}

    def fake_wait(self, cell_id, timeout_s, since=-1, until=None):
        captured.update(cell_id=cell_id, timeout_s=timeout_s, since=since, until=until)
        return Running(cell_id, "", 0)

    for mod in (cli, mcp_mod):
        monkeypatch.setattr(mod.KernelClient, "wait_cell", fake_wait, raising=False)
    return captured


def test_mcp_wait_threads_the_until_pattern_through(waited, monkeypatch):
    """`until=` is only useful if it reaches the client: the adapter is where a wait turns
    from a timeout into an event trigger."""
    asyncio.run(mcp_mod.wait_tool(cell_id=4, session="w1", until=r"PAIR DISAGREE"))
    assert (waited["cell_id"], waited["until"]) == (4, r"PAIR DISAGREE")


def test_mcp_wait_without_a_pattern_still_asks_for_none(waited, monkeypatch):
    asyncio.run(mcp_mod.wait_tool(cell_id=4, session="w1"))
    assert waited["until"] is None and waited["since"] == -1


def test_cli_wait_threads_the_until_pattern_through(waited, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_pick_session",
                        lambda explicit: ("k9", None, Resolved("k9", "explicit", None, None, False)))

    assert cli.main(["wait", "4", "--until", "PAIR DISAGREE"]) == 0
    assert (waited["cell_id"], waited["until"]) == (4, "PAIR DISAGREE")

    assert cli.main(["wait", "4"]) == 0
    assert waited["until"] is None, "an omitted --until must not invent a pattern"
    capsys.readouterr()


def test_cli_exec_carries_the_resolved_metadata_into_the_kernel(seen, monkeypatch, capsys):
    """`ptc exec` creating the first kernel dropped the discovered cwd and Claude session
    id, so meta.json had no session id and history()/agent.fork() stayed unavailable even
    though discovery had worked (the restart path learned this first)."""
    monkeypatch.setattr(
        cli, "_pick_session",
        lambda explicit: ("k8", None, Resolved("k8", "hook-runfile", "sess-1", "/w", False)))

    assert cli.main(["exec", "1"]) == cli.EXIT_BUSY
    assert seen["ensure"]["cwd"] == "/w"
    assert seen["ensure"]["claude_session_id"] == "sess-1"
