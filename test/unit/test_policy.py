"""The deny-policy core: user-owned, empty by default, loud when malformed."""
import json
import os
import time

import pytest
from ptc import policy


def _write(tmp_path, monkeypatch, obj) -> None:
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.delenv("PTC_POLICY", raising=False)
    p = tmp_path / "policy.json"
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj))


def test_absent_file_is_the_empty_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.delenv("PTC_POLICY", raising=False)
    assert policy.load_rules() is None
    assert policy.match("bash", "rm -rf /") is None
    assert policy.file_state() == "absent"


def test_valid_rules_load_and_match(monkeypatch, tmp_path):
    _write(tmp_path, monkeypatch, {"version": 1, "deny": [
        {"tools": ["bash"], "pattern": "^rm "},
        {"tools": ["write", "edit"], "path": "/etc/*"},
        {"tools": ["web_fetch"], "pattern": "internal\\.example"},
    ]})
    rules = policy.load_rules()
    assert [r.index for r in rules] == [0, 1, 2]
    assert policy.match("bash", "rm -rf /tmp/x").index == 0
    assert policy.match("bash", "echo rm") is None          # re.search, anchored by ^
    assert policy.match("edit", "/etc/hosts").index == 1
    assert policy.match("write", "/home/u/etc/hosts") is None
    assert policy.match("web_fetch", "https://internal.example/x").index == 2
    assert policy.match("llm", "anything") is None           # ungoverned tool names never match
    assert policy.file_state() == "active"


def test_empty_deny_is_empty_not_active(monkeypatch, tmp_path):
    _write(tmp_path, monkeypatch, {"version": 1, "deny": []})
    assert policy.load_rules() == []
    assert policy.file_state() == "empty"


@pytest.mark.parametrize("payload", [
    "not json at all",
    {"version": 2, "deny": []},
    {"version": 1, "deny": "nope"},
    {"version": 1, "deny": [{"tools": ["bash"]}]},              # no pattern and no path
    {"version": 1, "deny": [{"tools": ["bash"], "pattern": "("}]},   # uncompilable regex
    {"version": 1},                                              # deny missing entirely
    {"version": 1, "deny": [{"tools": ["bash"], "pattern": 5}]},        # pattern not a string
    {"version": 1, "deny": [{"tools": ["bash"], "pattern": ["^rm"]}]},  # pattern a list
    {"version": 1, "deny": [{"tools": ["write"], "path": 5}]},          # path not a string
    {"version": 1, "deny": [{"tools": [], "pattern": "^rm"}]},          # names no tool, cannot fire
])
def test_malformed_shapes_raise_policy_error_and_state_malformed(monkeypatch, tmp_path, payload):
    _write(tmp_path, monkeypatch, payload)
    with pytest.raises(policy.PolicyError):
        policy.load_rules()
    assert policy.file_state() == "malformed"


def test_unknown_tool_name_is_loud_not_silent_false_protection(monkeypatch, tmp_path):
    # A typo'd name can never match, so loading it would be protection that cannot fire.
    _write(tmp_path, monkeypatch, {"version": 1, "deny": [{"tools": ["bsah"], "pattern": "^rm "}]})
    with pytest.raises(policy.PolicyError, match="bsah"):
        policy.load_rules()
    assert policy.file_state() == "malformed"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
def test_unreadable_file_is_malformed_not_a_raw_oserror(monkeypatch, tmp_path):
    _write(tmp_path, monkeypatch, {"version": 1, "deny": [{"tools": ["bash"], "pattern": "^rm "}]})
    p = tmp_path / "policy.json"
    p.chmod(0o000)
    try:
        with pytest.raises(policy.PolicyError):
            policy.load_rules()
        assert policy.file_state() == "malformed"
    finally:
        p.chmod(0o600)


def test_mtime_cache_reloads_on_change(monkeypatch, tmp_path):
    _write(tmp_path, monkeypatch, {"version": 1, "deny": [{"tools": ["bash"], "pattern": "^a"}]})
    assert policy.match("bash", "abc") is not None
    p = tmp_path / "policy.json"
    p.write_text(json.dumps({"version": 1, "deny": [{"tools": ["bash"], "pattern": "^z"}]}))
    now = time.time() + 5
    os.utime(p, (now, now))                                # force a visible mtime step
    assert policy.match("bash", "abc") is None
    assert policy.match("bash", "zzz") is not None


def test_ptc_policy_env_overrides_the_path(monkeypatch, tmp_path):
    alt = tmp_path / "elsewhere.json"
    alt.write_text(json.dumps({"version": 1, "deny": [{"tools": ["bash"], "pattern": "x"}]}))
    monkeypatch.setenv("PTC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PTC_POLICY", str(alt))
    assert policy.policy_path() == alt
    assert policy.match("bash", "x marks") is not None
