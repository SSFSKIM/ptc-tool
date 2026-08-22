"""The kernel's own exits release the agent backends before they SIGKILL (r13 f3).

The group kill in `_reap_and_exit` reaps everything left in the kernel's process group.
A `codex app-server` shell-tool child is not in it — codex spawns those through `setsid()`
and its parent-death SIGTERM is Linux-only — so on macOS the kill leaves an in-flight
shell command running with nobody left who can enumerate it. Interrupting the turn and
closing the session first is the moment where that can still be avoided.

Fakes only: no codex binary is spawned, and the loop the release is handed to is a real
one running in a thread, which is the shape the watchdog really sees.
"""
import asyncio
import threading
import time

import pytest

from ptc import runtime
from ptc.runtime import bootstrap
from ptc.runtime.state import STATE


class _FakeSession:
    def __init__(self, *, interrupt_hangs: bool = False):
        self.calls: list[str] = []
        self._interrupt_hangs = interrupt_hangs

    async def interrupt(self):
        self.calls.append("interrupt")
        if self._interrupt_hangs:
            await asyncio.sleep(3600)

    async def close(self):
        self.calls.append("close")


class _FakeHandle:
    def __init__(self, session):
        self._session = session


class _FakeNamespace:
    def __init__(self, *handles):
        self._handles = {str(i): h for i, h in enumerate(handles)}


@pytest.fixture
def kernel_loop(monkeypatch):
    """A running event loop on another thread — what ipykernel keeps alive between cells,
    and the only thing the watchdog thread can hand asyncio work to."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    ready.wait(5)
    monkeypatch.setattr(STATE, "loop", loop)
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(5)
    loop.close()


def test_the_watchdog_path_interrupts_and_closes_every_live_backend(monkeypatch,
                                                                    kernel_loop):
    a, b = _FakeSession(), _FakeSession()
    monkeypatch.setattr(runtime, "agent", _FakeNamespace(_FakeHandle(a), _FakeHandle(b),
                                                         _FakeHandle(None)))

    t0 = time.monotonic()
    bootstrap._release_backends_now()          # called from a thread that is not the loop

    assert a.calls == ["interrupt", "close"], f"backend a: {a.calls}"
    assert b.calls == ["interrupt", "close"], f"backend b: {b.calls}"
    assert time.monotonic() - t0 < bootstrap._BACKEND_RELEASE_S


def test_a_backend_that_will_not_let_go_cannot_stall_the_exit(monkeypatch, kernel_loop):
    """Nothing may come between an expired kernel and its exit: the release is a chance to
    do better than the group kill, never something the exit waits on indefinitely."""
    stuck = _FakeSession(interrupt_hangs=True)
    monkeypatch.setattr(runtime, "agent", _FakeNamespace(_FakeHandle(stuck)))

    t0 = time.monotonic()
    bootstrap._release_backends_now(budget=0.3)
    elapsed = time.monotonic() - t0

    assert stuck.calls == ["interrupt"], "the hung step never even started"
    assert elapsed < 2.0, f"a wedged backend held the exit for {elapsed:.1f}s"


def test_a_dead_event_loop_is_skipped_rather_than_waited_on(monkeypatch):
    """The stated residual: the backends live on the kernel's loop and there is no other
    road to them. A loop that is gone takes the detached grandchildren with it, and the
    exit must not block on a hand-off that can never be picked up."""
    never = _FakeSession()
    monkeypatch.setattr(runtime, "agent", _FakeNamespace(_FakeHandle(never)))
    monkeypatch.setattr(STATE, "loop", None)

    t0 = time.monotonic()
    bootstrap._release_backends_now()
    assert never.calls == [] and time.monotonic() - t0 < 1.0


def test_reap_and_exit_releases_the_backends_before_it_kills(monkeypatch, tmp_path,
                                                             kernel_loop):
    """The order is the whole point: after the group kill there is nobody left to ask."""
    order: list = []

    class _Recording(_FakeSession):
        async def close(self):
            order.append("close")
            await super().close()

    session = _Recording()
    monkeypatch.setattr(runtime, "agent", _FakeNamespace(_FakeHandle(session)))
    monkeypatch.setattr(STATE, "kernel_dir", tmp_path)
    monkeypatch.setattr("os.killpg", lambda pgid, sig: order.append("killpg"))
    monkeypatch.setattr("os._exit", lambda code: order.append("exit"))
    monkeypatch.setattr("os.getpgid", lambda pid: 4242)
    monkeypatch.setattr("os.getpid", lambda: 4242)

    bootstrap._reap_and_exit()

    assert order[:2] == ["close", "killpg"], f"the backends were killed unasked: {order}"
    assert session.calls == ["interrupt", "close"]
