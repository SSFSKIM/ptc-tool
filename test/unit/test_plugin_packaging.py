"""The plugin's checked-in manifests and its stdlib-only launcher.

`bin/ptc-launch` is what a clean profile runs before `ptc` exists anywhere: it must be
executable, importable without the venv, and stamp the venv with a payload byte-identical
to `ptc.venv.stamp_payload()` — otherwise launcher and library each think the other's
venv is stale and re-provision on every start.
"""
import ast
import importlib.util
import json
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

from ptc.venv import stamp_payload

PLUGIN = Path(__file__).resolve().parent.parent.parent / "plugin"
LAUNCH = PLUGIN / "bin" / "ptc-launch"


def _load_launcher():
    loader = SourceFileLoader("ptc_launch", str(LAUNCH))
    spec = importlib.util.spec_from_loader("ptc_launch", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_launcher_stamp_matches_venv_module():
    assert _load_launcher().payload() == stamp_payload()


def test_launcher_is_executable():
    assert LAUNCH.stat().st_mode & 0o111, "ptc-launch must be committed with the exec bit"


def test_launcher_is_stdlib_only():
    tree = ast.parse(LAUNCH.read_text())
    for node in ast.walk(tree):
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        for name in names:
            assert name.split(".")[0] in sys.stdlib_module_names, name


def test_plugin_manifest():
    m = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert m["name"] == "ptc" and m["version"] and m["description"]


def test_mcp_manifest_points_at_the_launcher():
    cfg = json.loads((PLUGIN / ".mcp.json").read_text())
    assert cfg["mcpServers"]["ptc"]["command"] == "${CLAUDE_PLUGIN_ROOT}/bin/ptc-launch"
    assert LAUNCH.exists()


def test_hooks_manifest_registers_session_start():
    cfg = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    entries = cfg["hooks"]["SessionStart"][0]["hooks"]
    assert entries[0]["type"] == "command"
    assert "${CLAUDE_PLUGIN_ROOT}/hooks/session_start.py" in entries[0]["command"]
    assert (PLUGIN / "hooks" / "session_start.py").exists()
