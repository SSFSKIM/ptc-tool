"""S9: starting an agent host through the SDK, watching its process, reading its transcripts.

The launch is "The host's lifecycle" in the spec: `ClaudeSDKClient`, `setting_sources=[]`,
no MCP servers, the host plugin through `plugins`, `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1`
and `PTC_HOST_SOCKET` in the environment. What Q1 and Q2 settle (a first query, the
main loop's tools) are parameters here.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent / "host-plugin"
MODEL = "haiku"

#: Variables of the session the driver runs under that must not reach the host. The
#: first is load-bearing for the haiku-only rule: the engine forces it on every
#: subagent, a plugin's spawn included. The others would load the session's mods into
#: the host (PLUGIN_DIRS) or tie the host to the parent session's identity.
STRIP = (
    "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE",
    "CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL", "CLAUDE_CODE_PLUGIN_DIRS", "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID",
    "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH", "CLAUDE_MODEL", "CLAUDE_EFFORT",
)


def sanitize_environ() -> list[str]:
    """Drop STRIP from this process's environment (the SDK passes os.environ on)."""
    gone = [k for k in STRIP if k in os.environ]
    for k in gone:
        del os.environ[k]
    return gone


def main_tools_option(spec: str):
    """`none` → [] (`--tools ""`), `default` → None (the CLI's default set), else a list."""
    if spec == "none":
        return []
    if spec == "default":
        return None
    return [t for t in spec.split(",") if t]


@dataclass
class Host:
    client: object
    pid: int
    started: float                      # monotonic, before connect()
    hello_at: float | None = None
    messages: list = field(default_factory=list)
    reader_error: BaseException | None = None
    reader_error_at: float | None = None
    _reader: asyncio.Task | None = None

    @property
    def start_s(self) -> float | None:
        return None if self.hello_at is None else self.hello_at - self.started

    def kill9(self) -> None:
        os.kill(self.pid, 9)

    async def close(self) -> None:
        try:
            await asyncio.wait_for(self.client.disconnect(), 15)
        except BaseException:          # noqa: BLE001 — teardown of a spike never raises
            pass
        if self._reader is not None:
            self._reader.cancel()
        try:
            os.kill(self.pid, 0)
            os.kill(self.pid, 9)
        except ProcessLookupError:
            pass


async def start_host(chan, *, cwd: Path, main_tools: str, subagent_cap: int, run_dir: Path,
                     first_query: str | None = None, hello_timeout: float = 30.0,
                     extra_env: dict | None = None) -> Host:
    """Start one host and wait for its `/hello`. Raises TimeoutError without one."""
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    env = {
        "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "1",
        "PTC_HOST_SOCKET": chan.path,
        "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": str(subagent_cap),
        "CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION": "100000",
        **(extra_env or {}),
    }
    stderr_log = open(run_dir / "host-stderr.log", "a")
    opts = ClaudeAgentOptions(
        model=MODEL,
        cwd=str(cwd),
        setting_sources=[],
        plugins=[{"type": "local", "path": str(PLUGIN)}],
        permission_mode="bypassPermissions",
        tools=main_tools_option(main_tools),
        env=env,
        extra_args={"debug-file": str(run_dir / "host-debug.log")},
        stderr=lambda line: (stderr_log.write(line + "\n"), stderr_log.flush()),
    )
    client = ClaudeSDKClient(opts)
    started = time.monotonic()
    await client.connect()
    host = Host(client=client, pid=client._transport._process.pid, started=started)
    host._reader = asyncio.create_task(_read_messages(host, run_dir / "host-messages.jsonl"))
    if first_query is not None:
        await client.query(first_query)
    try:
        await asyncio.wait_for(asyncio.shield(chan.hello), hello_timeout)
    except TimeoutError:
        await host.close()
        raise
    host.hello_at = chan.hello_at
    return host


