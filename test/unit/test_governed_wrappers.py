"""Deny rules trip the governed wrappers; denials are audited; malformed is loud."""
import json

import pytest


@pytest.fixture
def governed(tmp_path, monkeypatch):
    """A kernel-side STATE rooted in tmp, with a policy file the test writes."""
    from ptc.runtime.state import STATE
    monkeypatch.setenv("PTC_HOME", str(tmp_path))
    monkeypatch.delenv("PTC_POLICY", raising=False)
    kd = tmp_path / "kernels" / "gov"
    (kd / "cells").mkdir(parents=True)
    monkeypatch.setattr(STATE, "kernel_dir", kd)
    monkeypatch.setattr(STATE, "current_cell", 1)
    monkeypatch.setattr(STATE, "cell_mutations", [])
    def write_policy(obj):
        (tmp_path / "policy.json").write_text(
            obj if isinstance(obj, str) else json.dumps(obj))
    return write_policy


def _audit_lines(tmp_path):
    p = tmp_path / "kernels" / "gov" / "audit.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_denied_write_raises_and_audits(governed, tmp_path):
    from ptc.runtime import files
    governed({"version": 1, "deny": [{"tools": ["write", "edit"], "path": str(tmp_path / "sec" / "*")}]})
    with pytest.raises(PermissionError, match="rule 0"):
        files.write(str(tmp_path / "sec" / "x.txt"), "data")
    assert not (tmp_path / "sec" / "x.txt").exists()
    denied = [e for e in _audit_lines(tmp_path) if e["kind"] == "denied"]
    assert denied and denied[0]["tool"] == "write" and denied[0]["rule"] == 0


def test_denied_bash_raises_before_spawn(governed, tmp_path):
    import asyncio
    from ptc.runtime import shell
    governed({"version": 1, "deny": [{"tools": ["bash"], "pattern": "^rm "}]})
    with pytest.raises(PermissionError, match="rule 0"):
        asyncio.run(shell.bash("rm -rf /tmp/never", timeout=5))
    denied = [e for e in _audit_lines(tmp_path) if e["kind"] == "denied"]
    assert denied and denied[0]["tool"] == "bash" and "rm -rf" in denied[0]["value"]


def test_unmatched_calls_run_untouched(governed, tmp_path):
    import asyncio
    from ptc.runtime import files, shell
    governed({"version": 1, "deny": [{"tools": ["bash"], "pattern": "^rm "}]})
    out = files.write(str(tmp_path / "ok.txt"), "fine")
    assert "Wrote" in out
    r = asyncio.run(shell.bash("echo ok"))        # acceptance 1's second half: unaffected
    assert getattr(r, "code", 0) == 0 or "ok" in str(r)


def test_malformed_policy_is_loud_for_governed_and_silent_for_read(governed, tmp_path):
    from ptc import policy
    from ptc.runtime import files
    governed("this is not json")
    (tmp_path / "readable.txt").write_text("still readable")
    with pytest.raises(policy.PolicyError):
        files.write(str(tmp_path / "x.txt"), "data")
    import asyncio
    from ptc.runtime import shell
    with pytest.raises(policy.PolicyError):        # acceptance 4: bash("echo hi") raises
        asyncio.run(shell.bash("echo hi"))
    assert files.read(str(tmp_path / "readable.txt")) == "still readable"
