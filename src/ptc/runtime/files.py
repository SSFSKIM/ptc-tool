"""File primitives with Claude-Code-exact edit semantics. Mutations audit."""
from pathlib import Path

from . import audit


def read(path, offset: int | None = None, limit: int | None = None,
         numbered: bool = False) -> str:
    p = Path(path).expanduser()
    text = p.read_text(errors="replace")
    if offset is None and limit is None and not numbered:
        return text
    lines = text.splitlines(keepends=True)
    start = (offset - 1) if offset else 0
    sel = lines[start: start + limit if limit else None]
    if numbered:
        return "".join(f"{start + i + 1:>6}\t{ln}" for i, ln in enumerate(sel))
    return "".join(sel)


def write(path, content: str) -> str:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    n = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
    audit.append("write", path=str(p.resolve()), added=n)
    return f"Wrote {p.resolve()} ({n} lines)"


def edit(path, old: str, new: str, replace_all: bool = False) -> str:
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"no such file: {p}")
    text = p.read_text()
    n = text.count(old)
    if n == 0:
        raise ValueError(f"string not found in {p}")
    if n > 1 and not replace_all:
        raise ValueError(f"found {n} occurrences in {p}, need exactly 1 — "
                         "widen the snippet to make it unique, or pass replace_all=True")
    count = n if replace_all else 1
    p.write_text(text.replace(old, new, count))
    removed = len(old.splitlines()) * count
    added = len(new.splitlines()) * count
    audit.append("edit", path=str(p.resolve()), added=added, removed=removed)
    return f"Edited {p.resolve()} (+{added}/−{removed})"
