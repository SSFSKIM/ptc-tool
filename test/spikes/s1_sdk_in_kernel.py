"""S1: run claude-agent-sdk INSIDE a real ptc kernel. Requires PTC_LIVE=1 and `claude` login.

Usage:  PTC_LIVE=1 uv run --group dev python test/spikes/s1_sdk_in_kernel.py

CASES are the spike's six planned cases, verbatim. ADDENDUM was added after the first
live pass came back clean, to close the questions the six leave open and that T20 has
to build on: whether nest_asyncio is present at all, whether a cancelled query reaps
its `claude` child or orphans it, whether a fresh SDK call works in the same kernel
AFTER a CLI has been killed under it, and how an interrupted turn is distinguishable
from a completed one in the message stream.
"""
import os
import sys
import textwrap

sys.path.insert(0, "src")
from ptc.client import Completed, KernelClient  # noqa: E402
from ptc.kernel import ensure_kernel, kill_kernel  # noqa: E402
from ptc.paths import Config  # noqa: E402

KEY = "spike-s1"
CASES = {
    "one_shot": """
        import asyncio, time
        from claude_agent_sdk import query, ClaudeAgentOptions
        async def one():
            texts = []
            async for m in query(prompt="Reply with exactly: PONG",
                                 options=ClaudeAgentOptions(max_turns=1, tools=[],
                                                            permission_mode="bypassPermissions")):
                texts.append(type(m).__name__)
            return texts
        r = await one()
        print("ONE_SHOT_OK", r[-1])
    """,
    "two_concurrent": """
        async def ask(word):
            out = ""
            async for m in query(prompt=f"Reply with exactly: {word}",
                                 options=ClaudeAgentOptions(max_turns=1, tools=[],
                                                            permission_mode="bypassPermissions")):
                if hasattr(m, "content"):
                    for b in m.content:
                        out += getattr(b, "text", "")
            return out
        a, b = await asyncio.gather(ask("ALPHA"), ask("BETA"))
        print("CONCURRENT_OK", "ALPHA" in a, "BETA" in b)
    """,
    "cancel_midstream": """
        t = asyncio.ensure_future(ask("GAMMA " * 200))
        await asyncio.sleep(2)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            print("CANCEL_OK")
        # kernel still responsive?
        print("STILL_ALIVE", 1 + 1)
    """,
    "client_lifecycle": """
        # Two REAL ClaudeSDKClient sessions: connect, first turn, follow-up send,
        # interrupt on one, disconnect both, and no leaked claude processes (F7).
        import subprocess as _sp, os as _os
        from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

        def _kids():
            out = _sp.run(["pgrep", "-P", str(_os.getpid())], capture_output=True, text=True)
            return set(out.stdout.split())

        base = _kids()
        async def session_flow(word):
            c = ClaudeSDKClient(options=ClaudeAgentOptions(
                max_turns=1, tools=[], permission_mode="bypassPermissions"))
            await c.connect()
            await c.query(f"Reply with exactly: {word}")
            got = ""
            async for m in c.receive_response():
                for b in getattr(m, "content", []) or []:
                    got += getattr(b, "text", "")
            await c.query(f"Now reply with exactly: {word}2")
            got2 = ""
            async for m in c.receive_response():
                for b in getattr(m, "content", []) or []:
                    got2 += getattr(b, "text", "")
            await c.disconnect()
            return word in got, f"{word}2" in got2
        r1, r2 = await asyncio.gather(session_flow("RED"), session_flow("BLUE"))
        print("CLIENTS_OK", r1, r2)
        await asyncio.sleep(1)
        leaked = _kids() - base
        print("LEAKED", sorted(leaked))
    """,
    "client_interrupt": """
        c = ClaudeSDKClient(options=ClaudeAgentOptions(
            tools=[], permission_mode="bypassPermissions"))
        await c.connect()
        await c.query("Count slowly from 1 to 500, one number per line.")
        await asyncio.sleep(2)
        await c.interrupt()
        try:
            async for m in c.receive_response():
                pass
        except Exception as e:
            print("INTERRUPT_RAISED", type(e).__name__)
        await c.disconnect()
        print("INTERRUPT_OK")
    """,
    "kill_cli_midstream": """
        import subprocess, signal
        async def ask_with_kill():
            task = asyncio.ensure_future(ask("DELTA"))
            await asyncio.sleep(1.5)
            # kill every `claude` child of this kernel
            me = str(__import__("os").getpid())
            out = subprocess.run(["pgrep", "-P", me], capture_output=True, text=True).stdout.split()
            for pid in out:
                try: __import__("os").kill(int(pid), signal.SIGKILL)
                except Exception: pass
            try:
                return await asyncio.wait_for(task, timeout=20)
            except (asyncio.TimeoutError, Exception) as e:
                return f"RAISED {type(e).__name__}"
        print("KILL_RESULT", await ask_with_kill())
        print("STILL_ALIVE_2", 2 + 2)
    """,
}

