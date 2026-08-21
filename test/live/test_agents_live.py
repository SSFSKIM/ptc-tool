"""Live happy path for the agent namespace, driven through a real kernel (A4's engine).

Requires PTC_LIVE=1 and a `claude` CLI logged in on subscription auth. Deliberately cheap:
three one-turn children on the small model, each asked for a single word — this exists to
prove run/spawn/gather/send/list work end to end against the real SDK, not to exercise the
model. Kernel hygiene: it spawns one kernel under the test's own PTC_HOME and kills it.
"""
import os
import textwrap

import pytest

from ptc.client import Completed, KernelClient
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config

live = pytest.mark.skipif(os.environ.get("PTC_LIVE") != "1", reason="PTC_LIVE=1 required")


@live
def test_run_spawn_gather_in_kernel(ptc_home):
    # Trust model: the key would shadow OAuth and silently flip billing to metered API.
    assert "ANTHROPIC_API_KEY" not in os.environ
    ensure_kernel("al1", cwd=str(ptc_home))
    kc = KernelClient("al1")
    out = kc.exec_cell(textwrap.dedent("""
        r = await agent.run("Reply with exactly: SOLO", model="haiku", max_turns=1)
        print("RUN:", r.text.strip()[:40], "sid:", bool(r.session_id))
        h1 = agent.spawn("Reply with exactly: P1", name="p1", model="haiku", max_turns=1)
        h2 = agent.spawn("Reply with exactly: P2", name="p2", model="haiku", max_turns=1)
        a, b = await agent.gather(h1, h2)
        print("GATHER:", "P1" in a.text, "P2" in b.text)
        print("STATUS:", h1.status, h2.status)
        print("LIST:", sorted(e["name"] for e in agent.list()))
    """), timeout_s=420, config=Config.from_env())
    assert isinstance(out, Completed), out
    assert "RUN: SOLO" in out.output and "GATHER: True True" in out.output
    assert "STATUS: done done" in out.output
    assert "p1" in out.output and "p2" in out.output
    kill_kernel("al1")
