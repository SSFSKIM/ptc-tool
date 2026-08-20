"""PTC_HOME filesystem layout and environment-derived configuration."""
import os
import re
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT_CLAMP = 50_000


def ptc_home() -> Path:
    return Path(os.environ.get("PTC_HOME") or (Path.home() / ".ptc"))


def venv_dir() -> Path:
    return ptc_home() / "venv"


def run_dir() -> Path:
    return ptc_home() / "run"


def kernels_root() -> Path:
    return ptc_home() / "kernels"


def kernel_dir(key: str) -> Path:
    return kernels_root() / key


def cells_dir(key: str) -> Path:
    return kernel_dir(key) / "cells"


def safe_key(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", raw)[:128]


@dataclass
class Config:
    yield_s: float = 300.0
    max_output_chars: int = 12_000
    idle_hours: float = 24.0
    max_concurrency: int = 8
    max_depth: int = 1
    depth: int = 0
    session: str | None = None
    cwd: str | None = None

    @classmethod
    def from_env(cls, env=None) -> "Config":
        env = os.environ if env is None else env

        def num(name: str, cast, default):
            raw = env.get(name)
            if not raw:
                return default
            try:
                return cast(raw)
            except (TypeError, ValueError):
                return default

        return cls(
            yield_s=num("PTC_YIELD_S", float, 300.0),
            max_output_chars=min(num("PTC_MAX_OUTPUT_CHARS", int, 12_000), MAX_OUTPUT_CLAMP),
            idle_hours=num("PTC_IDLE_HOURS", float, 24.0),
            max_concurrency=num("PTC_MAX_CONCURRENCY", int, 8),
            max_depth=num("PTC_MAX_DEPTH", int, 1),
            depth=num("PTC_DEPTH", int, 0),
            session=env.get("PTC_SESSION") or None,
            cwd=env.get("PTC_CWD") or None,
        )
