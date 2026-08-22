"""Mutable in-kernel state shared by hooks and the runtime API."""
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class _State:
    key: str = ""
    kernel_dir: Path = Path(".")
    config: dict = field(default_factory=dict)
    current_cell: int | None = None
    cell_started: float = 0.0
    last_activity: float = field(default_factory=time.time)
    cell_images: list = field(default_factory=list)
    cell_mutations: list = field(default_factory=list)
    #: The kernel's own asyncio loop, captured by `bootstrap.install()`. ipykernel keeps it
    #: RUNNING between cells — it is what serves ZMQ — which is the property that lets the
    #: watchdog THREAD reach objects that live on it (`bootstrap._release_backends_now`).
    #: None where install() ran with no loop under it.
    loop: object | None = None


STATE = _State()


def cells() -> Path:
    return STATE.kernel_dir / "cells"
