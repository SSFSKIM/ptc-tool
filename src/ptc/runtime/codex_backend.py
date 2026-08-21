"""Codex worker via `codex app-server` (stdio NDJSON JSON-RPC). One subprocess per session.

Same backend protocol as `claude_backend` (`run_once` / `open_session` → a `Session` with
`wait_result` / `send` / `interrupt` / `close` / `messages`); `agents.py` owns the registry,
the handles, the semaphore and the depth brake, and never calls `fork` on this backend.

The wire contract is spike S4's, pinned live on codex-cli 0.146.0 — several parts of it are
counter-intuitive enough to state here:

  * **The handshake is two messages.** `initialize` (whose `clientInfo` needs `name` AND
    `version`) then an `initialized` NOTIFICATION. Any request before it is refused with
    "Not initialized".
  * **`sandbox` and `sandboxPolicy` are different types.** `thread/start.sandbox` is the
    kebab-case `SandboxMode` string; `turn/start.sandboxPolicy` and every *response* field
    named `sandbox` are the camelCase tagged `SandboxPolicy` object. Sending the camelCase
    tag to `thread/start` is refused outright ("unknown variant `workspaceWrite`").
    Forward-compat: `app-server/README.md` calls the `sandbox` shorthand legacy and prefers
    permission-profile selection; proven on 0.146.0, on a deprecation track after it.
  * **Unknown params are ignored, not refused** — 0.146.0 accepted `thread/start
    {..., "effort": "high"}` and left `reasoningEffort` untouched. So a misplaced field is
    silently inert, which is why `effort` and `outputSchema` go on `turn/start`, where the
    schema actually declares them.
  * **The responder for server→client requests is load-bearing, not decorative.** Five of
    the six approval classes short-circuit on `approvalPolicy: "never"`, but MCP tool
    approvals do not, and `item/tool/requestUserInput` has no policy check at all — so a
    real request can arrive on a thread started read-only + never. A blanket
    `{"decision": "accept"}` is invalid for eight of the ten methods, and an invalid reply
    is indistinguishable from no reply: the turn simply never finishes.
  * **The final text arrives three times and only one copy is safe.** Accumulate from
    `item/completed` items of type `agentMessage`; take only status/duration from
    `turn/completed`, whose `items` array is an explicitly lossy `"itemsView": "summary"`
    projection.
  * **`turn/interrupt` needs `{threadId, turnId}`**, and the turn id is retained from the
    `turn/start` RESPONSE — not from the `turn/started` notification, which a client not
    yet draining can miss.
"""
import asyncio
import json
import os
import shlex
import time
from collections import Counter

from .agents import AgentOpts, AgentResult, child_ptc_env
from .state import STATE

#: Handshake and per-turn budgets. A caller's own `timeout` is enforced one layer up by
#: agents.py; these are the backstops that keep a wedged server from hanging forever.
_HANDSHAKE_S = 60.0
_TURN_S = 1800.0
_INTERRUPT_S = 30.0
_TERMINATE_S = 5.0
_STDERR_TAIL = 4000

#: asyncio's StreamReader defaults to a 64 KiB line limit and raises
#: `ValueError("Separator is not found, and chunk exceed the limit")` past it. Real
#: app-server lines blow through that: a single `mcpServerStatus/list` result carrying the
#: user's MCP tool catalogs measured over 100 KB on the first live run, and a long final
#: answer would too. NDJSON has no continuation, so one over-long line is a dead session.
_LINE_LIMIT = 16 * 1024 * 1024

#: Spawn-time isolation from the user's `~/.codex` (T22 live probe; see the Decision Log).
#: Only spawn-time flags work: the `thread/start.config` overlay is inert on 0.146.0, and
#: `-c mcp_servers.<name>.enabled=false` makes the binary exit silently before it speaks.
#: These two feature flags are the seam that does work — they took a PTC thread from six
#: inherited MCP servers to three and stop the user's hooks firing inside our child.
#: `PTC_CODEX_INHERIT=1` restores the user's full Codex surface.
_ISOLATION_ARGS = ("--disable", "hooks", "--disable", "plugins")

