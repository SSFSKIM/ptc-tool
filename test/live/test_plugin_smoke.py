"""M1 close-out live smoke: does the shipped plugin actually route a bulk-file prompt to
the ptc kernel, unprompted?

Requires PTC_LIVE=1 and a `claude` CLI logged in on subscription auth (no ANTHROPIC_API_KEY
— that would silently flip billing to metered API, see the spec's trust-model section).
Reuses the T11/T13 probe pattern (test/spikes/s3_hook_discovery.py): `claude -p
--plugin-dir <plugin>`, stream-json capture, JSON-parsed tool_use blocks rather than raw
string search, and a launch env stripped of CLAUDE_CODE_SESSION_ID/CLAUDECODE so the nested
session isn't keyed to the outer (agent-driven) one — a real user's terminal has neither var.

Kernel hygiene: this test snapshots ~/.ptc/kernels before launching and kills only the
kernel dir(s) that appear afterward — it never touches a kernel it did not spawn.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent.parent
PLUGIN = PKG / "plugin"
live = pytest.mark.skipif(os.environ.get("PTC_LIVE") != "1", reason="PTC_LIVE=1 required")

CLEAN_ENV = {k: v for k, v in os.environ.items()
             if k not in ("CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}

PROMPT = ("analyze all python files under src/ for TODO density; "
          "keep intermediate data in variables")


def _existing_kernel_keys() -> set[str]:
    root = Path.home() / ".ptc" / "kernels"
    return {p.name for p in root.iterdir() if p.is_dir()} if root.is_dir() else set()


def _run_claude(cwd: Path) -> dict:
    cmd = ["claude", "-p", "--plugin-dir", str(PLUGIN),
           "--permission-mode", "bypassPermissions",
           "--output-format", "stream-json", "--verbose", PROMPT]
    p = subprocess.run(cmd, cwd=str(cwd), env=CLEAN_ENV, capture_output=True, text=True,
                       timeout=600)
    tool_names, texts = [], []
    for line in p.stdout.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        for block in (msg.get("message") or {}).get("content", []) or []:
            if block.get("type") == "tool_use":
                tool_names.append(block.get("name"))
            elif block.get("type") == "text":
                texts.append(block.get("text", ""))
    return {"rc": p.returncode, "stdout": p.stdout, "stderr": p.stderr,
            "tool_names": tool_names, "texts": texts}


@live
def test_model_uses_ptc_for_bulk_prompt(tmp_path):
    """A11: the prompt does NOT mention ptc — the skill must route the model there, and
    the proof is an actual mcp__plugin_ptc_ptc__exec tool_use event in the stream (F10),
    not just a plausible-looking text reply."""
    src = tmp_path / "src"
    src.mkdir()
    for i in range(12):
        (src / f"m{i}.py").write_text(f"# TODO item {i}\nx = {i}\n")

    before = _existing_kernel_keys()
    try:
        r = _run_claude(tmp_path)
    finally:
        from ptc.kernel import kill_kernel
        for key in _existing_kernel_keys() - before:
            kill_kernel(key)

    assert r["rc"] == 0, r["stderr"][-2000:]
    assert "mcp__plugin_ptc_ptc__exec" in r["tool_names"], (
        f"model never called mcp__plugin_ptc_ptc__exec; skill routing failed. "
        f"tool_names={r['tool_names']!r} stderr={r['stderr'][-1000:]!r}")
    assert "12" in r["stdout"], r["stdout"][-2000:]
