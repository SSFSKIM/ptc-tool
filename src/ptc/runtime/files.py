"""File primitives with Claude-Code-exact edit semantics. Mutations audit, with diffs."""
import difflib
from pathlib import Path

from ptc import policy

from . import audit

_DIFF_CAP = 2000   # INCLUSIVE of the truncation note: spec acceptance 2 binds ≤2000


def _denied(tool: str, value: str) -> None:
    """Consult the deny policy for one governed call; raise (and audit) on a match.
    PolicyError from a malformed file propagates — loud is the contract."""
    rule = policy.match(tool, value)
    if rule is None:
        return
    audit.append("denied", tool=tool, rule=rule.index, value=value[:200])
    raise PermissionError(
        f"denied by policy rule {rule.index} "
        f"({'pattern ' + rule.pattern if rule.pattern else 'path ' + rule.path}) — "
        f"policy file: {policy.policy_path()}")


def _diff(path: str, old: str, new: str) -> str:
    d = "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=path, tofile=path, n=3))
    if len(d) > _DIFF_CAP:
        note = "\n…[diff truncated — the full change is in the file]"
        d = d[:_DIFF_CAP - len(note)] + note        # cap INCLUSIVE of the note
    return d


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
    resolved = str(p.resolve())     # non-strict on 3.12: fine for a not-yet-existing file;
    _denied("write", resolved)      # the CHECKED string equals the AUDITED string
    old = p.read_text(errors="replace") if p.is_file() else ""
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    n = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
    audit.append("write", path=resolved, added=n, diff=_diff(resolved, old, content))
    return f"Wrote {resolved} ({n} lines)"


def edit(path, old: str, new: str, replace_all: bool = False) -> str:
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"no such file: {p}")
    _denied("edit", str(p.resolve()))
    text = p.read_text()
    n = text.count(old)
    if n == 0:
        raise ValueError(f"string not found in {p}")
    if n > 1 and not replace_all:
        raise ValueError(f"found {n} occurrences in {p}, need exactly 1 — "
                         "widen the snippet to make it unique, or pass replace_all=True")
    count = n if replace_all else 1
    replaced = text.replace(old, new, count)
    p.write_text(replaced)
    removed = len(old.splitlines()) * count
    added = len(new.splitlines()) * count
    audit.append("edit", path=str(p.resolve()), added=added, removed=removed,
                 diff=_diff(str(p.resolve()), text, replaced))
    return f"Edited {p.resolve()} (+{added}/−{removed})"
