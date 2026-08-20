import json
from ptc import venv
from ptc.paths import venv_dir


def _fake_run_factory(calls):
    def fake_run(cmd, **kw):
        calls.append(cmd)
        # simulate uv creating the python binary on `uv venv`
        if cmd[1] == "venv":
            p = venv_dir() / "bin"
            p.mkdir(parents=True, exist_ok=True)
            (p / "python").write_text("#!fake\n")
        class R: returncode = 0
        return R()
    return fake_run


def test_provisions_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    calls: list = []
    py = venv.ensure_venv(run=_fake_run_factory(calls))
    assert py == venv_dir() / "bin" / "python"
    assert any(c[1] == "venv" for c in calls)
    assert any("pip" in c for c in calls)          # uv pip install -e .[kernel]
    stamp = json.loads((venv_dir() / ".ptc-version").read_text())
    assert stamp["schema"] == 1 and "pyproject_sha" in stamp


def test_skips_when_stamp_current(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    calls: list = []
    venv.ensure_venv(run=_fake_run_factory(calls))
    calls.clear()
    venv.ensure_venv(run=_fake_run_factory(calls))
    assert calls == []


def test_reprovisions_on_stamp_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    calls: list = []
    venv.ensure_venv(run=_fake_run_factory(calls))
    (venv_dir() / ".ptc-version").write_text('{"schema": 0}')
    calls.clear()
    venv.ensure_venv(run=_fake_run_factory(calls))
    assert any(c[1] == "venv" for c in calls)
