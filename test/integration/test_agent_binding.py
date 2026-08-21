"""Keyless proof that the agent namespace is really bound inside a live kernel, and that
the depth brake fires there before any SDK call (A14's engine — no auth needed).
"""
import textwrap

from ptc.client import Completed, KernelClient
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config


def test_agent_is_bound_in_the_kernel_namespace(ptc_home):
    ensure_kernel("ab1", cwd=str(ptc_home))
    out = KernelClient("ab1").exec_cell(
        "print('BOUND', type(agent).__name__, agent.list(), asyncio.__name__)",
        timeout_s=60, config=Config.from_env())
    assert isinstance(out, Completed), out
    assert "BOUND _Agent [] asyncio" in out.output, out.output
    kill_kernel("ab1")


def test_depth_guard_fires_in_the_kernel(ptc_home):
    """A child kernel runs at PTC_DEPTH=1: agent.run must refuse before it ever reaches
    the SDK, which is why this is provable with no credentials at all."""
    ensure_kernel("ab2", cwd=str(ptc_home), config=Config(depth=1, max_depth=1))
    out = KernelClient("ab2").exec_cell(textwrap.dedent("""
        try:
            await agent.run("should never start")
        except RuntimeError as e:
            print("DEPTH:", e)
    """), timeout_s=60, config=Config.from_env())
    assert isinstance(out, Completed), out
    assert "DEPTH: agent depth limit reached (PTC_DEPTH=1, PTC_MAX_DEPTH=1)" in out.output
    assert "raise PTC_MAX_DEPTH to allow grandchildren" in out.output
    kill_kernel("ab2")
