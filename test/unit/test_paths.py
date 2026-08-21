from pathlib import Path

import pytest

from ptc.paths import Config, MAX_OUTPUT_CLAMP, kernel_dir, kernels_root, ptc_home, safe_key


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