#: Why this thread had no human on it, handed back wherever a reply needs a sentence.
_UNATTENDED = "PTC runs this Codex thread unattended; no user is available to answer."


def _grant_nothing(_p: dict) -> dict:
    """Deny a permission escalation without fabricating consent.

    `GrantedPermissionProfile` has no required fields, so `{}` is a schema-valid grant of
    nothing. Echoing back the requested `permissions` (what the S4 probe did) would grant
    whatever was asked for and silently undo the read-only sandbox the thread was started
    with — the opposite of what an unattended child should do.
    """
    return {"permissions": {}, "scope": "turn"}


#: One schema-valid reply per server→client request method that can reach an unattended
#: thread. Approvals of a command or a file change accept: they are only ever raised for
#: work the sandbox still constrains, and refusing them would stall turns that
#: `approvalPolicy: "never"` was supposed to wave through. Everything that asks for user
#: DATA or user CONSENT declines instead of inventing an answer — including
#: `item/tool/requestUserInput`, where an empty `answers` map is read as "cancel" by
#: `parse_mcp_tool_approval_response`, i.e. it denies the tool call rather than
#: permissively answering it. Methods absent from this table get a JSON-RPC error, never
#: an empty `{}`: `attestation/generate` and `account/chatgptAuthTokens/refresh` both have
#: required response fields no client of ours can mint, and a malformed result would look
#: exactly like a client that never replied at all.
_RESPONDERS = {
    "item/commandExecution/requestApproval": lambda p: {"decision": "accept"},
    "item/fileChange/requestApproval": lambda p: {"decision": "accept"},
    # The legacy v1 approvals use ReviewDecision, whose accept value is "approved".
    "execCommandApproval": lambda p: {"decision": "approved"},
    "applyPatchApproval": lambda p: {"decision": "approved"},
    "item/permissions/requestApproval": _grant_nothing,
    "mcpServer/elicitation/request": lambda p: {"action": "decline", "content": None},
    "item/tool/requestUserInput": lambda p: {"answers": {}},
    "item/tool/call": lambda p: {"contentItems": [{"type": "inputText", "text": _UNATTENDED}],
                                 "success": False},
}


def _argv() -> list[str]:
    """The command that speaks app-server on stdio.

    `CODEX_BIN` is a whole command line (tests point it at the fake); `app-server` is
    appended when it is not already there so the common `CODEX_BIN=/path/to/codex` works.
    """
    argv = shlex.split(os.environ.get("CODEX_BIN") or "codex")
    if "app-server" not in argv:
        argv.append("app-server")
    if os.environ.get("PTC_CODEX_INHERIT") != "1":
        argv.extend(_ISOLATION_ARGS)
    return argv


#: The codex child's environment is BUILT, not inherited — the one place this backend
#: deliberately diverges from `claude_backend._child_env`, which lets the SDK merge PTC's
#: variables over the kernel's own environment. The kernel inherits that environment
#: verbatim from the MCP adapter, credential-bearing `CLAUDE_*` variables included (an
#: `sk-ant-oat…` OAuth bearer among them — Trust model + T18). Handing those to another
#: vendor's binary, and through it to every user MCP server that binary starts inside PTC's
#: thread, is a cross-vendor credential leak; `os.environ` verbatim would also let a codex
#: child that reaches PTC's own MCP server attach to the PARENT's kernel at the parent's
#: depth. So only names codex genuinely needs cross the seam, and PTC's four variables are
#: rewritten on the way. Note what is NOT here: `OPENAI_API_KEY` — PTC's codex path is
#: subscription auth out of `~/.codex/auth.json`, and an API key would shadow it.
_ENV_PASSTHROUGH = (
    "PATH",                                     # the binary, git, every MCP server command
    "HOME", "CODEX_HOME",                       # ~/.codex/auth.json — subscription auth
    "USER", "LOGNAME", "SHELL", "TMPDIR",
    "TERM", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",    # egress on a corporate network
    "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
)