async def _read_messages(host: Host, log: Path) -> None:
    with open(log, "a") as f:
        try:
            async for m in host.client.receive_messages():
                rec = {"t": time.monotonic() - host.started, "type": type(m).__name__,
                       "repr": repr(m)[:2000]}
                host.messages.append(rec)
                f.write(json.dumps(rec) + "\n")
                f.flush()
        except BaseException as e:
            host.reader_error, host.reader_error_at = e, time.monotonic()
            f.write(json.dumps({"t": time.monotonic() - host.started, "error": repr(e)}) + "\n")
            if isinstance(e, asyncio.CancelledError):
                raise


class Sampler:
    """RSS (MB) and CPU of the host's process tree, every `every` seconds, in a thread."""

    def __init__(self, pid: int, every: float = 0.25):
        self.pid, self.every = pid, every
        self.peak_rss_mb = 0.0
        self.peak_cpu_pct = 0.0
        self.samples: list[tuple[float, float, float]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.cpu_s_start = cpu_seconds(pid)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()
        self.cpu_s = (cpu_seconds(self.pid) or 0.0) - (self.cpu_s_start or 0.0)

    def _run(self):
        while not self._stop.is_set():
            rss, cpu = tree_usage(self.pid)
            self.samples.append((time.monotonic(), rss, cpu))
            self.peak_rss_mb = max(self.peak_rss_mb, rss)
            self.peak_cpu_pct = max(self.peak_cpu_pct, cpu)
            self._stop.wait(self.every)


def tree_usage(root: int) -> tuple[float, float]:
    """(RSS MB, %CPU) summed over `root` and its descendants."""
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,rss=,%cpu="], capture_output=True, check=False,
                         text=True).stdout
    rows = {}
    for line in out.splitlines():
        p = line.split()
        if len(p) == 4:
            rows[int(p[0])] = (int(p[1]), int(p[2]), float(p[3]))
    keep, frontier = set(), {root}
    while frontier:
        keep |= frontier
        frontier = {pid for pid, (ppid, _, _) in rows.items() if ppid in frontier} - keep
    rss = sum(rows[p][1] for p in keep if p in rows) / 1024
    cpu = sum(rows[p][2] for p in keep if p in rows)
    return rss, cpu


def cpu_seconds(pid: int) -> float | None:
    out = subprocess.run(["ps", "-o", "time=", "-p", str(pid)], capture_output=True, check=False,
                         text=True).stdout.strip()
    if not out:
        return None
    days, _, rest = out.rpartition("-")
    parts = [float(x) for x in rest.split(":")]
    secs = 0.0
    for x in parts:
        secs = secs * 60 + x
    return secs + (int(days) * 86400 if days else 0)


def transcript(session_id: str, agent_id: str) -> Path | None:
    hits = list((Path.home() / ".claude" / "projects").glob(
        f"*/{session_id}/subagents/agent-{agent_id}.jsonl"))
    return hits[0] if hits else None


def requests_of(path: Path) -> list[dict]:
    """One entry per model request in a subagent transcript (assistant rows by message id)."""
    seen: dict[str, dict] = {}
    shape: dict | None = None
    for line in path.read_text().splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") == "api-request-shape" and shape is None:
            shape = d.get("shape") or {}
        m = d.get("message") or {}
        if d.get("type") == "assistant" and m.get("id") and m["id"] not in seen:
            u = m.get("usage") or {}
            seen[m["id"]] = {
                "model": m.get("model"), "cwd": d.get("cwd"), "ts": d.get("timestamp"),
                "prompt_tokens": (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                                  + u.get("cache_read_input_tokens", 0)),
            }
            if shape is not None and len(seen) == 1:
                # The first request's shape, as the engine records it: the tools it offered.
                seen[m["id"]]["tools"] = [t.get("name") for t in shape.get("tools", [])]
                seen[m["id"]]["system_chars"] = sum(len(b.get("text", "")) for b in shape.get("system", []))
    return list(seen.values())
