import hashlib
import os
import subprocess
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
CACHE_VENV = PKG / ".venv-kernel"


@pytest.fixture(scope="session")
def kernel_venv() -> Path:
    """A real venv with ipykernel + ptc (editable), cached across runs.

    The cache is keyed on pyproject.toml's contents: the .ok marker stores the
    hash it was provisioned with, and a change invalidates the cache.
    """
    py = CACHE_VENV / "bin" / "python"
    marker = CACHE_VENV / ".ok"
    sha = hashlib.sha256((PKG / "pyproject.toml").read_bytes()).hexdigest()
    if not (py.exists() and marker.exists() and marker.read_text() == sha):
        uv = os.environ.get("UV", "uv")
        subprocess.run([uv, "venv", str(CACHE_VENV), "--python", "3.12", "--seed"], check=True)
        subprocess.run([uv, "pip", "install", "--python", str(py), "-e", f"{PKG}[kernel]"], check=True)
        marker.write_text(sha)
    return CACHE_VENV


@pytest.fixture(autouse=True)
def _reap_kernels(tmp_path):
    """Kill every kernel a test spawned under its own PTC_HOME, however it ended.

    Tests kill their kernels on the happy path, but a failed assertion skips that line
    and leaves a detached kernel running for the rest of the machine's uptime. Resolve
    PTC_HOME from tmp_path rather than the environment: monkeypatch's env teardown may
    already have run by the time this finalizer does.
    """
    yield
    root = tmp_path / "ptc-home" / "kernels"
    if not root.is_dir():
        return
    prev = os.environ.get("PTC_HOME")
    os.environ["PTC_HOME"] = str(tmp_path / "ptc-home")
    try:
        from ptc.kernel import kill_kernel
        for kd in root.iterdir():
            if kd.is_dir():
                try:
                    kill_kernel(kd.name)
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        if prev is None:
            os.environ.pop("PTC_HOME", None)
        else:
            os.environ["PTC_HOME"] = prev


@pytest.fixture
def ptc_home(tmp_path, monkeypatch, kernel_venv) -> Path:
    """Isolated PTC_HOME whose venv/ is the cached real venv."""
    home = tmp_path / "ptc-home"
    home.mkdir()
    (home / "venv").symlink_to(kernel_venv)
    monkeypatch.setenv("PTC_HOME", str(home))
    monkeypatch.delenv("PTC_SESSION", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return home
