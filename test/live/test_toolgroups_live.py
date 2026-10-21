"""A real host's Bash tools do not outlive it, however it dies (`ptc/toolgroups.py`).

Zero model calls: a second plugin loaded into the host runs Bash tools itself, through
`$.tool.call` at `session.start`, which is the engine's own Bash tool (a detached shell
per command, a direct child of the host), with no model turn deciding to.

    PTC_LIVE=1 uv run pytest test/live/test_toolgroups_live.py -q -rs
"""
import asyncio
import json
import os
import signal
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from ptc import toolgroups
from ptc.runtime import host as host_mod
from ptc.runtime.host import AgentHost
from ptc.runtime.state import STATE

live = pytest.mark.skipif(os.environ.get("PTC_LIVE") != "1", reason="PTC_LIVE=1 required")

PLUGIN = Path(__file__).resolve().parents[2] / "host-plugin"


def tools_plugin(root: Path, tag: int) -> Path:
    """A plugin whose mod starts two background Bash tools at `session.start`, `sleep <tag>`
    and `sleep <tag+1>`. (A foreground one called from a mod there was never run.)"""
    d = root / f"tools-{tag}"
    (d / ".claude-plugin").mkdir(parents=True)
    (d / "hooks" / "mods").mkdir(parents=True)
    (d / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": f"tools{tag}", "version": "0.0.0"}))
    (d / "hooks" / "hooks.json").write_text(json.dumps({"modules": ["./mods/tools.tsx"]}))
    (d / "hooks" / "mods" / "tools.tsx").write_text(textwrap.dedent(f"""\
        import type {{ Register }} from 'claude-code'
        let started = false
        export const register: Register = (on) => {{
          on('session.start', async ($, e, next) => {{
            const r = await next(e)
            if (!started) {{
              started = true
              void $.tool.call({{ tool: 'Bash', command: 'sleep {tag}', run_in_background: true }})
              void $.tool.call({{ tool: 'Bash', command: 'sleep {tag + 1}', run_in_background: true }})
            }}
            return r
          }})
        }}
        """))
    return d


def sleeps(tag: int) -> list[int]:
    out = subprocess.run(["ps", "-A", "-o", "pid=,command="], capture_output=True, text=True,
                         check=True).stdout
    return [int(line.split(None, 1)[0]) for line in out.splitlines()
            if line.split(None, 1)[1:] in ([f"sleep {tag}"], [f"sleep {tag + 1}"])]


def wait_for(pred, within: float) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


@pytest.fixture
def kernel_dir(tmp_path, monkeypatch):
    """This process stands in for the kernel: the tracker watches its children."""
    monkeypatch.setattr(STATE, "kernel_dir", tmp_path / "kd")
    (tmp_path / "kd").mkdir()
    toolgroups._reset()
    yield tmp_path / "kd"
    toolgroups._reset()


def host_with(extra: Path):
    async def launch(env, cwd, plugin_dir):
        from claude_agent_sdk import ClaudeAgentOptions
        orig = ClaudeAgentOptions.__init__

        def with_extra(self, *a, **kw):
            kw["plugins"] = [*kw.get("plugins", []), {"type": "local", "path": str(extra)}]
            orig(self, *a, **kw)
        ClaudeAgentOptions.__init__ = with_extra
        try:
            return await host_mod._launch_sdk(env, cwd, plugin_dir)
        finally:
            ClaudeAgentOptions.__init__ = orig
    return launch


def run_host(tmp_path, tag: int, kill) -> None:
    async def main():
        h = AgentHost(kernel_dir=tmp_path, cwd=str(tmp_path), plugin_dir=PLUGIN,
                      launch=host_with(tools_plugin(tmp_path, tag)))
        try:
            await h.ensure_started()
            assert await asyncio.to_thread(wait_for, lambda: len(sleeps(tag)) == 2, 30), \
                "the host's two Bash tools never started"
            # recorded by the watcher while the host lives
            assert await asyncio.to_thread(
                wait_for, lambda: set(sleeps(tag)) <= _recorded_members(), 5)
            await kill(h)
        finally:
            await h.close()
    asyncio.run(main())


def _recorded_members() -> set[int]:
    """Every process in a group the tracker has a row for."""
    pgids = {r["pgid"] for r in toolgroups.tracker().rows()}
    out = subprocess.run(["ps", "-A", "-o", "pid=,pgid="], capture_output=True, text=True).stdout
    return {int(p) for p, g in (line.split() for line in out.splitlines()) if int(g) in pgids}


@live
def test_a_host_sigkilled_from_outside_leaves_no_tool_running(tmp_path, kernel_dir):
    tag = 7741

    async def kill(h):
        os.kill(h.pid, signal.SIGKILL)            # an OOM kill: no ptc code runs at it
        assert await asyncio.to_thread(wait_for, lambda: not sleeps(tag), 5), sleeps(tag)
    run_host(tmp_path, tag, kill)


@live
def test_a_host_that_ignores_sigterm_is_killed_with_its_tools(tmp_path, kernel_dir):
    tag = 7743

    async def kill(h):
        os.kill(h.pid, signal.SIGSTOP)            # it can answer no SIGTERM now
        await h.close()                           # SIGTERM, 3 s, then the escalation
        assert await asyncio.to_thread(wait_for, lambda: not sleeps(tag), 5), sleeps(tag)
    run_host(tmp_path, tag, kill)


@live
def test_a_graceful_close_leaves_no_tool_running(tmp_path, kernel_dir):
    tag = 7745

    async def kill(h):
        await h.close()
        assert await asyncio.to_thread(wait_for, lambda: not sleeps(tag), 5), sleeps(tag)
    run_host(tmp_path, tag, kill)


# ---- in a real kernel ----------------------------------------------------------------------

LAUNCHER = Path(__file__).resolve().parents[2] / "bin" / "ptc-launch"


@pytest.fixture
def kernel(ptc_home, monkeypatch):
    """A real kernel that may start a host (`PTC_HOST` unset, `PTC_LAUNCHER` this
    checkout's), with Bash-running `claude` children: function hooks on for the CLI route's
    too, which inherits the kernel's environment."""
    from ptc.client import KernelClient
    from ptc.kernel import ensure_kernel, kill_kernel
    monkeypatch.delenv("PTC_HOST", raising=False)
    monkeypatch.setenv("PTC_LAUNCHER", str(LAUNCHER))
    monkeypatch.setenv("CLAUDE_CODE_ENABLE_FUNCTION_HOOKS", "1")
    keys = []

    def start(key: str, cwd: Path) -> KernelClient:
        ensure_kernel(key, cwd=str(cwd))
        keys.append(key)
        return KernelClient(key)
    yield start
    for k in keys:
        kill_kernel(k)


def cell(kc, code: str, timeout_s: float = 90) -> str:
    from ptc.client import Completed, Running
    from ptc.paths import Config
    out = kc.exec_cell(textwrap.dedent(code), timeout_s=timeout_s, config=Config.from_env())
    while isinstance(out, Running):
        out = kc.wait_cell(out.cell_id, timeout_s=timeout_s)
    assert isinstance(out, Completed), out
    return out.output


def with_tools(plugin: Path) -> str:
    """Cell code that loads `plugin` into every `claude` the kernel starts after it."""
    return f"""
        import claude_agent_sdk
        _orig = claude_agent_sdk.ClaudeAgentOptions.__init__
        def _with(self, *a, **kw):
            kw["plugins"] = [*kw.get("plugins", []), {{"type": "local", "path": {str(plugin)!r}}}]
            _orig(self, *a, **kw)
        claude_agent_sdk.ClaudeAgentOptions.__init__ = _with
    """


@live
def test_ptc_kill_takes_the_hosts_tools_with_the_kernel(kernel, tmp_path):
    from ptc.kernel import kill_kernel
    tag = 7761
    kc = kernel("tgkill", tmp_path)
    out = cell(kc, with_tools(tools_plugin(tmp_path, tag)) + """
        from ptc.runtime.host import get_host
        await get_host().ensure_started()
        print("HOST", get_host().pid)
    """)
    assert "HOST" in out, out
    assert wait_for(lambda: len(sleeps(tag)) == 2, 30), "the host's Bash tools never started"
    assert kill_kernel("tgkill")
    assert wait_for(lambda: not sleeps(tag), 3), sleeps(tag)


@live
def test_a_cli_child_sigkilled_from_outside_leaves_no_tool_running(kernel, tmp_path):
    tag = 7763
    kc = kernel("tgcli", tmp_path)
    out = cell(kc, with_tools(tools_plugin(tmp_path, tag)) + f"""
        from ptc.runtime import claude_backend
        from ptc.runtime.agents import AgentOpts
        s = await claude_backend.open_session(None, AgentOpts(cwd={str(tmp_path)!r}))
        print("CLI", s._client._transport._process.pid)
    """)
    pid = int(out.split("CLI", 1)[1].split()[0])
    assert wait_for(lambda: len(sleeps(tag)) == 2, 30), "the child's Bash tools never started"
    time.sleep(1)                                 # a tick or more: they are recorded
    os.kill(pid, signal.SIGKILL)
    assert wait_for(lambda: not sleeps(tag), 3), sleeps(tag)
