"""The in-kernel runtime API. Every name bind() puts in the user namespace is ALSO a
module-level export, so `from ptc.runtime import *` works as the spec promises. Later
tasks extend __all__ and the imports in lockstep; bind() itself does not change again.
"""
import asyncio  # noqa: F401 — bound into the namespace: gather() is the fan-out idiom

from .files import edit, read, write  # noqa: F401
from .shell import bash  # noqa: F401

__all__ = ["read", "write", "edit", "bash", "agent", "asyncio"]

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
    ip.user_ns.update({name: globals()[name] for name in __all__})
