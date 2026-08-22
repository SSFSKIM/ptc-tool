"""Provision ~/.ptc/venv with uv. Stamp payload is shared with plugin/bin/ptc-launch."""
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
