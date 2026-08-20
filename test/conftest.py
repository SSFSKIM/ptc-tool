import os
import subprocess
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
CACHE_VENV = PKG / ".venv-kernel"


@pytest.fixture(scope="session")
def kernel_venv() -> Path:
    """A real venv with ipykernel + ptc (editable), cached across runs."""
    py = CACHE_VENV / "bin" / "python"
    marker = CACHE_VENV / ".ok"
    if not (py.exists() and marker.exists()):
        uv = os.environ.get("UV", "uv")
        subprocess.run([uv, "venv", str(CACHE_VENV), "--python", "3.12", "--seed"], check=True)
        subprocess.run([uv, "pip", "install", "--python", str(py), "-e", f"{PKG}[kernel]"], check=True)
        marker.write_text("ok")
    return CACHE_VENV


@pytest.fixture
def ptc_home(tmp_path, monkeypatch, kernel_venv) -> Path:
    """Isolated PTC_HOME whose venv/ is the cached real venv."""
    home = tmp_path / "ptc-home"
    home.mkdir()
    (home / "venv").symlink_to(kernel_venv)
    (home / "venv" / ".ptc-version").exists()  # stamp not needed: tests bypass ensure_venv
    monkeypatch.setenv("PTC_HOME", str(home))
    monkeypatch.delenv("PTC_SESSION", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return home
