"""The ptc MCP server (stdio). Tools: exec, wait, interrupt, restart, kernels."""
from pathlib import Path

# Installed mcp SDK is 2.0.0: FastMCP (mcp.server.fastmcp) was replaced by MCPServer
# (mcp.server.mcpserver). Handler contracts below are unchanged from the FastMCP design;
# only the server class import/name and the registration loop adapt to the new API.
from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent, TextContent

from .client import KernelClient
from .discovery import resolve as _resolve
from .kernel import ensure_kernel, kill_kernel, list_kernels, restart_kernel
from .paths import MAX_OUTPUT_CLAMP, Config
from .shape import render

INSTRUCTIONS = """\
ptc is a persistent IPython kernel for this session. Namespace (variables, imports,
functions, agent handles) persists across calls, turns, compaction, and --resume,
until the kernel's idle TTL. Assign large results to variables and print compact
summaries; output truncates with a full-log path. Pre-bound: read, write, edit,
bash, agent, llm, web_fetch, web_search, history, workflow, asyncio (all Python;
async ones are awaited at top level).
If a cell yields `running`, use wait(cell_id); if the
kernel is busy, wait or interrupt — nothing queues. Pass session="<id>" explicitly
if results ever look like a different session's namespace.
"""

server = MCPServer("ptc", instructions=INSTRUCTIONS)


_MAX_IMAGE_BYTES = 1_500_000


def _content(rendered) -> list:
    out = [TextContent(type="text", text=rendered.text)]
    budget = 4_000_000 - len(rendered.text)
    for p in rendered.images[:2]:
        path = Path(p)
        size = path.stat().st_size
        if size > _MAX_IMAGE_BYTES:
            out.append(TextContent(type="text", text=(
                f"[image {path.name} skipped: {size} bytes exceeds 1.5MB per-image cap "
                f"— saved at {path}]")))
            continue
        data = path.read_bytes()
        if len(data) * 1.4 > budget:      # base64 inflation
            break
        import base64
        mime = "image/png" if str(path).endswith("png") else "image/jpeg"
        out.append(ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType=mime))
        budget -= int(len(data) * 1.4)
    return out


def _cfg(timeout_s: float, max_output_chars: int) -> Config:
    cfg = Config.from_env()
    cfg.yield_s = timeout_s
    cfg.max_output_chars = min(int(max_output_chars), MAX_OUTPUT_CLAMP)
    return cfg


async def exec_tool(code: str, session: str | None = None,
                    timeout_s: float = 300, max_output_chars: int = 12_000) -> list:
    r = _resolve(session)
    cfg = _cfg(timeout_s, max_output_chars)
    info = ensure_kernel(r.key, cwd=r.cwd, claude_session_id=r.claude_session_id, config=cfg)
    outcome = KernelClient(r.key).exec_cell(code, timeout_s=timeout_s, config=cfg)
    rendered = render(outcome, r.key, cfg, degraded=r.degraded)
    if info.expired_notice:
        rendered.text = (f"[previous kernel expired: {info.expired_notice.strip()} — fresh "
                         f"namespace; agent sessions remain resumable via agent.list()]\n"
                         + rendered.text)
    return _content(rendered)


async def wait_tool(cell_id: int, session: str | None = None,
                    timeout_s: float = 300, max_output_chars: int = 12_000,
                    since: int = -1) -> list:
    r = _resolve(session)
    cfg = _cfg(timeout_s, max_output_chars)
    outcome = KernelClient(r.key).wait_cell(cell_id, timeout_s=timeout_s, since=since)
    return _content(render(outcome, r.key, cfg, degraded=r.degraded))


async def interrupt_tool(session: str | None = None) -> list:
    r = _resolve(session)
    KernelClient(r.key).interrupt()
    return [TextContent(type="text", text=f"[interrupt sent to kernel {r.key}]")]


async def restart_tool(session: str | None = None) -> list:
    r = _resolve(session)
    restart_kernel(r.key, cwd=r.cwd, claude_session_id=r.claude_session_id)
    return [TextContent(type="text", text=(
        f"[kernel {r.key} restarted — the Python namespace was lost; variables and imports "
        "must be recreated. Agent sessions remain resumable via agent.list().]"))]


async def kernels_tool() -> list:
    rows = list_kernels()
    import datetime
    def _ts(v):
        return datetime.datetime.fromtimestamp(v).strftime("%m-%d %H:%M") if v else "-"
    lines = [f"{r['key']}  pid={r['pid']}  alive={r['alive']}  depth={r['depth']}  "
             f"last_used={_ts(r.get('last_used'))}  cwd={r['cwd']}"
             for r in rows] or ["(no kernels)"]
    return [TextContent(type="text", text="\n".join(lines))]


# structured_output=False: MCPServer (mcp 2.0.0) would otherwise auto-detect an output
# schema from the return annotation and populate CallToolResult.structured_content — the
# bare `-> list` annotation already yields no schema (no structured_content) under that
# auto-detection, but pinning it False makes "content array only, no structuredContent"
# an explicit guarantee rather than an incidental consequence of the annotation's shape.
for fn, name in ((exec_tool, "exec"), (wait_tool, "wait"), (interrupt_tool, "interrupt"),
                 (restart_tool, "restart"), (kernels_tool, "kernels")):
    server.tool(name=name, structured_output=False)(fn)


def main() -> None:
    server.run()          # stdio transport


if __name__ == "__main__":
    main()