def _child_env(o: AgentOpts) -> dict:
    env = {name: os.environ[name] for name in _ENV_PASSTHROUGH if name in os.environ}
    env.update(child_ptc_env(o))
    return env


def _cwd(o: AgentOpts) -> str:
    return o.cwd or STATE.config.get("cwd") or os.getcwd()


def _thread_params(o: AgentOpts, resume: str | None = None) -> dict:
    """S4's promoted params, verbatim. `sandbox` is the kebab-case SandboxMode string."""
    p: dict = {"cwd": _cwd(o), "approvalPolicy": "never", "sandbox": "read-only"}
    if resume:
        p["threadId"] = resume
    if o.model:
        p["model"] = o.model
    if o.system:
        # `ThreadStartParams`/`ThreadResumeParams` declare BOTH `developerInstructions` and
        # `baseInstructions`, and they are not interchangeable: `base_instructions` is
        # "Base instructions override" (it replaces codex's own core prompt, taking the
        # apply-patch and tool-use instructions with it), while `developer_instructions`
        # is "injected as a separate message" (codex-rs/core/src/config/mod.rs:676-680).
        # A caller-supplied system prompt is the second kind.
        p["developerInstructions"] = o.system
    return p


#: `AgentOpts` is one option set for both providers, so every field it accepts has to be
#: either honoured here or refused here. These three have no codex analogue: the app-server
#: exposes no per-turn tool allowlist and no turn cap, and the thread is pinned to
#: `approvalPolicy: "never"` + `sandbox: "read-only"` regardless of `permission_mode` (a
#: deliberate S4 binding — see the module docstring on the responder). Refusing is the same
#: policy T22 applied to a misplaced `effort`: an option that is accepted and then dropped
#: is a silent no-op no caller can see.
_CLAUDE_ONLY = ("allowed_tools", "max_turns")
_PINNED_PERMISSION_MODE = AgentOpts.__dataclass_fields__["permission_mode"].default


def _reject_unsupported(o: AgentOpts) -> None:
    bad = [name for name in _CLAUDE_ONLY if getattr(o, name) is not None]
    if o.permission_mode != _PINNED_PERMISSION_MODE:
        bad.append("permission_mode")
    if bad:
        raise TypeError(
            f"provider='codex' does not support these agent options: {', '.join(bad)}. "
            "`codex app-server` has no per-turn tool allowlist and no turn cap, and PTC "
            "pins its codex threads to approvalPolicy='never' + sandbox='read-only'; "
            "use provider='claude' when you need them.")


def _turn_params(thread_id: str, text: str, o: AgentOpts) -> dict:
    p: dict = {"threadId": thread_id, "input": [{"type": "text", "text": text}]}
    if o.effort:
        p["effort"] = o.effort          # ReasoningEffort — a turn/start field, not thread/start
    if o.output_schema:
        p["outputSchema"] = o.output_schema
    return p


