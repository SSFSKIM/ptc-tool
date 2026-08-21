"""The in-kernel runtime API. Every name bind() puts in the user namespace is ALSO a
module-level export, so `from ptc.runtime import *` works as the spec promises. Later
tasks extend __all__ and the imports in lockstep; bind() itself does not change again.
"""
import asyncio  # noqa: F401 — bound into the namespace: gather() is the fan-out idiom

from ._llm import llm  # noqa: F401 — `llm` lives in the private `_llm` submodule so the
# public name `ptc.runtime.llm` can BE the callable (re-exported here), not the module
# that import machinery would otherwise leave it as. Tests reach internals via
# `from ptc.runtime import _llm`.
from .files import edit, read, write  # noqa: F401
from .shell import bash  # noqa: F401
from .transcript import history  # noqa: F401 — plain function, no submodule-name
# collision (module is `transcript`, callable is `history`).
from .web import web_fetch, web_search  # noqa: F401 — plain functions, so (unlike `llm`)
# no private-submodule dance is needed: `web` the module keeps its own name and the two
# callables are bound over nothing.
from .wf import workflow  # noqa: F401 — module `wf`, namespace object `workflow`: the
# two names differ, so the submodule attribute cannot shadow the exported object the way
# it did for `llm`. Tests reach internals via `from ptc.runtime import wf`.

__all__ = ["read", "write", "edit", "bash", "agent", "llm", "web_fetch", "web_search",
           "history", "workflow", "asyncio"]

#: constructed per-kernel by bind(); None until then (importing this module must not
#: require a live kernel).
agent = None


def _make_agent():
    from . import claude_backend
    from .agents import _Agent
    from .state import STATE
    backends = {"claude": claude_backend}
    try:
        from . import codex_backend          # T22; absent until then is fine
        backends["codex"] = codex_backend
    except ImportError:
        pass
    return _Agent(STATE.config, backends)


def bind(ip) -> None:
    global agent
    agent = _make_agent()
    ns = {name: globals()[name] for name in __all__}
    ip.user_ns.update(ns)
