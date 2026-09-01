"""Attach gates for the immutable-venv scheme (v0.3 i1): a live kernel is recycled ONLY
when its venv is gone or its protocol mismatches; a build difference is a notice."""
import ptc.kernel as kernel
from ptc.discovery import write_meta
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
