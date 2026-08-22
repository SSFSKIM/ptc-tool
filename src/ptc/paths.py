"""PTC_HOME filesystem layout and environment-derived configuration."""
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT_CLAMP = 50_000

#: Longest kernel directory name safe_key() will hand out. `runtime.agents.child_key`
#: builds child keys to fit inside it.
KEY_MAX = 128

#: Kernel state holds model-written code, command output, task prompts and whatever
#: secrets those touch. Under the common 022 umask a state directory lands 0755 and its
#: files 0644, so on a shared machine every other local account can read all of it.
#: Everything PTC creates under PTC_HOME is owner-only instead.
DIR_MODE = 0o700
FILE_MODE = 0o600

#: (raw PTC_HOME, resolved absolute path) — see ptc_home().
_HOME: tuple[str, Path] | None = None


def ptc_home() -> Path:
    """PTC_HOME, always absolute and `~`-expanded.

    A relative PTC_HOME resolves against the working directory of whoever reads it, and
    PTC's processes do not share one: the adapter reads it beside itself, `ensure_kernel`
    hands the same relative string to a kernel it starts in the session's `cwd`, and an
    `agent(..., cwd=…)` child gets a third answer — three processes provisioning and reading
    three different registries under one name, none of them able to see the others' kernels.
    Anchoring it here anchors every path in this module, and the absolute form is what
    `ensure_kernel` exports to the kernel and its children (`PTC_HOME=kernels_root().parent`).

    Resolved ONCE per value: a process that moves after it has provisioned state must not
    start reading a second home, so the answer is cached against the raw setting and only
    recomputed when the setting itself changes.
    """
    raw = os.environ.get("PTC_HOME")
    if not raw:
        return Path.home() / ".ptc"       # already absolute; left as the user's own path
    global _HOME
    if _HOME is None or _HOME[0] != raw:
        _HOME = (raw, Path(raw).expanduser().resolve())
    return _HOME[1]


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


def secure_dir(path) -> Path:
    """`mkdir -p` with owner-only modes, repairing the modes of directories that exist.

    Ancestors are created one level at a time because pathlib's `parents=True` applies
    `mode` to the leaf only, leaving the parents at the umask default. The repair pass
    walks upward but stops at PTC_HOME: a directory PTC did not create (a home directory,
    /tmp) keeps whatever mode its owner chose.
    """
    path = Path(path)
    missing = []
    p = path
    while not p.exists() and p.parent != p:
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        try:
            d.mkdir(DIR_MODE)
        except OSError:
            pass
    home = ptc_home()
    for d in (path, *path.parents):
        if d != home and home not in d.parents:
            continue
        try:
            if d.stat().st_mode & 0o077:
                d.chmod(DIR_MODE)
        except OSError:
            pass
    return path


def private_open(path, mode: str = "a", **kw):
    """`open()` that CREATES the file owner-only. os.open's mode applies at creation
    only, so a file that already exists keeps the mode it has."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if "a" in mode else os.O_TRUNC)
    return os.fdopen(os.open(path, flags, FILE_MODE), mode, **kw)


def private_write_text(path, text: str, *, tmp=None) -> None:
    """Atomic owner-only replace: an 0600 temp file beside the target, then rename."""
    path = Path(path)
    tmp = Path(tmp) if tmp is not None else path.with_name(path.name + ".tmp")
    with private_open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def safe_key(raw: str) -> str:
    """The one directory name a session key owns — LOSSLESSLY, where it has to be.

    Substitution and truncation are both many-to-one: `team/a` and `team?a` sanitized to
    the same `team-a`, and any two keys agreeing on 128 characters truncated together. The
    losers shared a kernel directory — one Python namespace, one cell log, one agent
    registry for two unrelated sessions. So a key the mapping CHANGED carries a digest of
    the original, which the collision partners cannot match.

    The digest is the whole of that separation, so its width is the width of an isolation
    boundary: at eight hex the two aliases `team!@@[x` and `team!)(~x` sanitize alike AND
    digest alike, which is 32 bits — a few tens of thousands of tries to search for, and
    near enough that two hand-written aliases can simply land on it. Sixteen is not a
    guess about how much margin is comfortable; it is the point where a directory name
    stops being a plausible thing to collide.

    A key that was already a clean name of legal length is returned byte-identical, digest
    and all: that is the common case (every generated key is one) and its kernel directory
    must not migrate. The mapping stays idempotent — its own output is always clean and
    within the bound, so a second pass changes nothing.
    """
    clean = re.sub(r"[^A-Za-z0-9._-]", "-", raw)
    if clean != raw or len(clean) > KEY_MAX:
        digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
        key = f"{clean[:KEY_MAX - len(digest) - 2]}-h{digest}"
    else:
        key = clean
    # Separators are already mapped away, but an all-dots (or empty) result is still a
    # path segment rather than a name — "." is the kernels root itself and ".." its
    # parent. Prefix it into a literal name. Only an unchanged key can land here: the
    # substitution emits "-", never ".", so a digested key is never all dots.
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

        # A bound below 1 parses fine and then hands out no permit ever: every agent,
        # llm() and web_search() call blocks forever on the shared semaphore, holding its
        # kernel cell in flight. Refuse it where it is read, the way the workflow-level
        # `limit` does, rather than deadlocking a kernel that looks healthy.
        max_concurrency = num("PTC_MAX_CONCURRENCY", int, 8)
        if max_concurrency < 1:
            raise ValueError(
                f"PTC_MAX_CONCURRENCY must be >= 1, got {max_concurrency}: a bound of "
                "zero or less releases no permits, so every agent/llm/web call would "
                "block forever waiting for one")

        return cls(
            yield_s=num("PTC_YIELD_S", float, 300.0),
            max_output_chars=min(num("PTC_MAX_OUTPUT_CHARS", int, 12_000), MAX_OUTPUT_CLAMP),
            idle_hours=num("PTC_IDLE_HOURS", float, 24.0),
            max_concurrency=max_concurrency,
            max_depth=num("PTC_MAX_DEPTH", int, 1),
            depth=num("PTC_DEPTH", int, 0),
            session=env.get("PTC_SESSION") or None,
            cwd=env.get("PTC_CWD") or None,
        )
