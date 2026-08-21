"""The ptc MCP server (stdio). Tools: exec, wait, interrupt, restart, kernels."""
import asyncio
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
async ones are awaited at top level). Tools: exec, wait, interrupt, restart, kernels.
If a cell yields `running`, use wait(cell_id); if the kernel is busy, wait or
interrupt — nothing queues. Pass session="<id>" explicitly if results ever look like
a different session's namespace, or if this client does not set a session id of its
own (the header then reads `keying: adapter-local`).
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


# Every handler below does its kernel work in a WORKER THREAD. The client library is
# synchronous by design (blocking flock, poll-and-sleep follow loops), and running it on
# the stdio server's own event loop meant one in-flight exec/wait froze the whole adapter
# for its full timeout — an interrupt arriving in parallel could not be dispatched until
# the call it was meant to stop had already returned. T8 parked this and named to_thread
# as the remedy; parallel tool calls are the story that needed it.
#
# Nothing about the protocol changes: each handler still runs the same calls in the same
# order and renders the same text. Mutual exclusion still holds across threads, too —
# lock.py opens a fresh descriptor per acquisition and `flock` locks the open file
# DESCRIPTION, so two threads in this process contend exactly like two processes do, and
# a loser still gets its "lock-held" Busy from the same timeout.


async def exec_tool(code: str, session: str | None = None,
                    timeout_s: float = 300, max_output_chars: int = 12_000) -> list:
    r = await asyncio.to_thread(_resolve, session)
    cfg = _cfg(timeout_s, max_output_chars)
    info = await asyncio.to_thread(ensure_kernel, r.key, cwd=r.cwd,
                                   claude_session_id=r.claude_session_id, config=cfg)
    outcome = await asyncio.to_thread(KernelClient(r.key).exec_cell, code,
                                      timeout_s=timeout_s, config=cfg)
    rendered = render(outcome, r.key, cfg, degraded=r.degraded)
    if info.expired_notice:
        rendered.text = (f"[previous kernel expired: {info.expired_notice.strip()} — fresh "
                         f"namespace; agent sessions remain resumable via agent.list()]\n"
                         + rendered.text)
    return _content(rendered)


async def wait_tool(cell_id: int, session: str | None = None,
                    timeout_s: float = 300, max_output_chars: int = 12_000,
                    since: int = -1) -> list:
    r = await asyncio.to_thread(_resolve, session)
    cfg = _cfg(timeout_s, max_output_chars)
    outcome = await asyncio.to_thread(KernelClient(r.key).wait_cell, cell_id,
                                      timeout_s=timeout_s, since=since)
    return _content(render(outcome, r.key, cfg, degraded=r.degraded))


#: How long the interrupt tool waits for the cell it stopped to settle. interrupt() itself
#: already spends up to ~4 s (control-channel reply, then the 2 s grace before the SIGINT
#: fallback); the KeyboardInterrupt lands well inside a second after that. Past this budget
#: the cell is reported as still running rather than waited on — the tool stays bounded.
_INTERRUPT_SETTLE_S = 10.0


def _interrupt_and_settle(key: str):
    """Interrupt whatever is running and follow that cell to its terminal record.

    The cell id has to be read BEFORE the interrupt: once the cell settles, current.json
    no longer names anything running and the id the caller needs is gone. Returns None
    when nothing was running (or when no real id exists yet — the pending sentinel).
    """
    client = KernelClient(key)
    busy = client.is_busy()
    client.interrupt()
    cell_id = busy.cell_id if busy is not None else None
    if cell_id in (None, -1):
        return None
    return client.wait_cell(cell_id, timeout_s=_INTERRUPT_SETTLE_S, since=-1)


async def interrupt_tool(session: str | None = None) -> list:
    r = await asyncio.to_thread(_resolve, session)
    ack = f"[interrupt sent to kernel {r.key}]"
    outcome = await asyncio.to_thread(_interrupt_and_settle, r.key)
    if outcome is None:
        return [TextContent(type="text", text=f"{ack} — no cell was running")]
    # the interrupted cell's own tail, through the same wait/render path a wait() call
    # takes (same cursor, so nothing is replayed and nothing is consumed twice)
    cfg = _cfg(_INTERRUPT_SETTLE_S, 12_000)
    rendered = render(outcome, r.key, cfg, degraded=r.degraded)
    rendered.text = f"{ack}\n{rendered.text}"
    return _content(rendered)


async def restart_tool(session: str | None = None) -> list:
    r = await asyncio.to_thread(_resolve, session)
    await asyncio.to_thread(restart_kernel, r.key, cwd=r.cwd,
                            claude_session_id=r.claude_session_id)
    return [TextContent(type="text", text=(
        f"[kernel {r.key} restarted — the Python namespace was lost; variables and imports "
        "must be recreated. Agent sessions remain resumable via agent.list().]"))]


async def kernels_tool() -> list:
    rows = await asyncio.to_thread(list_kernels)
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
