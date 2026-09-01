"""Peek against a REAL busy kernel — the spike's mechanism, hardened (spec acceptance
1/5 twins). Follows test_kernel_lifecycle.py's spawn idioms."""
import time

from ptc import peek_client
from ptc.client import KernelClient
from ptc.kernel import ensure_kernel, kill_kernel
from ptc.paths import Config


def test_peek_answers_under_a_busy_kernel_within_a_second(ptc_home):
    cfg = Config.from_env()
    ensure_kernel("peeklive", cwd=str(ptc_home), config=cfg)
    kc = KernelClient("peeklive")
    kc.exec_cell("n = 0", timeout_s=30, config=cfg)
    kc.exec_cell("while True:\n    n += 1", timeout_s=0.5, config=cfg)  # leaves it running
    try:
        t0 = time.monotonic()
        r = peek_client.peek_kernel("peeklive", "n")
        assert (time.monotonic() - t0) < 1.0
        assert r["repr"].isdigit() and r["truncated"] is False
    finally:
        kc.interrupt()
        kill_kernel("peeklive")


def test_peek_on_a_dead_kernel_raises_unavailable(ptc_home):
    cfg = Config.from_env()
    ensure_kernel("peekdead", cwd=str(ptc_home), config=cfg)
    kill_kernel("peekdead")
    import pytest
    with pytest.raises(peek_client.PeekUnavailable):
        peek_client.peek_kernel("peekdead", "x")