ADDENDUM = {
    "loop_identity": """
        # No quota. What loop are we actually on, and is nest_asyncio anywhere near it?
        import sys as _sys, sniffio
        _loop = asyncio.get_running_loop()
        print("NEST_ASYNCIO_IMPORTED", "nest_asyncio" in _sys.modules)
        print("LOOP", f"{type(_loop).__module__}.{type(_loop).__name__}",
              "running", _loop.is_running(), "patched", hasattr(_loop, "_nest_patched"))
        print("SNIFFIO_BACKEND", sniffio.current_async_library())
    """,
    "cancel_reaps_child": """
        # cancel_midstream proved the CELL survives cancellation. This asks the other
        # half: does the cancelled query take its `claude` child down with it?
        _before = _kids()
        _t = asyncio.ensure_future(ask("ZETA " * 200))
        await asyncio.sleep(2.5)
        _during = _kids()
        _t.cancel()
        try:
            await _t
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(2)
        _after = _kids()
        print("CANCEL_CHILDREN before/during/after", len(_before), len(_during), len(_after))
        print("CANCEL_ORPHANS", sorted(_after - _before))
    """,
    "recover_after_cli_death": """
        # kill_cli_midstream showed the kernel survives. Can the SDK still be USED here?
        _seq, _res = [], None
        async for m in query(prompt="Reply with exactly: EPSILON",
                             options=ClaudeAgentOptions(max_turns=1, tools=[],
                                                        permission_mode="bypassPermissions")):
            _seq.append(type(m).__name__)
            _res = m
        print("RECOVER_SEQ", _seq)
        print("RECOVER_RESULT", {k: getattr(_res, k, None) for k in
              ("subtype", "is_error", "num_turns", "terminal_reason", "total_cost_usd")})
        print("RECOVER_SESSION_ID_LEN", len(getattr(_res, "session_id", "") or ""))
    """,
    "interrupt_terminal_reason": """
        # How does a caller TELL an interrupted turn from a finished one?
        _c = ClaudeSDKClient(options=ClaudeAgentOptions(
            tools=[], permission_mode="bypassPermissions"))
        await _c.connect()
        await _c.query("Count slowly from 1 to 500, one number per line.")
        await asyncio.sleep(2)
        await _c.interrupt()
        _last = None
        async for m in _c.receive_response():
            _last = m
        await _c.disconnect()
        print("INTERRUPT_LAST", type(_last).__name__,
              {k: getattr(_last, k, None) for k in
               ("subtype", "is_error", "num_turns", "terminal_reason")})
    """,
    "settings_isolation": """
        # ClaudeAgentOptions(setting_sources=None) is "load everything the CLI would":
        # user + project settings, their hooks, their CLAUDE.md. Price the SAME trivial
        # prompt both ways — what an in-kernel agent inherits by default is a cost and a
        # blast-radius question, not a style one.
        import asyncio, os as _os
        from claude_agent_sdk import query, ClaudeAgentOptions
        print("APIKEY_IN_KERNEL_ENV", "ANTHROPIC_API_KEY" in _os.environ)

        async def _probe(**kw):
            seq, res = [], None
            async for m in query(prompt="Reply with exactly: OMEGA",
                                 options=ClaudeAgentOptions(max_turns=1, tools=[],
                                                            permission_mode="bypassPermissions",
                                                            **kw)):
                seq.append(type(m).__name__)
                res = m
            return seq, res

        for _label, (_seq, _res) in (("INHERITED", await _probe()),
                                     ("ISOLATED", await _probe(setting_sources=[]))):
            _u = getattr(_res, "usage", None) or {}
            print(f"SETTINGS_{_label}",
                  "cost_usd", getattr(_res, "total_cost_usd", None),
                  "hook_msgs", _seq.count("HookEventMessage"),
                  "in", _u.get("input_tokens"),
                  "cache_read", _u.get("cache_read_input_tokens"),
                  "cache_write", _u.get("cache_creation_input_tokens"))
    """,
}


def main():
    if os.environ.get("PTC_LIVE") != "1":
        print("SKIP: set PTC_LIVE=1"); return
    ensure_kernel(KEY, cwd=os.getcwd())
    kc = KernelClient(KEY)
    cfg = Config.from_env()
    for name, code in {**CASES, **ADDENDUM}.items():
        out = kc.exec_cell(textwrap.dedent(code), timeout_s=180, config=cfg)
        status = out.record.status if isinstance(out, Completed) else "TIMEOUT/RUNNING"
        print(f"=== {name}: {status}\n{getattr(out, 'output', '')[:1500]}\n")
    kill_kernel(KEY)


if __name__ == "__main__":
    main()
