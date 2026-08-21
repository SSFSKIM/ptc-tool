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
    """The one directory a key owns. A key is a NAME, never a path expression: `.` and
    `..` would resolve to the kernels root or its parent, and every lifecycle write that
    follows (owner.json, cells/, the recursive kill on restart) would land outside the
    namespace. safe_key() neutralizes those segments; this is the assertion that it did.
    """
    root = kernels_root()
    d = root / key
    if key in ("", ".", "..") or d.parent != root:
        raise ValueError(f"unsafe kernel key {key!r}: not a single name under {root}")
    return d


def cells_dir(key: str) -> Path:
    return kernel_dir(key) / "cells"


def safe_key(raw: str) -> str:
    key = re.sub(r"[^A-Za-z0-9._-]", "-", raw)[:128]
    # Separators are already mapped away, but an all-dots (or empty) result is still a
    # path segment rather than a name — "." is the kernels root itself and ".." its
    # parent. Prefix it into a literal name; the mapping is idempotent, so a key that
    # already went through here comes back unchanged.
    return "key-" + key if key.strip(".") == "" else key


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
