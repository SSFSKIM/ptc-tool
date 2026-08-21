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


def test_bash_timeout_preserves_pre_kill_output(ptc_home):
    """The timeout path must not discard output produced before the kill:
    asyncio.wait_for cancelling proc.communicate() mid-read must not drop
    already-emitted stdout."""
    ensure_kernel("b4", cwd=str(ptc_home))
    out = _exec("b4", """
        r = await bash("echo before; sleep 30", timeout=1)
        print(r.timed_out, r.code, repr(r.stdout))
    """)
    assert isinstance(out, Completed)
    assert "True None" in out.output
    assert "before" in out.output
    kill_kernel("b4")


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


def _wait_gone(pid: int, patience: float = 10.0) -> bool:
    deadline = time.time() + patience
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.1)
    return False


def test_background_group_dies_with_the_kernel(ptc_home):
    """A background bash child is its OWN session (that is what lets a timeout kill the
    command's whole tree), which also puts it outside the kernel's process group — so the
    group kill that reaps every other kernel child never reached it and a
    `bash(..., background=True)` outlived kill/restart/TTL as an orphan."""
    import json
    ensure_kernel("b5", cwd=str(ptc_home))
    out = _exec("b5", """
        h = await bash("sleep 300", background=True)
        print(h.pid)
    """)
    pid = int(out.output.strip())
    try:
        rows = json.loads((ptc_home / "kernels" / "b5" / "bash-pgids.json").read_text())
        assert [r for r in rows if r["pid"] == pid], f"{pid} was never registered: {rows}"

        kill_kernel("b5")

        assert _wait_gone(pid), f"background group {pid} outlived the kernel that spawned it"
        assert not (ptc_home / "kernels" / "b5" / "bash-pgids.json").exists()
    finally:
        try:
            os.kill(pid, 9)          # never leave a stray sleep behind
        except OSError:
            pass


def test_background_group_is_forgotten_when_it_ends(ptc_home):
    """The registry must shrink as groups exit, or a reap eventually signals a pgid the
    OS has handed to somebody else."""
    import json
    ensure_kernel("b6", cwd=str(ptc_home))
    out = _exec("b6", """
        h = await bash("true", background=True)
        r = await h.wait()
        print("code", r.code)
    """)
    assert "code 0" in out.output
    rows = json.loads((ptc_home / "kernels" / "b6" / "bash-pgids.json").read_text())
    assert rows == [], f"an exited group stayed in the registry: {rows}"
    kill_kernel("b6")


def test_percent_bash_magic_still_works(ptc_home):
    ensure_kernel("b2", cwd=str(ptc_home))
    out = _exec("b2", "%%bash\necho magic-$((1+1))")
    assert "magic-2" in out.output
    kill_kernel("b2")
