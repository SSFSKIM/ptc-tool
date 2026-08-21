import asyncio
import os
import textwrap
import time

import pytest

from ptc.client import Completed, KernelClient
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config


def _exec(key, code):
    return KernelClient(key).exec_cell(textwrap.dedent(code), timeout_s=90, config=Config.from_env())


def test_bash_result_timeout_background(ptc_home):
    ensure_kernel("b1", cwd=str(ptc_home))
    out = _exec("b1", """
        r = await bash("echo out; echo err 1>&2; exit 3")
        print(r.code, r.stdout.strip(), r.stderr.strip(), r.timed_out)
    """)
    assert isinstance(out, Completed) and "3 out err False" in out.output

    out = _exec("b1", """
        r = await bash("sleep 30", timeout=1)
        print(r.timed_out, r.code)
    """)
    assert "True" in out.output

    out = _exec("b1", """
        h = await bash("sleep 0.5; echo done", background=True)
        print(type(h).__name__, h.poll() is None)
        r = await h.wait()
        print(r.stdout.strip())
    """)
    assert "BashHandle True" in out.output and "done" in out.output
    kill_kernel("b1")


def test_bash_background_kill_reaps_process(ptc_home):
    """kill() must not leak the grandchild: the kernel reaper only kills kernels,
    not the processes they spawn, so a leaked `sleep` would outlive the test."""
    ensure_kernel("b3", cwd=str(ptc_home))
    out = _exec("b3", """
        h = await bash("sleep 60", background=True)
        print(h.pid)
    """)
    pid = int(out.output.strip())
    _exec("b3", "h.kill()")

    deadline = time.time() + 10
    alive = True
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            alive = False
            break
        time.sleep(0.1)
    assert not alive, f"pid {pid} still alive {time.time() - deadline + 10:.1f}s after kill()"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    kill_kernel("b3")


def test_percent_bash_magic_still_works(ptc_home):
    ensure_kernel("b2", cwd=str(ptc_home))
    out = _exec("b2", "%%bash\necho magic-$((1+1))")
    assert "magic-2" in out.output
    kill_kernel("b2")
