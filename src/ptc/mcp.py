"""The ptc MCP server (stdio). Tools: exec, wait, interrupt, restart, kernels."""
import asyncio
from pathlib import Path

# Installed mcp SDK is 2.0.0: FastMCP (mcp.server.fastmcp) was replaced by MCPServer
# (mcp.server.mcpserver). Handler contracts below are unchanged from the FastMCP design;
# only the server class import/name and the registration loop adapt to the new API.
from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent, TextContent

from .client import KernelClient
from .discovery import read_meta
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
interrupt — nothing queues. wait also takes until="<python regex>", returning as
soon as new output first matches instead of at settle. Pass session="<id>"
explicitly if results ever look like a different session's namespace, or if this
client does not set a session id of its own (the header then reads
`keying: adapter-local`).
Tool descriptions carry only the call-time contracts; for anything beyond quick
calls — agent fan-out, llm, web, workflow — invoke the ptc:ptc skill first: it is
the full API doctrine.
"""

server = MCPServer("ptc", instructions=INSTRUCTIONS)


_MAX_IMAGE_BYTES = 1_500_000


def _moved_image_note(path: Path) -> TextContent:
    """Names the convention rather than the file, because the file is what is missing:
    a restart archives the whole of `cells/` under a `cells-prev-<stamp>` sibling, so
    that is where a reader goes looking."""
    return TextContent(type="text", text=(
        f"[image {path.name} is gone: a concurrent restart moved this cell's files out "
        f"of {path.parent} into a `cells-prev-*` archive beside it while this reply was "
        "being assembled — look for it there]"))


def _content(rendered) -> list:
    """The tool reply: the cell's text, then up to two of its images.

    The text is the result; the images are an enrichment of it. `render()` verified those
    paths, but a restart rotating `cells/` into `cells-prev-*` between that verification
    and these reads takes the files out from under them — and an unguarded stat or read
    then raised out of the whole handler, discarding the text the caller actually asked
    for along with every image that was still there. A lost image costs a note and the
    next image is still tried; the text survives every one of them.
    """
    out = [TextContent(type="text", text=rendered.text)]
    budget = 4_000_000 - len(rendered.text)
    for p in rendered.images[:2]:
        path = Path(p)
        try:
            size = path.stat().st_size
        except OSError:
            out.append(_moved_image_note(path))
            continue
        if size > _MAX_IMAGE_BYTES:
            out.append(TextContent(type="text", text=(
                f"[image {path.name} skipped: {size} bytes exceeds 1.5MB per-image cap "
                f"— saved at {path}]")))
            continue
        try:
            data = path.read_bytes()
        except OSError:
            out.append(_moved_image_note(path))
            continue
        if len(data) * 1.4 > budget:      # base64 inflation
            break
        import base64
        mime = "image/png" if str(path).endswith("png") else "image/jpeg"
        out.append(ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType=mime))
        budget -= int(len(data) * 1.4)
    return out


def _cfg(timeout_s: float | None, max_output_chars: int | None) -> Config:
    """The call's configuration: environment first, explicit arguments on top.

    `None` means "the caller omitted this", which is not the same as asking for the
    default. Substituting the defaults in the handler signatures instead made `_cfg`
    overwrite the values loaded from PTC_YIELD_S and PTC_MAX_OUTPUT_CHARS on every normal
    call, so those documented settings had no effect on the MCP path at all.
    """
    cfg = Config.from_env()
    if timeout_s is not None:
        cfg.yield_s = float(timeout_s)
    if max_output_chars is not None:
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
                    timeout_s: float | None = None,
                    max_output_chars: int | None = None) -> list:
    """Run Python in this session's persistent IPython kernel. Variables, imports, and
    handles survive across calls, turns, and compaction — assign large results to variables
    and print compact summaries; output truncates with a path to the full log. Pre-bound:
    read, write, edit, bash, agent, llm, web_fetch, web_search, history, workflow. The
    async ones (bash, agent.*, llm, web_*) are coroutines: without `await` a call silently
    returns a coroutine object instead of running. In-kernel bash() takes timeout= in
    SECONDS (this tool takes timeout_s=) and accepts an argv list — bash(["cmd", arg]) —
    which skips the shell so quoting layers never stack. One session key is one kernel:
    parallel callers (subagents included) must each pass their own session="<name>" or
    they contend for it and see each other's cells. A `running` yield means use the wait
    tool; `busy` means another cell is still running — nothing queues. The ptc:ptc skill
    documents the full agent/llm/web/workflow API — invoke it before using those."""
    r = await asyncio.to_thread(_resolve, session)
    cfg = _cfg(timeout_s, max_output_chars)
    info = await asyncio.to_thread(ensure_kernel, r.key, cwd=r.cwd,
                                   claude_session_id=r.claude_session_id, config=cfg)
    outcome = await asyncio.to_thread(KernelClient(r.key).exec_cell, code,
                                      timeout_s=cfg.yield_s, config=cfg)
    rendered = render(outcome, r.key, cfg, degraded=r.degraded)
    if info.expired_notice:
        rendered.text = (f"[previous kernel expired: {info.expired_notice.strip()} — fresh "
                         f"namespace; agent sessions remain resumable via agent.list()]\n"
                         + rendered.text)
    return _content(rendered)


async def wait_tool(cell_id: int, session: str | None = None,
                    timeout_s: float | None = None,
                    max_output_chars: int | None = None,
                    since: int = -1, until: str | None = None) -> list:
    """Collect a running cell's result, or follow it until it settles. since=-1 (default)
    resumes after what this caller was last served; an explicit byte offset re-reads from
    there. Set timeout_s above the cell's expected runtime: a call still in flight at ~2
    minutes is auto-backgrounded by the host and the full result arrives as a task
    notification — one long wait beats polling. until="<python regex>" returns EARLY the
    moment new output first matches (the match rides back in `matched`, capped at 512
    chars; the cell keeps running) — a cell that completes always supersedes a match."""
    r = await asyncio.to_thread(_resolve, session)
    cfg = _cfg(timeout_s, max_output_chars)
    # A pattern that will not compile raises out of here, and the MCP layer renders a tool
    # error: the caller asked for something impossible and needs to be told, not defaulted.
    outcome = await asyncio.to_thread(KernelClient(r.key).wait_cell, cell_id,
                                      timeout_s=cfg.yield_s, since=since, until=until)
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
    """Stop the running cell and return that cell's own output tail — after this there is
    nothing left to wait for. A no-op (with a note) when nothing is running."""
    r = await asyncio.to_thread(_resolve, session)
    ack = f"[interrupt sent to kernel {r.key}]"
    outcome = await asyncio.to_thread(_interrupt_and_settle, r.key)
    if outcome is None:
        return [TextContent(type="text", text=f"{ack} — no cell was running")]
    # the interrupted cell's own tail, through the same wait/render path a wait() call
    # takes (same cursor, so nothing is replayed and nothing is consumed twice)
    cfg = _cfg(_INTERRUPT_SETTLE_S, None)
    rendered = render(outcome, r.key, cfg, degraded=r.degraded)
    rendered.text = f"{ack}\n{rendered.text}"
    return _content(rendered)


async def restart_tool(session: str | None = None) -> list:
    """Kill and respawn the kernel: the Python namespace (variables, imports, handles) is
    lost. Child agent sessions survive on disk — agent.list() then agent.resume(sid)."""
    r = await asyncio.to_thread(_resolve, session)
    # The stored metadata first, exactly as the CLI restart path does it. Only the
    # hook-runfile rung resolves a cwd at all: an explicit `session=`, either env rung and
    # the adapter-local fallback all carry `cwd=None`, so without this the respawn landed in
    # whatever directory the ADAPTER happened to be in and overwrote meta.json's cwd with
    # it — a kernel created from another project then ran its file tools and its agents in
    # the wrong place. `claude_session_id` is the same story: history() and agent.fork()
    # read it back from meta.json, and a restart must not blank it.
    meta = await asyncio.to_thread(read_meta, r.key)
    await asyncio.to_thread(restart_kernel, r.key,
                            cwd=meta.get("cwd") or r.cwd,
                            claude_session_id=(meta.get("claude_session_id")
                                               or r.claude_session_id))
    return [TextContent(type="text", text=(
        f"[kernel {r.key} restarted — the Python namespace was lost; variables and imports "
        "must be recreated. Agent sessions remain resumable via agent.list().]"))]


async def kernels_tool() -> list:
    """List every live kernel: key, pid, alive, depth, last-used, cwd — what exists before
    choosing a session= value."""
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