class CodexProc:
    """One `codex app-server` child, its reader, and one turn at a time."""

    def __init__(self, o: AgentOpts):
        self._o = o
        self._proc = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task = None
        self._stderr_task = None
        self._stderr = ""
        self._exit_reason: str | None = None
        self._closed = False
        self.turn_id: str | None = None
        self._done = asyncio.Event()
        self._turn: dict = {}
        self._reset_turn()

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        from ptc import __version__
        argv = _argv()
        self._proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=_LINE_LIMIT, env=_child_env(self._o))
        self._reader_task = asyncio.ensure_future(self._read_stdout())
        self._stderr_task = asyncio.ensure_future(self._read_stderr())
        await self.request("initialize",
                           {"clientInfo": {"name": "ptc", "title": "Programmatic Tool Calling",
                                           "version": __version__}},
                           timeout=_HANDSHAKE_S)
        await self._send({"method": "initialized"})

    async def close(self) -> None:
        self._closed = True
        # Settle whatever is still waiting BEFORE the reader is cancelled: cancelling it
        # removes the only thing that could ever answer, so a turn caught mid-flight would
        # otherwise sit out the whole _TURN_S budget — holding its agents.py concurrency
        # permit — with the server already gone. EOF has this covered on every other path;
        # close() is the one that suppresses EOF.
        self._fail("codex app-server was closed")
        proc, self._proc = self._proc, None
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        self._reader_task = self._stderr_task = None
        if proc is None:
            return
        if proc.stdin is not None and not proc.stdin.is_closing():
            proc.stdin.close()
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), _TERMINATE_S)
            except (TimeoutError, asyncio.TimeoutError):
                proc.kill()
                await proc.wait()
        else:
            await proc.wait()

    # -- io ---------------------------------------------------------------
    async def _send(self, obj: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdin.is_closing():
            raise RuntimeError(self._dead("codex app-server is not running"))
        proc.stdin.write((json.dumps(obj) + "\n").encode())
        await proc.stdin.drain()

    def _dead(self, headline: str) -> str:
        tail = self._stderr.strip()
        rc = self._proc.returncode if self._proc is not None else None
        detail = f" (exit {rc})" if rc is not None else ""
        return f"{headline}{detail}" + (f": {tail[-_STDERR_TAIL:]}" if tail else "")

    async def _read_stderr(self) -> None:
        proc = self._proc
        while proc is not None and proc.stderr is not None:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            self._stderr = (self._stderr + chunk.decode(errors="replace"))[-_STDERR_TAIL:]

    def _fail(self, reason: str) -> None:
        """The server is gone: settle everything waiting on it rather than let the turn
        sit out its full timeout with nobody left to answer."""
        self._exit_reason = reason
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError(reason))
        self._done.set()

    async def _read_stdout(self) -> None:
        stdout = self._proc.stdout          # bound once: close() drops the process handle
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    reason = self._dead("codex app-server closed its stdout")
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue                # the server prints nothing else, but be safe
                if "method" in msg and "id" in msg:
                    await self._respond(msg)
                elif "id" in msg:
                    self._settle(msg)
                elif "method" in msg:
                    self._notify(msg["method"], msg.get("params") or {})
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a dead reader must not strand the turn
            reason = self._dead(f"codex app-server reader failed: {e!r}")
        self._fail(reason)

    async def _respond(self, msg: dict) -> None:
        method = msg["method"]
        self._turn["server_requests"].append(method)
        responder = _RESPONDERS.get(method)
        if responder is None:
            await self._send({"id": msg["id"], "error": {
                "code": -32601,
                "message": f"ptc cannot service {method}: {_UNATTENDED}"}})
            return
        await self._send({"id": msg["id"], "result": responder(msg.get("params") or {})})

    def _settle(self, msg: dict) -> None:
        fut = self._pending.pop(msg["id"], None)
        if fut is None or fut.done():
            return
        if "error" in msg:
            fut.set_exception(RuntimeError(f"codex app-server: {msg['error']}"))
        else:
            fut.set_result(msg.get("result") or {})

    def _notify(self, method: str, params: dict) -> None:
        self._turn["notifications"][method] += 1
        if method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage" and item.get("text"):
                self._turn["texts"].append(item["text"])
        elif method == "thread/tokenUsage/updated":
            self._turn["usage"] = (params.get("tokenUsage") or {}).get("total")
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            self._turn["status"] = turn.get("status")
            self._turn["error"] = turn.get("error")
            self._turn["duration_ms"] = turn.get("durationMs")
            self._done.set()

    # -- requests ---------------------------------------------------------
    async def request(self, method: str, params: dict, timeout: float = _HANDSHAKE_S) -> dict:
        if self._closed or self._exit_reason:
            raise RuntimeError(self._exit_reason or "codex app-server was closed")
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"id": rid, "method": method, "params": params})
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    def _reset_turn(self) -> None:
        self._done.clear()
        self._turn = {"texts": [], "notifications": Counter(), "server_requests": [],
                      "usage": None, "status": None, "error": None, "duration_ms": None}

    async def turn(self, thread_id: str, text: str, o: AgentOpts,
                   timeout: float = _TURN_S) -> dict:
        self._reset_turn()
        started = await self.request("turn/start", _turn_params(thread_id, text, o), timeout)
        self.turn_id = (started.get("turn") or {}).get("id")
        await asyncio.wait_for(self._done.wait(), timeout)
        if self._exit_reason:
            raise RuntimeError(self._exit_reason)
        out = dict(self._turn)
        out["text"] = "\n".join(t for t in out["texts"] if t)
        if out["status"] == "failed":
            raise RuntimeError(f"codex turn failed: {out['error']}")
        return out

    async def interrupt(self, thread_id: str) -> None:
        """Ask the server to end the in-flight turn. It then completes NORMALLY with
        `status: "interrupted"`, which is what agents.py's drain is written to collect."""
        if not self.turn_id or self._closed or self._exit_reason:
            return
        try:
            await self.request("turn/interrupt",
                               {"threadId": thread_id, "turnId": self.turn_id}, _INTERRUPT_S)
        except (RuntimeError, TimeoutError, asyncio.TimeoutError):
            pass        # a turn that already ended, or a server already gone: nothing to do


