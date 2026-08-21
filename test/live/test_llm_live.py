"""Live happy path for llm() through a real kernel (A8's engine — the full A-cell runs in
T28 inside a real conversation). Requires PTC_LIVE=1 and a `claude` CLI logged in on
subscription auth. Deliberately cheap: five one-word replies on the small model — this
exists to prove llm() reaches the real SDK end to end via asyncio.gather, not to exercise
the model. Kernel hygiene: it spawns one kernel under the test's own PTC_HOME and kills it.
"""
import os
import textwrap

import pytest

from ptc.client import Completed, KernelClient
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config

live = pytest.mark.skipif(os.environ.get("PTC_LIVE") != "1", reason="PTC_LIVE=1 required")


@live
def test_llm_gather_map_reduce(ptc_home):
    # Trust model: the key would shadow OAuth and silently flip billing to metered API.
    assert "ANTHROPIC_API_KEY" not in os.environ
    ensure_kernel("ll1", cwd=str(ptc_home))
    out = KernelClient("ll1").exec_cell(textwrap.dedent("""
        words = ["ocean", "volcano", "glacier", "desert", "forest"]
        outs = await asyncio.gather(*[
            llm(f"Reply with exactly one word: the temperature (hot or cold) of a {w}")
            for w in words])
        print("N:", len(outs), "nonempty:", all(bool(o.strip()) for o in outs))
    """), timeout_s=420, config=Config.from_env())
    assert isinstance(out, Completed) and "N: 5 nonempty: True" in out.output
    kill_kernel("ll1")
