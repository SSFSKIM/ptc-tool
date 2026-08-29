"""`bash()` given an argv LIST runs the program directly — no shell, no quoting layer.

A prompt, a regex or a `--` payload built in Python and interpolated into a command
STRING is escaped twice: once by Python's own literal rules and again by the shell that
parses the result. The observed failure was silent — a `--` prompt arrived empty and the
command still exited 0 — so the argv form exists to remove the second parser entirely,
while keeping everything else about a `bash()` call (cwd, env merge, timeout, the process
group registration every reaper depends on) identical.
"""
import asyncio

import pytest

from ptc import bgroups
from ptc.runtime import shell
from ptc.runtime.state import STATE


@pytest.fixture(autouse=True)
def _fresh_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(STATE, "kernel_dir", tmp_path)
    shell._LIVE.clear()
    yield
    shell._LIVE.clear()


#: Every character here is one a shell would have claimed: a pipe, a variable expansion,
#: an unbalanced quote. Arriving verbatim is the whole proof.
_HOSTILE = "a|b$HOME'x"


def test_argv_list_passes_arguments_verbatim():
    r = asyncio.run(shell.bash(["printf", "%s", _HOSTILE]))

    assert r.code == 0, r.stderr
    assert r.stdout == _HOSTILE, "a shell got between the argv and the program"


def test_argv_list_still_registers_its_process_group(tmp_path):
    """The registry row is what `ptc kill`, `restart` and the TTL watchdog reap a
    `bash()` child by — it is its own session, so nothing else can reach it."""
    seen = []

    async def flow():
        task = asyncio.ensure_future(shell.bash(["sleep", "0.5"]))
        await asyncio.sleep(0.15)
        seen.extend(bgroups.read(tmp_path))
        return await task

    r = asyncio.run(flow())
    assert r.code == 0
    assert len(seen) == 1 and seen[0]["cmd"] == "sleep 0.5", seen


def test_argv_list_backgrounds_into_a_handle_that_settles():
    async def flow():
        h = await shell.bash(["printf", "%s", _HOSTILE], background=True)
        return await h.wait()

    r = asyncio.run(flow())
    assert r.code == 0, r.stderr
    assert r.stdout == _HOSTILE