class Session:
    def __init__(self, proc: CodexProc, thread_id: str, o: AgentOpts, first: str | None):
        self._proc = proc
        self._o = o
        self._first = first
        self.session_id = thread_id
        self._messages: list[dict] = []

    async def _turn(self, text: str) -> AgentResult:
        t0 = time.time()
        out = await self._proc.turn(self.session_id, text, self._o)
        # Everything the turn saw, kept for the caller: `notifications` is how a live run
        # shows whether the user's MCP servers and hooks fired inside a PTC-spawned thread,
        # and `usage` is the inheritance cost the isolation flags exist to cut.
        self._messages.append({"type": "turn", "text": out["text"], "status": out["status"],
                               "duration_ms": out["duration_ms"], "usage": out["usage"],
                               "server_requests": out["server_requests"],
                               "notifications": dict(out["notifications"])})
        return AgentResult(out["text"], self.session_id, _structured(out["text"], self._o),
                           None, None, int((time.time() - t0) * 1000))

    async def wait_result(self) -> AgentResult:
        first, self._first = self._first, None
        if first is None:
            # A resumed session opened without an initial turn: nothing is in flight.
            return AgentResult("", self.session_id, None, None, None, 0)
        return await self._turn(first)

    async def send(self, msg: str) -> AgentResult:
        return await self._turn(msg)

    async def interrupt(self) -> None:
        await self._proc.interrupt(self.session_id)

    async def close(self) -> None:
        await self._proc.close()

    def messages(self) -> list:
        return list(self._messages)


def _structured(text: str, o: AgentOpts) -> dict | None:
    if not o.output_schema or not text:
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _thread_id(result: dict) -> str:
    thread = result.get("thread") or result
    return thread.get("id") or thread.get("threadId")


async def open_session(task: str | None, o: AgentOpts, *, resume: str | None = None) -> Session:
    _reject_unsupported(o)
    proc = CodexProc(o)
    try:
        await proc.start()
        method = "thread/resume" if resume else "thread/start"
        result = await proc.request(method, _thread_params(o, resume))
    except BaseException:
        await proc.close()          # never leave a child behind a failed handshake
        raise
    return Session(proc, _thread_id(result), o, task)


async def run_once(task: str, o: AgentOpts, *, resume: str | None = None,
                   fork: bool = False) -> AgentResult:
    if fork:
        raise NotImplementedError("codex fork; use provider='claude'")
    session = await open_session(task, o, resume=resume)
    try:
        return await session.wait_result()
    finally:
        await session.close()
