import stat
from pathlib import Path

import pytest

from ptc.paths import (
    MAX_OUTPUT_CLAMP,
    Config,
    kernel_dir,
    kernels_root,
    private_open,
    private_write_text,
    ptc_home,
    safe_key,
    secure_dir,
)


def test_ptc_home_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "home"))
    assert ptc_home() == tmp_path / "home"
    assert kernel_dir("abc") == tmp_path / "home" / "kernels" / "abc"


def test_config_defaults():
    cfg = Config.from_env(env={})
    assert (cfg.yield_s, cfg.max_output_chars, cfg.idle_hours) == (300.0, 12_000, 24.0)
    assert (cfg.max_concurrency, cfg.max_depth, cfg.depth) == (8, 1, 0)
    assert cfg.session is None


def test_config_env_and_clamp():
    env = {"PTC_YIELD_S": "5", "PTC_MAX_OUTPUT_CHARS": "999999", "PTC_DEPTH": "2",
           "PTC_SESSION": "abc", "PTC_IDLE_HOURS": "0.01", "PTC_MAX_OUTPUT_CHARS_BAD": "x"}
    cfg = Config.from_env(env=env)
    assert cfg.yield_s == 5.0
    assert cfg.max_output_chars == MAX_OUTPUT_CLAMP  # clamped
    assert cfg.depth == 2 and cfg.session == "abc" and cfg.idle_hours == 0.01


def test_config_bad_values_fall_back():
    assert Config.from_env(env={"PTC_YIELD_S": "banana"}).yield_s == 300.0


def test_safe_key():
    assert safe_key("96abe6e2-80aa") == "96abe6e2-80aa"
    assert safe_key("a/b c!") == "a-b-c-"


def test_safe_key_neutralizes_dot_segments(monkeypatch, tmp_path):
    """"." and ".." survived sanitization as themselves, so kernel_dir() resolved to the
    kernels root or its PARENT — every lifecycle write (owner.json, cells/, the group
    kill on restart) then landed outside the key's own namespace."""
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "home"))
    for raw in (".", "..", "", "...", "./", "../"):
        key = safe_key(raw)
        assert key not in ("", ".", "..")
        d = kernel_dir(key)
        assert d.parent == kernels_root(), (raw, key, d)
        assert safe_key(key) == key, "safe_key must be idempotent"


def test_kernel_dir_rejects_a_key_that_is_not_a_name(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "home"))
    for bad in ("", ".", "..", "../escape", "a/b"):
        with pytest.raises(ValueError, match="unsafe kernel key"):
            kernel_dir(bad)


def test_zero_or_negative_concurrency_is_rejected_at_parse():
    """PTC_MAX_CONCURRENCY=0 parsed fine and then built an asyncio.Semaphore(0): no permit
    is ever released, so every agent/llm/web call blocks forever with its kernel cell still
    in flight. A deadlock that looks like a hung kernel becomes a legible error here —
    the same call the workflow-level `limit` makes."""
    for bad in ("0", "-1"):
        with pytest.raises(ValueError, match="PTC_MAX_CONCURRENCY"):
            Config.from_env(env={"PTC_MAX_CONCURRENCY": bad})
    # unparseable is not the same as invalid: that still falls back to the default
    assert Config.from_env(env={"PTC_MAX_CONCURRENCY": "banana"}).max_concurrency == 8


def test_secure_dir_creates_and_repairs_owner_only_directories(monkeypatch, tmp_path):
    """Every ancestor PTC creates is owner-only, not just the leaf, and an existing
    directory from a laxer umask is repaired in place."""
    home = tmp_path / "home"
    monkeypatch.setenv("PTC_HOME", str(home))
    d = secure_dir(kernels_root() / "k" / "cells")
    for p in (d, d.parent, kernels_root(), home):
        assert stat.S_IMODE(p.stat().st_mode) == 0o700, p

    d.chmod(0o755)
    kernels_root().chmod(0o755)
    secure_dir(d)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE(kernels_root().stat().st_mode) == 0o700, "ancestors repair too"

    # never a directory outside PTC's own tree: tmp_path is somebody else's to set
    tmp_path.chmod(0o755)
    secure_dir(home)
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o755


def test_private_writes_create_owner_only_files(tmp_path):
    private_write_text(tmp_path / "f.json", '{"a": 1}')
    assert stat.S_IMODE((tmp_path / "f.json").stat().st_mode) == 0o600
    assert (tmp_path / "f.json").read_text() == '{"a": 1}'
    assert not list(tmp_path.glob("*.tmp")), "the temp file must be renamed, not left"

    with private_open(tmp_path / "log.txt", "a") as f:
        f.write("line\n")
    assert stat.S_IMODE((tmp_path / "log.txt").stat().st_mode) == 0o600
