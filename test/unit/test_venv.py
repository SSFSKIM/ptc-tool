import json
import subprocess
import time

import pytest
from ptc import venv
from ptc.paths import venv_dir


def _fake_run_factory(calls):
    def fake_run(cmd, **kw):
        calls.append(cmd)
        # simulate uv creating the python binary on `uv venv`
        if cmd[1] == "venv":
            p = venv_dir() / "bin"
            if p.exists() and "--clear" not in cmd:
                # uv 0.11: "A virtual environment already exists at: ..." (exit 2).
                # The re-provision path hits this on every upgrade, so the fake must
                # refuse exactly as uv does.
                raise subprocess.CalledProcessError(2, cmd)
            p.mkdir(parents=True, exist_ok=True)
            (p / "python").write_text("#!fake\n")
        class R: returncode = 0
        return R()
    return fake_run


def _must_not_provision(cmd, **kw):
    raise AssertionError("must not provision")


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


def test_waits_for_contended_lock_then_returns_without_provisioning(monkeypatch, tmp_path):
    """The lock dir already exists (another process is provisioning it). On
    our first poll it finishes: lock gone, venv python + a current stamp in
    place. We must pick that up and return without ever calling run."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    lock = tmp_path / "provision.lock"
    lock.mkdir(parents=True)

    sleep_calls: list = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        lock.rmdir()
        p = venv_dir() / "bin"
        p.mkdir(parents=True, exist_ok=True)
        (p / "python").write_text("#!fake\n")
        (venv_dir() / ".ptc-version").write_text(json.dumps(venv.stamp_payload()))

    monkeypatch.setattr(time, "sleep", fake_sleep)

    py = venv.ensure_venv(run=_must_not_provision)
    assert py == venv_dir() / "bin" / "python"
    assert sleep_calls == [0.5]  # broke out of the poll loop on the first check after sleeping


def test_raises_when_lock_contention_exceeds_budget(monkeypatch, tmp_path):
    """venv.py has no time.time()/time.monotonic() call: the 10-minute budget
    is a fixed 1200-iteration poll loop (1200 * 0.5s sleep), not a wall-clock
    check. A lock that never clears must exhaust every iteration and raise."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    lock = tmp_path / "provision.lock"
    lock.mkdir(parents=True)

    sleep_calls: list = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleep_calls.append(seconds))

    with pytest.raises(RuntimeError, match="lock"):
        venv.ensure_venv(run=_must_not_provision)
    assert len(sleep_calls) == 1200
