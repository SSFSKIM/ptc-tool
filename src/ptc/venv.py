"""Provision ~/.ptc/venv with uv. Stamp payload is shared with plugin/bin/ptc-launch."""
import hashlib
import json
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
    sha = hashlib.sha256((PKG_ROOT / "pyproject.toml").read_bytes()).hexdigest()
    return {"schema": 1, "pyproject_sha": sha, "pkg": str(PKG_ROOT)}


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
        run([uv, "venv", str(venv_dir()), "--python", "3.12", "--seed"], check=True)
        run([uv, "pip", "install", "--python", str(venv_python()),
             "-e", f"{PKG_ROOT}[kernel]"], check=True)
        (venv_dir() / ".ptc-version").write_text(json.dumps(stamp_payload()))
    finally:
        lock.rmdir()
    return venv_python()
