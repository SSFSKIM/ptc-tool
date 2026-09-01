"""Attach gates for the immutable-venv scheme (v0.3 i1): a live kernel is recycled ONLY
when its venv is gone or its protocol mismatches; a build difference is a notice."""
from pathlib import Path

import pytest

import ptc.client
import ptc.kernel as kernel
import ptc.venv
from ptc.discovery import read_meta, write_meta
from ptc.paths import PTC_PROTOCOL


def _meta(monkeypatch, tmp_path, **fields):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    write_meta("k", **fields)


def test_venv_gone_fires_only_when_recorded_venv_missing(monkeypatch, tmp_path):
    _meta(monkeypatch, tmp_path, venv=str(tmp_path / "venvs" / "dead"))
    assert "no longer exists" in kernel._venv_gone("k")
    live = tmp_path / "venvs" / "alive"
    live.mkdir(parents=True)
    _meta(monkeypatch, tmp_path, venv=str(live))
    assert kernel._venv_gone("k") is None
    _meta(monkeypatch, tmp_path, venv=None)          # no record: dev spawn, no claim
    assert kernel._venv_gone("k") is None


def test_protocol_absent_reads_zero_and_mismatches(monkeypatch, tmp_path):
    _meta(monkeypatch, tmp_path, cwd="/x")           # no protocol field
    note = kernel._protocol_mismatch("k")
    assert "protocol 0" in note and f"current {PTC_PROTOCOL}" in note
    _meta(monkeypatch, tmp_path, protocol=PTC_PROTOCOL)
    assert kernel._protocol_mismatch("k") is None


def test_build_note_names_both_builds(monkeypatch, tmp_path):
    _meta(monkeypatch, tmp_path, build="aaaaaaaaaaaa")
    monkeypatch.setattr(kernel, "build_identity", lambda: "bbbbbbbbbbbb")
    note = kernel._build_note("k")
    assert "aaaaaaaaaaaa" in note and "bbbbbbbbbbbb" in note and "restart()" in note
    monkeypatch.setattr(kernel, "build_identity", lambda: "aaaaaaaaaaaa")
    assert kernel._build_note("k") is None
    monkeypatch.setattr(kernel, "build_identity", lambda: None)
    assert kernel._build_note("k") is None


def test_spawn_records_the_venv_it_launched_from(monkeypatch, tmp_path):
    """The spawn resolved spawn_venv() twice — once for the Popen argv, once for
    write_meta after bootstrap — so a provision landing between them recorded a venv the
    kernel never launched from. That never self-heals: it falsifies _venv_gone and GC's
    reference set for the kernel's whole life. Both readings here return DIFFERENT paths,
    so the recorded venv can only match the argv if the resolution happened once."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    seq = iter([tmp_path / "venv-a", tmp_path / "venv-b"])
    # Both bindings a resolution can go through — kernel's own, and the one venv_python()
    # reads — draw from the same two-path sequence, so a SECOND resolution anywhere in
    # the spawn returns a different directory than the first.
    monkeypatch.setattr(kernel, "spawn_venv", lambda: next(seq))
    monkeypatch.setattr(ptc.venv, "spawn_venv", lambda: next(seq))
    monkeypatch.setattr(kernel, "build_identity", lambda: "aaaaaaaaaaaa")

    class _Proc:
        pid = 424242

    argv: list[list[str]] = []
    monkeypatch.setattr(kernel.subprocess, "Popen",
                        lambda cmd, **kw: (argv.append(cmd), _Proc())[1])
    monkeypatch.setattr(kernel, "proc_start_time", lambda pid: "birth")
    monkeypatch.setattr(kernel, "_wait_ports", lambda conn: Path(conn).write_text("{}"))
    monkeypatch.setattr(kernel, "_kernel_info_roundtrip", lambda conn: None)
    monkeypatch.setattr(kernel, "kill_process_tree", lambda pid: None)
    # Stop the spawn at the first step PAST write_meta: meta.json survives the abort
    # (the handler clears owner/ready/connection only), so the record can be read back.
    def _stop(key, cfg):
        raise KeyboardInterrupt("bootstrap stopped: meta is already written")
    monkeypatch.setattr(ptc.client, "run_bootstrap", _stop)

    with pytest.raises(KeyboardInterrupt):
        kernel.ensure_kernel("k", cwd=str(tmp_path))

    assert len(argv) == 1
    launched_from = Path(argv[0][0]).parent.parent            # <venv>/bin/python
    assert read_meta("k")["venv"] == str(launched_from)
