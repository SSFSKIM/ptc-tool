"""Live fork through the kernel (A5's engine — the full A-cell runs in T28 inside a
real conversation). Requires PTC_LIVE=1 and a `claude` CLI logged in on subscription
auth. Deliberately cheap: one real parent turn plus one one-turn forked child, both on
whatever default model `claude -p` picks. Kernel hygiene: spawns one kernel under the
test's own PTC_HOME and kills it.
"""
import os
import textwrap

import pytest

from ptc.client import Completed, KernelClient
from ptc.discovery import write_meta
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config

live = pytest.mark.skipif(os.environ.get("PTC_LIVE") != "1", reason="PTC_LIVE=1 required")


@live
def test_fork_recalls_parent_fact(ptc_home):
    import subprocess, json, re
    # create a real claude session containing a marker, capture its session id
    r = subprocess.run(["claude", "-p", "--output-format", "json",
                        "Remember: the launch code is ZEBRA-77. Say OK."],
                       capture_output=True, text=True, timeout=300)
    sid = json.loads(r.stdout)["session_id"]
    ensure_kernel("fl1", cwd=str(ptc_home), claude_session_id=sid)
    out = KernelClient("fl1").exec_cell(textwrap.dedent("""
        r = await agent.fork("What is the launch code? Reply with only the code.", max_turns=1)
        print("FORK:", r.text.strip())
    """), timeout_s=300, config=Config.from_env())
    assert isinstance(out, Completed) and "ZEBRA-77" in out.output
    kill_kernel("fl1")
