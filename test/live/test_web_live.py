"""Live engine for A7: web_search through a real kernel, real CLI, real WebSearch tool.

Requires PTC_LIVE=1 and a `claude` CLI logged in on subscription auth. Deliberately one
cheap query — this proves the S6-pinned parse survives the whole path (kernel → scoped SDK
query → tool_result → SearchResult), not that the search engine is good. Kernel hygiene:
one kernel under the test's own PTC_HOME, killed at the end.

The A7 wording asks the test to prove the KERNEL's env carries no ANTHROPIC_API_KEY, so
the check runs inside the cell as well as in the test process: the kernel is a separate
process and only the in-cell read speaks for it.
"""
import os
import textwrap

import pytest

from ptc.client import Completed, KernelClient
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config

live = pytest.mark.skipif(os.environ.get("PTC_LIVE") != "1", reason="PTC_LIVE=1 required")


@live
def test_web_search_structured(ptc_home):
    # Trust model: the key would shadow OAuth and silently flip billing to metered API.
    assert "ANTHROPIC_API_KEY" not in os.environ
    ensure_kernel("w1", cwd=str(ptc_home))
    out = KernelClient("w1").exec_cell(textwrap.dedent("""
        import os
        rs = await web_search("anthropic claude release notes")
        print("no_api_key:", "ANTHROPIC_API_KEY" not in os.environ)
        print("N:", len(rs), "urls_ok:", all(r.url.startswith("http") for r in rs))
        print("titled:", sum(1 for r in rs if r.title))
        print("first:", rs[0].url if rs else "-")
    """), timeout_s=420, config=Config.from_env())
    assert isinstance(out, Completed), out
    assert "no_api_key: True" in out.output
    assert "urls_ok: True" in out.output and "N: 0" not in out.output
    kill_kernel("w1")
