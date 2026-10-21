"""The agent host's mod (`host-plugin/hooks/mods/host.tsx`), under the engine's test kit.

`claude plugin test host-plugin` loads the folder as the kernel's host loads it and runs
every `*.test.ts` under it (`host-plugin/tests/`), each test's hooks standing beneath the
mod as the kernel's channel, the model, the subagents, the disk and the clock. That is the
mod's side of protocol 1, which the Python fakes in `host_fakes.py` stand in for and so
cannot test. No model is called. The run gets a scratch HOME: it needs nothing of the
user's, and it writes a `.claude.json` there.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

HOST_PLUGIN = Path(__file__).resolve().parents[2] / "host-plugin"


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude is not on PATH")
def test_the_host_mod_passes_its_kit_tests(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    proc = subprocess.run(["claude", "plugin", "test", str(HOST_PLUGIN)],
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, f"claude plugin test failed:\n{proc.stdout}\n{proc.stderr}"
