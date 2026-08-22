"""Provision ~/.ptc/venv with uv. Stamp payload is shared with bin/ptc-launch."""
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from .paths import ptc_home, venv_dir

PKG_ROOT = Path(__file__).resolve().parent.parent.parent  # .../ptc-surface/ptc


def venv_python() -> Path:
    return venv_dir() / "bin" / "python"


def _uv() -> str:
    return shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")


def stamp_payload() -> dict:
    """What the venv was built FROM, both files of it.

    `uv.lock` is half the answer and used to be no part of it: a dependency fix that
    changed only the lock left every existing venv reading as current and every fresh one
    resolving unconstrained from `pyproject.toml`, so the deployed plugin could run
    versions nobody tested for as long as nobody touched the other file.
    """
    def sha(name: str) -> str:
        return hashlib.sha256((PKG_ROOT / name).read_bytes()).hexdigest()
    return {"schema": 2, "pyproject_sha": sha("pyproject.toml"),
            "lock_sha": sha("uv.lock"), "pkg": str(PKG_ROOT)}


def build_identity() -> str | None:
    """Which BUILD of the shared venv a process is running from; None where there is none.

    The provisioners write `stamp_payload()` to `venv/.ptc-version` at the end of every
    provision and `--clear` the whole directory at the start of the next one, so this
    string changes exactly when the venv under a running kernel has been REPLACED. That is
    the question `ensure_kernel` asks before attaching to a long-lived kernel, and it is
    not the same question `stamp_current()` asks: the live payload changes the moment the
    package sources do, whether or not anybody has rebuilt anything, and recycling a
    kernel whose venv is still standing would throw a namespace away for nothing.

    Hashed rather than kept whole — meta.json wants an identity, not a copy of a payload
    carrying an absolute path. None where no stamp exists at all: a dev run, a hand-made
    venv, a test fixture, or the instants a provision spends between the clear and the new
    stamp. None of those is an upgrade. (A rebuild that lands on the SAME payload — a
    repair of a corrupted venv — is invisible here, as it is to every other reading of the
    stamp.)
    """
    try:
        text = (venv_dir() / ".ptc-version").read_text()
    except OSError:
        return None
    return hashlib.sha256(text.encode()).hexdigest()


def stamp_current() -> bool:
    stamp = venv_dir() / ".ptc-version"
    if not (venv_python().exists() and stamp.exists()):
        return False
    try:
        return json.loads(stamp.read_text()) == stamp_payload()
    except (json.JSONDecodeError, OSError):
        return False


def ensure_venv(run=subprocess.run) -> Path:
    if stamp_current():
        return venv_python()
    lock = ptc_home() / "provision.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()  # mkdir-based lock, matches ptc-launch
    except FileExistsError:
        # another provisioner is running; wait for it (up to 10 min), then re-check
        import time
        for _ in range(1200):
            if not lock.exists():
                break
            time.sleep(0.5)
        if stamp_current():
            return venv_python()
        raise RuntimeError("venv provisioning lock held and venv still stale; "
                           f"remove {lock} if no other ptc process is running")
    try:
        uv = _uv()
        # --clear: uv refuses to create a venv where one already exists, which is the
        # state every upgrade starts from. It still declines to wipe a non-venv directory.
        run([uv, "venv", str(venv_dir()), "--python", "3.12", "--seed", "--clear"],
            check=True)
        # `uv sync --locked` installs the versions in the checked-in `uv.lock` and fails
        # loudly if that lock no longer matches `pyproject.toml` — the honest failure, at
        # provisioning time, rather than a kernel quietly running a resolution nobody
        # tested. `--inexact` leaves the seeded pip alone (a bare sync removes anything the
        # lock does not name); `--no-dev` keeps the test group out of a user's runtime; the
        # project itself is installed editable, exactly as `uv pip install -e` had it.
        run([uv, "sync", "--locked", "--inexact", "--no-dev", "--extra", "kernel",
             "--project", str(PKG_ROOT)],
            check=True, env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv_dir())})
        (venv_dir() / ".ptc-version").write_text(json.dumps(stamp_payload()))
    finally:
        lock.rmdir()
    return venv_python()
