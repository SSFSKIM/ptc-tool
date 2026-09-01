"""Provision immutable per-build venvs under ~/.ptc/venvs/<build_id>/.

Payload computation lives ONLY in the provisioners — this module's ensure_venv (run from a
checkout or the plugin dir) and bin/ptc-launch (stdlib twin; MUST stay semantically
identical). Runtimes never recompute a payload: a provisioned venv carries its stamp
inside, and a process identifies its own build via sys.prefix (`runtime_venv`).
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .paths import ptc_home, venv_dir, venvs_root

PKG_ROOT = Path(__file__).resolve().parent.parent.parent  # .../ptc-tool


def _uv() -> str:
    return shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")


def src_tree_sha(root: Path | None = None) -> str:
    """Content hash of the packaged source tree — sorted relative posix paths + content
    hashes, so the same source hashes the same from any absolute location."""
    base = (root or PKG_ROOT) / "src" / "ptc"
    parts = [f"{p.relative_to(base).as_posix()}:"
             f"{hashlib.sha256(p.read_bytes()).hexdigest()}"
             for p in sorted(base.rglob("*.py"))]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def stamp_payload() -> dict:
    """What a build is MADE FROM — and nothing about where it sits.

    `uv.lock` is half the answer and used to be no part of it: a dependency fix that
    changed only the lock left every existing venv reading as current and every fresh one
    resolving unconstrained from `pyproject.toml`, so the deployed plugin could run
    versions nobody tested for as long as nobody touched the other file.

    The old schema's embedded `pkg` path was the other half of the problem: it made every
    plugin-cache relocation read as a new build, which is why unchanged-dependency
    upgrades kept killing kernels. Raises OSError where there is no source tree beside the
    install — a `--no-editable` runtime cannot answer this question and must not guess.
    """
    def sha(name: str) -> str:
        return hashlib.sha256((PKG_ROOT / name).read_bytes()).hexdigest()
    return {"schema": 3, "pyproject_sha": sha("pyproject.toml"),
            "lock_sha": sha("uv.lock"), "src_sha": src_tree_sha()}


def build_id(payload: dict | None = None) -> str:
    """The directory name a build owns. Short because it is a path segment people read;
    12 hex is 48 bits over a handful of builds on one machine, not an isolation boundary."""
    p = stamp_payload() if payload is None else payload
    return hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest()[:12]


def build_venv_dir(bid: str) -> Path:
    return venvs_root() / bid


def runtime_venv() -> Path | None:
    """This process's own provisioned venv — None for a dev/checkout run."""
    p = Path(sys.prefix)
    return p if (p / ".ptc-version").exists() else None


def spawn_venv() -> Path:
    """The venv kernels are spawned from: our own build when we run provisioned; else the
    current source's provisioned build when one is standing (a checkout CLI after
    `ptc setup`); else the legacy shared directory (dev runs and the test fixtures that
    symlink it — no test home carries a provisioned build, so fixtures keep resolving to
    their symlink)."""
    rv = runtime_venv()
    if rv is not None:
        return rv
    try:
        bd = build_venv_dir(build_id())
    except OSError:
        return venv_dir()
    return bd if (bd / "bin" / "python").exists() else venv_dir()


def venv_python() -> Path:
    return spawn_venv() / "bin" / "python"


def _identity_of(venv_path: Path) -> str | None:
    """The identity of the build standing at `venv_path`, read from its own stamp.

    Schema >= 3 stamps identify as build_id(payload); older stamps keep the legacy
    identity (sha256 of the raw text) so pre-v0.3 meta.json comparisons still hold.
    None where no stamp exists at all: a dev run, a hand-made venv, a test fixture, or the
    instants a provision spends before it writes its stamp. None of those is an upgrade.
    """
    try:
        text = (venv_path / ".ptc-version").read_text()
    except OSError:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("schema"), int) \
            and payload["schema"] >= 3:
        return build_id(payload)
    return hashlib.sha256(text.encode()).hexdigest()


