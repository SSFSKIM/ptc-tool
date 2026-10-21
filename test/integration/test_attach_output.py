"""A fresh client's slow IOPub join cannot lose ptc's disk-backed cell output."""
import subprocess
import sys
import textwrap
import time
from queue import Empty

from ptc.client import Completed, KernelClient
from ptc.kernel import ensure_kernel, kill_kernel, restart_kernel
from ptc.paths import Config


def _immediate_print_without_iopub(key):
    kc = KernelClient(key)
    connect = kc._connect
    # iopub=False alone is insufficient: get_iopub_msg lazily subscribes. Refuse
    # EVERY IOPub read too, so no execute_input can substitute for the disk witness.
    # Bootstrap's own connection is separate and still synchronizes normally.
    def without_iopub():
        connection = connect(iopub=False)
        def empty(timeout):
            time.sleep(timeout)
            raise Empty
        connection.get_iopub_msg = empty
        return connection
    kc._connect = without_iopub
    out = kc.exec_cell('print("x")', timeout_s=30, config=Config.from_env())
    assert isinstance(out, Completed)
    assert out.output == "x\n"


def test_first_print_survives_every_fresh_client_attach_path(ptc_home):
    key = "attach-output"
    info = ensure_kernel(key, cwd=str(ptc_home))
    try:
        _immediate_print_without_iopub(key)  # first cell after bootstrap
        assert ensure_kernel(key).pid == info.pid
        _immediate_print_without_iopub(key)  # reattach to the standing kernel

        # Two fresh adapter processes: the second has no connection or Python state
        # left from the first, just as after an MCP server restart. Both attach to
        # the same live kernel, and their first statement prints immediately.
        for _ in range(2):
            child = subprocess.run([sys.executable, "-c", textwrap.dedent('''
                import time
                from queue import Empty
                from ptc.client import Completed, KernelClient
                from ptc.kernel import ensure_kernel
                from ptc.paths import Config
                info = ensure_kernel("attach-output")
                kc = KernelClient("attach-output")
                connect = kc._connect
                def without_iopub():
                    connection = connect(iopub=False)
                    def empty(timeout):
                        time.sleep(timeout)
                        raise Empty
                    connection.get_iopub_msg = empty
                    return connection
                kc._connect = without_iopub
                out = kc.exec_cell('print("x")', timeout_s=30, config=Config.from_env())
                assert isinstance(out, Completed), out
                assert out.output == "x\\n", out.output
                print(info.pid)
            ''')], capture_output=True, text=True, timeout=90)
            assert child.returncode == 0, child.stdout + child.stderr
            assert child.stdout.strip() == str(info.pid), "the adapter replaced the kernel"

        replacement = restart_kernel(key, cwd=str(ptc_home))
        assert replacement.pid != info.pid
        _immediate_print_without_iopub(key)  # first cell after a kernel restart
    finally:
        kill_kernel(key)
