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


#: Most output any one read hands back. A cell log has no size bound of its own — a chatty
#: cell writes gigabytes — and the caller is a CLI or an MCP adapter that would have to hold
#: the whole thing in memory before the renderer truncated it to a few thousand characters.
#: The cursor comes back pointing just past what WAS read, so the rest is still reachable.
READ_CHUNK_BYTES = 4_000_000


def read_since(path, offset: int, max_bytes: int = READ_CHUNK_BYTES) -> tuple[str, int]:
    """A bounded slice of `path` from `offset`, with the offset just past what was read."""
    try:
        with open(path, "rb") as f:
            f.seek(max(offset, 0))
            data = f.read(max_bytes)
            return data.decode(errors="replace"), f.tell()
    except OSError:
        return "", max(offset, 0)


def read_output_since(key: str, cell_id: int, offset: int,
                      max_bytes: int = READ_CHUNK_BYTES) -> tuple[str, int]:
    return read_since(cells_dir(key) / f"{cell_id}.log", offset, max_bytes)


def current_cell(key: str) -> int | None:
    try:
        return json.loads((cells_dir(key) / "current.json").read_text())["cell_id"]
    except (OSError, json.JSONDecodeError, KeyError):
        return None


#: (pid, identity) — this process's cursor identity, see `cursor_owner()`. The pid is
#: cached WITH the value because a process can acquire a new one without re-importing
#: this module: `os.fork()` (and so `multiprocessing` under its Linux-default `fork`
#: start method) hands the child a copy of every global the parent had computed. A child
#: that kept the parent's identity would read and advance the parent's offset sidecar —
#: the same two-callers-one-cursor gap `cursor_owner` exists to close, arriving by
#: inheritance instead of by sharing.
_CURSOR_OWNER: tuple[int, str] | None = None


def cursor_owner() -> str:
    """Whose implicit (`since=-1`) cursor the sidecar this process reads and writes is.

    An implicit cursor is CALLER state — "what have I already been shown" — kept on disk
    only because the caller (a CLI invocation, an MCP adapter, the kernel's own interrupt
    path) has nowhere else to keep it. Kept kernel-globally it was every caller's state at
    once: the MCP adapter and a `ptc wait` on the same cell both read one file and both
    advanced it, so whoever ran first consumed the output and the other silently saw a gap
    it could never ask for again. Per caller, each sees its own complete stream.

    The identity is the pid plus a digest of the process's birth time, so a recycled pid
    inherits nothing: the same missed-output failure, arriving by a slower route. It is
    recomputed whenever the pid under it has changed, which is how a FORKED child stops
    answering with the identity its parent computed before the fork (`_CURSOR_OWNER`).

    Callers that need a shared, durable cursor pass `since` explicitly — that remains the
    cross-process contract (and every `Running` render hands back the exact `since=` to
    continue from).
    """
    global _CURSOR_OWNER
    pid = os.getpid()
    if _CURSOR_OWNER is None or _CURSOR_OWNER[0] != pid:
        from .ownership import proc_start_time
        birth = hashlib.sha256((proc_start_time(pid) or "").encode()).hexdigest()[:8]
        _CURSOR_OWNER = (pid, f"{pid}-{birth}")
    return _CURSOR_OWNER[1]


def offset_name(cell_id: int) -> str:
    """This caller's cursor sidecar, by name.

    Public because the sidecar outlives the directory it was written in: a restart renames
    `cells/` — `offsets/` and all — into `cells-prev-*`, so an ARCHIVED epoch carries its
    own copy of this file, and `client._archived` has to look this caller's cursor up
    there under the same name (r11).
    """
    return f"{cell_id}.{cursor_owner()}.offset"


def offset_path(key: str, cell_id: int):
    return cells_dir(key) / "offsets" / offset_name(cell_id)


def default_offset(key: str, cell_id: int) -> int:
    try:
        return int(offset_path(key, cell_id).read_text())
    except (OSError, ValueError):
        return 0


def save_offset(key: str, cell_id: int, offset: int) -> None:
    secure_dir(cells_dir(key) / "offsets")
    offset_path(key, cell_id).write_text(str(offset))
