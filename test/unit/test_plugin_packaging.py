"""The plugin's checked-in manifests, its stdlib-only launcher, and what the plugin root
has to CONTAIN.

`bin/ptc-launch` is what a clean profile runs before `ptc` exists anywhere: it must be
executable, importable without the venv, and stamp the venv with a payload byte-identical
to `ptc.venv.stamp_payload()` — otherwise launcher and library each think the other's
venv is stale and re-provision on every start.

The package IS the plugin root: `.claude-plugin/plugin.json` sits beside `pyproject.toml`,
`uv.lock` and `src/ptc`. See `test_the_plugin_root_contains_everything_the_launcher_hashes`
for why that containment is the load-bearing fact and not a layout preference.
"""
import ast
import importlib.util
import json
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

from ptc.venv import stamp_payload

PLUGIN = Path(__file__).resolve().parent.parent.parent        # the package IS the plugin root
LAUNCH = PLUGIN / "bin" / "ptc-launch"


def _load_launcher():
    loader = SourceFileLoader("ptc_launch", str(LAUNCH))
    spec = importlib.util.spec_from_loader("ptc_launch", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_launcher_stamp_matches_venv_module():
    assert _load_launcher().payload() == stamp_payload()


def test_launcher_and_library_run_the_same_provisioning_commands(monkeypatch, tmp_path):
    """One venv, two provisioners: the launcher builds it before the package is importable
    and the library rebuilds it afterwards, and each accepts the other's stamp. So the
    stamp has to be backed by the same BUILD — agreeing about a venv the two filled
    differently (a lock-respecting sync on one side, a fresh resolution on the other) is
    worse than not agreeing at all."""
    import subprocess

    from ptc import venv

    def recorder(calls, home):
        def run(cmd, **kw):
            calls.append([("<venv>" if x == str(home / "venv") else x) for x in cmd[1:]])
            (home / "venv" / "bin").mkdir(parents=True, exist_ok=True)
        return run

    launcher_home, library_home = tmp_path / "a", tmp_path / "b"
    launched: list = []
    monkeypatch.setenv("PTC_HOME", str(launcher_home))
    monkeypatch.setattr(subprocess, "run", recorder(launched, launcher_home))
    _load_launcher().provision()

    library: list = []
    monkeypatch.setenv("PTC_HOME", str(library_home))
    (library_home / "venv" / "bin").mkdir(parents=True)
    (library_home / "venv" / "bin" / "python").write_text("#!fake\n")
    venv.ensure_venv(run=recorder(library, library_home))

    assert launched == library and launched, launched


def test_launcher_is_executable():
    assert LAUNCH.stat().st_mode & 0o111, "ptc-launch must be committed with the exec bit"


def test_launcher_is_stdlib_only():
    tree = ast.parse(LAUNCH.read_text())
    for node in ast.walk(tree):
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        for name in names:
            assert name.split(".")[0] in sys.stdlib_module_names, name


def test_the_plugin_root_contains_everything_the_launcher_hashes():
    """A marketplace install caches the PLUGIN ROOT and nothing above it.

    The plugin root is the directory holding `.claude-plugin/plugin.json` — that is the
    whole of what a marketplace (or `--plugin-dir`) install copies. With the Python package
    one level outside it, `pyproject.toml`, `uv.lock` and `src/ptc` were simply not present
    on any installed copy: `payload()` raised FileNotFoundError on the first line the
    launcher ran, before MCP startup, so the shipped install mode could never start at all.
    Dev installs pointed at the checkout kept working, which is what hid it.

    So the rule is containment, asked of the launcher's OWN paths rather than of a layout:
    every file it hashes, and the package it syncs, must live INSIDE the directory that
    holds the manifest. Structural on purpose — a future move that walks the package back
    out of the plugin root fails here instead of at a user's first session.
    """
    root = next(p for p in [PLUGIN, *PLUGIN.parents]
                if (p / ".claude-plugin" / "plugin.json").exists())
    pkg = _load_launcher().PKG

    for name in ("pyproject.toml", "uv.lock", "src/ptc"):
        target = (pkg / name).resolve()
        assert target.exists(), f"the launcher hashes {name}, which is not there"
        assert root.resolve() in target.parents, (
            f"{name} lives outside the plugin root {root} — a marketplace install would "
            "cache the manifest and leave it behind")


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


# --- r7 finding 4: PTC_HOME is expanded here the way the package expands it -----------

def test_launcher_expands_a_user_path_in_ptc_home(monkeypatch, tmp_path):
    """`PTC_HOME=~/.ptc-alt` is resolved to the user's home by `ptc.paths.ptc_home()`, so
    the adapter looks for its kernel Python there. The launcher treated `~` as a literal
    relative directory and provisioned `<cwd>/~/.ptc-alt/venv` — a venv the package can
    never find, in a directory nobody meant to create."""
    from ptc.paths import ptc_home

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PTC_HOME", "~/.ptc-alt")
    monkeypatch.chdir(tmp_path)
    import ptc.paths as paths
    monkeypatch.setattr(paths, "_HOME", None)

    mod = _load_launcher()

    assert mod.HOME.is_absolute() and "~" not in str(mod.HOME)
    assert mod.HOME == ptc_home(), "launcher and package must provision one home"
    assert mod.VENV == ptc_home() / "venv"


def test_launcher_default_home_is_the_package_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PTC_HOME", raising=False)
    import ptc.paths as paths
    monkeypatch.setattr(paths, "_HOME", None)

    assert _load_launcher().HOME == Path.home() / ".ptc"
