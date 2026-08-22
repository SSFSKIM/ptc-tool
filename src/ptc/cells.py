"""Pure file-level access to per-cell logs, records, current.json, offsets."""
import hashlib
import json
import os
from dataclasses import dataclass

from .paths import cells_dir, secure_dir


@dataclass
class CellRecord:
    status: str
    duration_ms: int
    result_repr: str | None
    error: dict | None
    images: list
    mutations: list


def read_record(key: str, cell_id: int) -> CellRecord | None:
    p = cells_dir(key) / f"{cell_id}.json"
    try:
        d = json.loads(p.read_text())
        return CellRecord(**d)
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def read_output_since(key: str, cell_id: int, offset: int,
                      max_bytes: int = 4_000_000) -> tuple[str, int]:
    p = cells_dir(key) / f"{cell_id}.log"
    try:
        with open(p, "rb") as f:
            f.seek(max(offset, 0))
            data = f.read(max_bytes)
            return data.decode(errors="replace"), f.tell()
    except OSError:
        return "", max(offset, 0)


def current_cell(key: str) -> int | None:
    try:
        return json.loads((cells_dir(key) / "current.json").read_text())["cell_id"]
    except (OSError, json.JSONDecodeError, KeyError):
        return None


#: This process's cursor identity — see `cursor_owner()`. Computed once; a pid does not
#: change, and neither does the process it belongs to.
_CURSOR_OWNER: str | None = None


def cursor_owner() -> str:
    """Whose implicit (`since=-1`) cursor the sidecar this process reads and writes is.

    An implicit cursor is CALLER state — "what have I already been shown" — kept on disk
    only because the caller (a CLI invocation, an MCP adapter, the kernel's own interrupt
    path) has nowhere else to keep it. Kept kernel-globally it was every caller's state at
    once: the MCP adapter and a `ptc wait` on the same cell both read one file and both
    advanced it, so whoever ran first consumed the output and the other silently saw a gap
    it could never ask for again. Per caller, each sees its own complete stream.

    The identity is the pid plus a digest of the process's birth time, so a recycled pid
    inherits nothing: the same missed-output failure, arriving by a slower route.

    Callers that need a shared, durable cursor pass `since` explicitly — that remains the
    cross-process contract (and every `Running` render hands back the exact `since=` to
    continue from).
    """
    global _CURSOR_OWNER
    if _CURSOR_OWNER is None:
        from .ownership import proc_start_time
        pid = os.getpid()
        birth = hashlib.sha256((proc_start_time(pid) or "").encode()).hexdigest()[:8]
        _CURSOR_OWNER = f"{pid}-{birth}"
    return _CURSOR_OWNER


def _offset_path(key: str, cell_id: int):
    return cells_dir(key) / "offsets" / f"{cell_id}.{cursor_owner()}.offset"


def default_offset(key: str, cell_id: int) -> int:
    try:
        return int(_offset_path(key, cell_id).read_text())
    except (OSError, ValueError):
        return 0


def save_offset(key: str, cell_id: int, offset: int) -> None:
    secure_dir(cells_dir(key) / "offsets")
    _offset_path(key, cell_id).write_text(str(offset))