def build_identity() -> str | None:
    """Which build THIS process (and the kernels it spawns) runs from.

    Read from the venv's own stamp rather than recomputed from source: the live payload
    changes the moment the package sources do, whether or not anybody has rebuilt
    anything, and a `--no-editable` runtime has no source to recompute from at all.
    """
    return _identity_of(spawn_venv())


def _stamp_matches(vd: Path, payload: dict) -> bool:
    """Does the build already standing at `vd` carry exactly this payload? The directory
    name is only 12 hex of the answer; the stamp is the whole of it."""
    try:
        return json.loads((vd / ".ptc-version").read_text()) == payload
    except (json.JSONDecodeError, OSError):
        return False


def stamp_current() -> bool:
    """Is the CURRENT SOURCE's build already provisioned? Provisioner-side only — it needs
    the source tree, so a `--no-editable` runtime gets False meaning "cannot know", which
    is why callers that report to a human ask stamp_payload() themselves first."""
    try:
        payload = stamp_payload()
    except OSError:
        return False
    vd = build_venv_dir(build_id(payload))
    return (vd / "bin" / "python").exists() and _stamp_matches(vd, payload)


def ensure_venv(run=subprocess.run) -> Path:
    try:
        payload = stamp_payload()
    except OSError as e:
        raise RuntimeError(
            "cannot provision: no source tree beside this install (missing "
            f"{e.filename}) — provisioning happens in the launcher (bin/ptc-launch) "
            "or a checkout, never from a --no-editable runtime") from e
    bid = build_id(payload)
    vd = build_venv_dir(bid)
    lock = ptc_home() / "provision.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    import time
    polls = 1200                       # 10 min at 0.5 s
    while True:
        if (vd / "bin" / "python").exists() and _stamp_matches(vd, payload):
            return vd / "bin" / "python"
        try:
            lock.mkdir()  # mkdir-based lock, matches ptc-launch
        except FileExistsError:
            # Take-the-lock retry, not wait-then-recheck: under per-build directories the
            # holder is usually provisioning a DIFFERENT build (old and new adapters
            # overlap across every rollout), so a cleared lock says nothing about ours —
            # keep trying to TAKE it ourselves until the budget runs out.
            if polls <= 0:
                raise RuntimeError(
                    "venv provisioning lock held and build still absent; "
                    f"remove {lock} if no other ptc process is running")
            polls -= 1
            time.sleep(0.5)
            continue
        try:
            # Re-checked under the lock: the holder we queued behind may have been
            # provisioning this very build.
            if not ((vd / "bin" / "python").exists() and _stamp_matches(vd, payload)):
                uv = _uv()
                # --clear is harmless on a fresh directory and load-bearing on a retry over
                # a half-provisioned one (uv refuses to create over an existing venv). It
                # never touches another build: this directory is named for THIS payload.
                run([uv, "venv", str(vd), "--python", "3.12", "--seed", "--clear"],
                    check=True)
                # `uv sync --locked` installs the versions in the checked-in `uv.lock` and
                # fails loudly if that lock no longer matches `pyproject.toml` — the honest
                # failure, at provisioning time, rather than a kernel quietly running a
                # resolution nobody tested. `--inexact` leaves the seeded pip alone (a bare
                # sync removes anything the lock does not name); `--no-dev` keeps the test
                # group out of a user's runtime; `--no-editable` copies the project into
                # the venv so the build outlives the source directory it came from — the
                # self-containment half of survivability.
                run([uv, "sync", "--locked", "--inexact", "--no-dev", "--no-editable",
                     "--extra", "kernel", "--project", str(PKG_ROOT)],
                    check=True, env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(vd)})
                # The stamp goes in LAST: it is what marks the directory complete, so a
                # provision that dies midway leaves a build nothing mistakes for finished.
                (vd / ".ptc-version").write_text(json.dumps(payload))
        finally:
            lock.rmdir()
        return vd / "bin" / "python"
