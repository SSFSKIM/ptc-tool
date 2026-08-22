"""CLI commands that are not about running a cell: `kill --all` and `doctor`.

Both are exercised in-process (main(argv)) with the kernel layer faked — what is under
test is the command's own contract, not a kernel round trip (test/integration/test_cli.py
covers that).
"""
import json
from pathlib import Path

import pytest

from ptc import cli
from ptc.kernel import KernelInfo
from ptc.ownership import UnknownOwner


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
    """The epilog and the README both define exit 1 for "a kill that found nothing", and
    `--all` is a kill: reporting 0 told shell automation a no-op cleanup had succeeded."""
    _no_session(monkeypatch)
    monkeypatch.setattr(cli, "list_kernels", lambda: [])
    monkeypatch.setattr(cli, "kill_kernel", lambda k: True)

    assert cli.main(["kill", "--all"]) == 1
    assert "(no kernels to kill)" in capsys.readouterr().out


def test_kill_all_that_killed_nothing_live_also_exits_one(monkeypatch, capsys):
    """Kernels were listed but every one of them was already gone (ownership check failed,
    a recycled pid): still nothing killed, still exit 1."""
    _no_session(monkeypatch)
    monkeypatch.setattr(cli, "list_kernels", lambda: [{"key": "k1"}, {"key": "k2"}])
    monkeypatch.setattr(cli, "kill_kernel", lambda k: False)

    assert cli.main(["kill", "--all"]) == 1
    assert "(no kernels to kill)" in capsys.readouterr().out


def test_kill_all_json_keeps_the_same_exit_code(monkeypatch, capsys):
    """The machine form reports the same outcome as the text form — the exit code is the
    part shell automation actually branches on."""
    _no_session(monkeypatch)
    monkeypatch.setattr(cli, "list_kernels", lambda: [])
    monkeypatch.setattr(cli, "kill_kernel", lambda k: True)

    assert cli.main(["kill", "--all", "--json"]) == 1
    assert _json_out(capsys) == {"all": True, "killed": [], "unverified": []}


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


# -- --json is advertised on every subcommand, so every subcommand must honor it ---------
# The shared parser puts `--json` on all nine commands, but only exec/wait ever read it:
# `ptc list --json`, `ptc doctor --json` and the lifecycle commands went on printing human
# text, so a caller that selected the accepted machine-readable option got something it
# could not parse. Human output is the default and is unchanged — the tests above are the
# guard on that half.

ROWS = [{"key": "k1", "pid": 11, "alive": True, "cwd": "/proj", "depth": 0,
         "spawned_at": 1.0, "last_used": 2.0}]


def _json_out(capsys) -> dict:
    out = capsys.readouterr().out
    assert out.count("\n") == 1, f"--json must emit one JSON document, got: {out!r}"
    return json.loads(out)


def test_list_json_carries_the_kernel_rows(monkeypatch, capsys):
    _no_session(monkeypatch)
    monkeypatch.setattr(cli, "list_kernels", lambda: ROWS)

    assert cli.main(["list", "--json"]) == 0

    d = _json_out(capsys)
    assert d["kernels"] == ROWS


def test_list_json_on_an_empty_machine_is_still_a_document(monkeypatch, capsys):
    """The human form prints nothing at all when there is no kernel; the machine form must
    still be parseable rather than empty."""
    _no_session(monkeypatch)
    monkeypatch.setattr(cli, "list_kernels", lambda: [])

    assert cli.main(["list", "--json"]) == 0
    assert _json_out(capsys) == {"kernels": []}


def test_setup_json_names_the_venv(monkeypatch, capsys):
    monkeypatch.setattr(cli, "ensure_venv", lambda: "/somewhere/python")

    assert cli.main(["setup", "--json"]) == 0
    assert _json_out(capsys) == {"venv": "/somewhere/python"}


def test_doctor_json_is_the_same_report_on_one_line(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "stamp_current", lambda: True)

    assert cli.main(["doctor", "--json"]) == 0
    d = _json_out(capsys)
    assert d["venv_ready"] is True and d["venv"].endswith("/venv/bin/python")
    assert "setup_would" in d and "uv" in d


def test_kill_all_json_lists_what_it_killed(monkeypatch, capsys):
    _no_session(monkeypatch)
    monkeypatch.setattr(cli, "list_kernels", lambda: [{"key": "k1"}, {"key": "k2"}])
    monkeypatch.setattr(cli, "kill_kernel", lambda k: k != "k2")

    assert cli.main(["kill", "--all", "--json"]) == 0
    assert _json_out(capsys) == {"all": True, "killed": ["k1"], "unverified": []}


def test_kill_all_skips_a_kernel_whose_ownership_cannot_be_read(monkeypatch, capsys):
    """`--all` kills every kernel whose ownership CHECKS OUT. One whose identity could not
    be read does not check out — it is named and left alone, and it must not abort the
    sweep over the kernels that can be verified."""
    def kill(k):
        if k == "k1":
            raise UnknownOwner("cannot tell whether pid 7 is still running")
        return True

    _no_session(monkeypatch)
    monkeypatch.setattr(cli, "list_kernels", lambda: [{"key": "k1"}, {"key": "k2"}])
    monkeypatch.setattr(cli, "kill_kernel", kill)

    assert cli.main(["kill", "--all", "--json"]) == 0
    assert _json_out(capsys) == {"all": True, "killed": ["k2"], "unverified": ["k1"]}


