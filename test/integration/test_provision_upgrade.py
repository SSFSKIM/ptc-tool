"""Real `uv` provisioning of versioned builds — both provisioner twins.

Three proofs a fake `run` cannot give: (a) a build provisions into venvs/<build_id>/ and
imports work; (b) a source change provisions a SECOND build and leaves the first standing
(the survivability core); (c) --no-editable really copies the project — the venv keeps
working after its source tree is deleted (the self-containment acceptance).
"""
import importlib.util
import shutil
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent.parent


def _copy_minimal_source(dst: Path) -> Path:
    """A real, provisionable copy of this package: manifest, lock, source, launcher."""
    dst.mkdir(parents=True)
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy(PKG / name, dst / name)
    shutil.copytree(PKG / "src", dst / "src")
    shutil.copytree(PKG / "bin", dst / "bin")
    return dst


def _launcher_provision(src_root: Path):
    loader = SourceFileLoader("ptc_launch_v3", str(src_root / "bin" / "ptc-launch"))
    mod = importlib.util.module_from_spec(
        importlib.util.spec_from_loader("ptc_launch_v3", loader))
    loader.exec_module(mod)          # reads PTC_HOME at import
    mod.provision()
    assert mod.current()
    return mod.venv_dir()


def _library_provision(src_root: Path):
    import ptc.venv as pv
    old = pv.PKG_ROOT
    pv.PKG_ROOT = src_root
    try:
        py = pv.ensure_venv()
        assert pv.stamp_current()
        return py.parent.parent
    finally:
        pv.PKG_ROOT = old


@pytest.mark.parametrize("provision", [_launcher_provision, _library_provision],
                         ids=["ptc-launch", "ptc.venv"])
def test_versioned_provision_survives_source_deletion(tmp_path, monkeypatch, provision):
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "home"))
    src = _copy_minimal_source(tmp_path / "cache" / "pkg")
    vd = provision(src)
    assert vd.parent == tmp_path / "home" / "venvs"
    py = vd / "bin" / "python"
    subprocess.run([str(py), "-c", "import ptc, ipykernel"], check=True)
    # (c) self-containment: the plugin cache is pruned; the build must not care
    shutil.rmtree(src)
    subprocess.run([str(py), "-c", "import ptc, ptc.runtime.bootstrap"], check=True)


def test_source_change_provisions_beside_not_over(tmp_path, monkeypatch):
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "home"))
    src = _copy_minimal_source(tmp_path / "cache" / "pkg")
    old_vd = _launcher_provision(src)
    marker = old_vd / "bin" / "python"
    stamp_before = (old_vd / ".ptc-version").read_text()
    (src / "src" / "ptc" / "__init__.py").write_text("VERSION = 'changed'\n")
    new_vd = _launcher_provision(src)
    assert new_vd != old_vd
    assert marker.exists(), "the old build must remain standing"
    assert (old_vd / ".ptc-version").read_text() == stamp_before
