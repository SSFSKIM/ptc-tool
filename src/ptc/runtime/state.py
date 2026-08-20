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


STATE = _State()


def cells() -> Path:
    return STATE.kernel_dir / "cells"