def test_an_unidentifiable_owner_is_a_sentence_not_a_traceback(monkeypatch, capsys):
    """A command that declined to act on a kernel it could not identify changed nothing;
    that is a state report for the user, and it exits nonzero without a stack trace."""
    def kill(k):
        raise UnknownOwner("cannot tell whether pid 7 is still running")

    monkeypatch.setattr(cli, "_pick_session", lambda e: ("k9", None, None))
    monkeypatch.setattr(cli, "kill_kernel", kill)

    assert cli.main(["kill", "-s", "k9"]) == 1
    out = capsys.readouterr()
    assert "cannot tell whether pid 7" in out.err and out.out == ""


def test_kill_one_json_reports_the_outcome_and_keeps_the_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_pick_session", lambda e: ("k9", None, None))
    monkeypatch.setattr(cli, "kill_kernel", lambda k: False)

    assert cli.main(["kill", "-s", "k9", "--json"]) == 1, "the exit code still reports it"
    assert _json_out(capsys) == {"key": "k9", "killed": False}


def test_interrupt_json(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_pick_session", lambda e: ("k9", None, None))
    monkeypatch.setattr(cli.KernelClient, "interrupt", lambda self: None)

    assert cli.main(["interrupt", "-s", "k9", "--json"]) == 0
    assert _json_out(capsys) == {"key": "k9", "interrupted": True}


def test_restart_json_names_the_respawned_kernel(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_pick_session", lambda e: ("k9", None, None))
    monkeypatch.setattr(cli, "read_meta", lambda k: {"cwd": "/proj", "claude_session_id": "s"})
    monkeypatch.setattr(cli, "restart_kernel",
                        lambda k, **kw: KernelInfo(k, 4242, Path("/c.json"), True, None))

    assert cli.main(["restart", "-s", "k9", "--json"]) == 0
    d = _json_out(capsys)
    assert d == {"key": "k9", "pid": 4242, "cwd": "/proj", "namespace_lost": True}


def test_restart_json_still_respawns_where_the_kernel_lived(monkeypatch, capsys):
    """The --json branch must not become a second, divergent code path: the stored cwd and
    session id travel with the respawn there too."""
    seen = {}
    monkeypatch.setattr(cli, "_pick_session", lambda e: ("k9", None, None))
    monkeypatch.setattr(cli, "read_meta", lambda k: {"cwd": "/proj", "claude_session_id": "s"})
    monkeypatch.setattr(cli, "restart_kernel",
                        lambda k, **kw: seen.update(kw) or
                        KernelInfo(k, 1, Path("/c.json"), True, None))

    cli.main(["restart", "-s", "k9", "--json"])
    assert seen == {"cwd": "/proj", "claude_session_id": "s"}


# --- r6 finding 9: a refused exec is not a successful exec ----------------------------

def _exec_returning(monkeypatch, outcome):
    from ptc import client

    monkeypatch.setattr(cli, "_pick_session", lambda e: ("k9", None,
                                                         type("R", (), {"cwd": None,
                                                                        "claude_session_id": None})()))
    monkeypatch.setattr(cli, "ensure_kernel",
                        lambda k, **kw: KernelInfo(k, 1, Path("/c.json"), False, None))
    monkeypatch.setattr(client.KernelClient, "exec_cell", lambda self, *a, **kw: outcome)


def test_exec_refused_as_busy_exits_nonzero(monkeypatch, capsys):
    """`exec_cell` returning Busy means the kernel ran NOTHING and queued nothing, but the
    CLI exited 0 — so a shell chain, a Makefile or CI treated a dropped command as one that
    had executed. Busy has its own code so a script can retry on it."""
    from ptc.client import Busy

    _exec_returning(monkeypatch, Busy(7, reason="running"))
    assert cli.main(["exec", "-s", "k9", "1+1"]) == cli.EXIT_BUSY
    assert cli.EXIT_BUSY != 0
    assert "busy" in capsys.readouterr().out


def test_exec_that_yielded_while_still_running_is_still_success(monkeypatch, capsys):
    """The distinction the code carries: a Running cell WAS submitted — the caller's yield
    budget ran out, not the work — so it keeps exit 0 and a cell id to wait on."""
    from ptc.client import Running

    _exec_returning(monkeypatch, Running(7, "partial\n", 8))
    assert cli.main(["exec", "-s", "k9", "1+1"]) == 0
    assert "still running" in capsys.readouterr().out


def test_the_busy_exit_code_is_documented_in_help():
    """An exit code nobody can look up is a private convention, not a contract."""
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out), pytest.raises(SystemExit):
        cli.main(["--help"])
    assert f"{cli.EXIT_BUSY} the kernel was busy" in " ".join(out.getvalue().split())
