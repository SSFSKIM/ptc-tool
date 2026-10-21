"""Bootstrap's timeout covers synchronization AND execution, not a hidden 20 s cap.

The transport is a logical-clock double: kernel scheduling delays are external to
ptc, and spending real seconds here would make this regression load-sensitive too.
"""
from queue import Empty

import pytest

from ptc import client
from ptc.client import Completed, KernelClient, Running


class _DelayedKernel:
    def __init__(self, clock, ready_s, replies=True):
        self.clock = clock
        self.ready_s = ready_s
        self.replies = replies
        self.stopped = False

    def wait_for_ready(self, timeout):
        if timeout < self.ready_s:
            self.clock[0] += timeout
            raise RuntimeError("kernel was not scheduled before the readiness deadline")
        self.clock[0] += self.ready_s

    def execute(self, *args, **kwargs):
        return "bootstrap-request"

    def get_shell_msg(self, timeout):
        if not self.replies:
            self.clock[0] += timeout
            raise Empty
        return {"parent_header": {"msg_id": "bootstrap-request"},
                "content": {"status": "ok"}}

    def stop_channels(self):
        self.stopped = True


def _connection(monkeypatch, tmp_path, ready_s, replies=True):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    clock = [0.0]
    monkeypatch.setattr(client.time, "monotonic", lambda: clock[0])
    connection = _DelayedKernel(clock, ready_s, replies)
    kc = KernelClient("bootstrap-budget")
    monkeypatch.setattr(kc, "_connect", lambda: connection)
    monkeypatch.setattr(kc, "_await_cell_id", lambda *args, **kwargs: 1)
    return kc, connection, clock


def test_bootstrap_allows_a_slow_handshake_within_its_requested_budget(monkeypatch, tmp_path):
    # A loaded but healthy kernel answers after 21 s, within this caller's 30 s.
    # The former per-submission 20 s cap is not a second bootstrap deadline.
    kc, connection, _ = _connection(monkeypatch, tmp_path, ready_s=21)
    assert isinstance(kc._exec_raw("bootstrap()", timeout_s=30), Completed)
    assert connection.stopped


def test_bootstrap_does_not_restart_its_budget_after_synchronizing(monkeypatch, tmp_path):
    kc, connection, clock = _connection(monkeypatch, tmp_path, ready_s=4, replies=False)
    assert isinstance(kc._exec_raw("bootstrap()", timeout_s=5), Running)
    assert clock[0] == pytest.approx(5), "the handshake bought another execution budget"
    assert connection.stopped
