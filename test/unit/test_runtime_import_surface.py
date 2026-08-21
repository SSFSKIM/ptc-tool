"""Pins the whole promised import surface: `from ptc.runtime import *` must hand back the
real callable/object for every name in `__all__`, not an import-machinery side effect.

Task 23 review F1: `__all__` listed "llm", but `ptc.runtime.llm` the package attribute was
permanently the SUBMODULE (Python sets it as a side effect of `from .llm import llm`, and
nothing ever wrote the callable back over it) — so `from ptc.runtime import *` silently
handed back the module, not the function, while the module's own docstring claimed
star-import "works as the spec promises." Nothing tested the import surface at all. This
file pins the whole promise so that class of defect can't come back unnoticed.
"""
import inspect

import ptc.runtime as rt


def test_all_lists_exactly_the_promised_names():
    # Locks the promise's shape itself — a name silently added/dropped from __all__
    # should fail this test, not slip through unnoticed alongside the exports below.
    assert rt.__all__ == ["read", "write", "edit", "bash", "agent", "llm", "asyncio"]


def test_star_import_yields_the_real_object_for_every_all_name():
    ns: dict = {}
    exec("from ptc.runtime import *", ns)

    for name in rt.__all__:
        assert name in ns, f"{name!r} missing from `from ptc.runtime import *`"

    for name in ("read", "write", "edit", "bash", "llm"):
        obj = ns[name]
        assert callable(obj), f"{name!r} should be callable, got {obj!r}"
        assert not inspect.ismodule(obj), (
            f"{name!r} resolved to its own submodule instead of the callable — "
            "this is exactly the defect class this test exists to catch")

    import asyncio as real_asyncio
    assert ns["asyncio"] is real_asyncio

    # `agent` is constructed per-kernel by bind() (module docstring); star-import before
    # any bind() has run in-process reaches the documented None placeholder, so this pins
    # star-import returns the CURRENT module global, whatever it is — not a stale copy.
    assert ns["agent"] is rt.agent


def test_bind_populates_a_real_agent_object_reachable_by_star_import():
    """After bind() runs once in-process, `ptc.runtime.agent` (and therefore
    star-import's `agent`) is the live `_Agent` namespace object, not the None
    placeholder — and `llm` is still the callable, not the submodule."""
    from ptc.runtime.agents import _Agent
    from ptc.runtime.state import STATE

    class _FakeShell:
        def __init__(self):
            self.user_ns: dict = {}

    STATE.config = {"key": "k", "depth": 0, "max_depth": 1, "max_concurrency": 2}
    ip = _FakeShell()
    try:
        rt.bind(ip)
        assert isinstance(rt.agent, _Agent)
        assert isinstance(ip.user_ns["agent"], _Agent)
        assert callable(ip.user_ns["llm"]) and not inspect.ismodule(ip.user_ns["llm"])
        assert ip.user_ns["llm"] is rt.llm

        ns: dict = {}
        exec("from ptc.runtime import *", ns)
        assert isinstance(ns["agent"], _Agent)
    finally:
        rt.agent = None   # don't leak a live _Agent into other tests' star-import checks
