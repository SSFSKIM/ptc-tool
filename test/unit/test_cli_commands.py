"""CLI commands that are not about running a cell: `kill --all` and `doctor`.

Both are exercised in-process (main(argv)) with the kernel layer faked — what is under
test is the command's own contract, not a kernel round trip (test/integration/test_cli.py
covers that).
"""
import json

import pytest

from ptc import cli


def _no_session(monkeypatch):
    def boom(_explicit):
        raise AssertionError("this command must not need a session")
    monkeypatch.setattr(cli, "_pick_session", boom)


def test_kill_all_kills_every_known_kernel(monkeypatch, capsys):
    """`ptc kill --all` is documented in the spec's CLI table; it used to be documented
    only. It also must not go through session discovery — "all" is not one session."""
    _no_session(monkeypatch)
    killed = []
    monkeypatch.setattr(cli, "list_kernels",
                        lambda: [{"key": "k1"}, {"key": "k2"}, {"key": "k3"}])
    monkeypatch.setattr(cli, "kill_kernel", lambda k: killed.append(k) or True)

    assert cli.main(["kill", "--all"]) == 0

    assert killed == ["k1", "k2", "k3"]
    out = capsys.readouterr().out
    assert "[killed k1]" in out and "[killed k3]" in out


def test_kill_all_with_nothing_to_kill_says_so(monkeypatch, capsys):
    _no_session(monkeypatch)
    monkeypatch.setattr(cli, "list_kernels", lambda: [])
    monkeypatch.setattr(cli, "kill_kernel", lambda k: True)

    assert cli.main(["kill", "--all"]) == 0
    assert "(no kernels to kill)" in capsys.readouterr().out


def test_kill_one_still_targets_the_picked_session(monkeypatch, capsys):
    killed = []
    monkeypatch.setattr(cli, "_pick_session", lambda e: ("k9", None, None))
    monkeypatch.setattr(cli, "kill_kernel", lambda k: killed.append(k) or True)

    assert cli.main(["kill", "-s", "k9"]) == 0
    assert killed == ["k9"]


def test_doctor_inspects_and_never_provisions(monkeypatch, tmp_path, capsys):
    """doctor is an INSPECTION command: it reports what setup would do and changes
    nothing. Calling ensure_venv() made the diagnostic rebuild the venv it was asked
    about — minutes of network, triggered by a question."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "home"))

    def boom(*a, **kw):
        raise AssertionError("doctor provisioned the venv")
    monkeypatch.setattr(cli, "ensure_venv", boom)

    assert cli.main(["doctor"]) == 0

    d = json.loads(capsys.readouterr().out)
    assert d["venv_ready"] is False
    assert "ptc setup" in d["setup_would"]
    assert d["venv"].endswith("/venv/bin/python")
    assert not (tmp_path / "home" / "venv").exists(), "doctor created state"


def test_doctor_reports_a_current_venv(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "stamp_current", lambda: True)

    assert cli.main(["doctor"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["venv_ready"] is True and "nothing" in d["setup_would"]


def test_setup_still_provisions(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "ensure_venv", lambda: calls.append(1) or "/somewhere/python")

    assert cli.main(["setup"]) == 0
    assert calls, "setup is the command that provisions"
    assert "ptc venv ready" in capsys.readouterr().out


def test_kill_all_flag_is_only_on_kill():
    with pytest.raises(SystemExit):
        cli.main(["list", "--all"])
