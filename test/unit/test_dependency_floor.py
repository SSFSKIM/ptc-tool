"""The declared dependency floors must admit only versions whose API this code imports.

The adapter imports `mcp.server.mcpserver.MCPServer`, which exists only in mcp 2.x —
1.x ships `mcp.server.fastmcp.FastMCP` under a different name. A `mcp>=1.2` floor let a
resolver pick a 1.x wheel that import-errors the moment Claude Code starts the server,
and no lockfile protects a consumer installing from the published metadata.
"""
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"


def _floor(dep: str) -> tuple[int, ...]:
    deps = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]
    spec = next(d for d in deps if d.split(">=")[0].strip() == dep)
    return tuple(int(p) for p in spec.split(">=")[1].strip().split("."))


def test_mcp_floor_admits_only_the_api_the_adapter_imports():
    assert _floor("mcp") >= (2, 0)


def test_the_installed_mcp_actually_has_that_api():
    from importlib.metadata import version

    from mcp.server.mcpserver import MCPServer          # noqa: F401 — the import IS the test

    installed = tuple(int(p) for p in version("mcp").split(".")[:2])
    assert installed >= _floor("mcp")[:2], f"installed mcp {installed} is below the floor"
