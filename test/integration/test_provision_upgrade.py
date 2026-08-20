"""Real `uv` provisioning over an existing venv — the upgrade path, both twins.

`uv venv` refuses to create a virtual environment where one already exists (exit 2,
"hint: Use the `--clear` flag"). That is exactly the state every upgrade starts from:
the stamp mismatches, the venv is there. Unit tests that inject a fake `run` cannot see
this, so both provisioners are exercised against the real `uv` here.
"""
import importlib.util
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent.parent
LAUNCH = PKG / "plugin" / "bin" / "ptc-launch"


def _seed_existing_venv(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    subprocess.run(["uv", "venv", str(home / "venv"), "--python", "3.12"], check=True)


def _launcher_provision(home: Path):
    loader = SourceFileLoader("ptc_launch_upgrade", str(LAUNCH))
    mod = importlib.util.module_from_spec(
        importlib.util.spec_from_loader("ptc_launch_upgrade", loader))
    loader.exec_module(mod)          # reads PTC_HOME at import
    mod.provision()
    return mod.current()


def _library_provision(home: Path):
    from ptc import venv
    venv.ensure_venv()
    return venv.stamp_current()


@pytest.mark.parametrize("provision", [_launcher_provision, _library_provision],
                         ids=["ptc-launch", "ptc.venv"])
def test_provisions_over_an_existing_venv(tmp_path, monkeypatch, provision):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    _seed_existing_venv(tmp_path)
    assert provision(tmp_path), "stale venv must be replaced, not fail the start"
    subprocess.run([str(tmp_path / "venv" / "bin" / "python"), "-c",
                    "import ptc, ipykernel"], check=True)   # the install really happened
