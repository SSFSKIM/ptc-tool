import json
import os
import subprocess
import sys


def _cli(*args, env_extra=None, input_=None):
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run([sys.executable, "-m", "ptc.cli", *args],
                          capture_output=True, text=True, env=env, input=input_, timeout=120)


def test_cli_exec_shares_kernel_and_json(ptc_home):
    env = {"PTC_SESSION": "cli1"}
    r = _cli("exec", "x = 6 * 7", env_extra=env)
    assert r.returncode == 0, r.stderr
    r2 = _cli("exec", "-", env_extra=env, input_="print(x)")
    assert "42" in r2.stdout
    r3 = _cli("exec", "--json", "x + 1", env_extra=env)
    d = json.loads(r3.stdout)
    assert d["status"] == "ok" and d["result_repr"] == "43"
    r4 = _cli("list", env_extra=env)
    assert "cli1" in r4.stdout
    r5 = _cli("kill", "-s", "cli1", env_extra=env)
    assert r5.returncode == 0


def test_cli_newest_kernel_fallback_prints_notice(ptc_home):
    _cli("exec", "1", env_extra={"PTC_SESSION": "cli2"})
    r = _cli("exec", "2", env_extra={})   # no session env at all
    assert "cli2" in r.stdout or "cli2" in r.stderr   # notice names the picked kernel
    _cli("kill", "-s", "cli2")
