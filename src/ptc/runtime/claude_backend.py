"""claude-agent-sdk backend. Every model call in the kernel goes through here or codex_backend.

S1 promoted the direct-loop design: the SDK runs on ipykernel's own asyncio loop, no thread
shim and no nest_asyncio. The SDK is imported lazily inside the call paths so bootstrap (and
any keyless test that imports this module) never pays for it.
"""
import json
import os
import time

from .agents import AgentFailed, AgentOpts, AgentResult, child_ptc_env
from .profiles import PRESETS, Profile, memory_text, resolve
from .state import STATE


def _child_env(o: AgentOpts) -> dict:
    """The environment PTC forwards to a Claude child EXPLICITLY.

    The SDK merges this over the kernel's own environment, which the kernel in turn
    inherited verbatim from the MCP adapter — credential-bearing CLAUDE_* variables
    included (an sk-ant-oat OAuth bearer among them). For a CLAUDE child that inheritance
    is what pays for subscription auth, so it stays; what must never happen is PTC
    *enumerating* that environment into a log, an audit record or a registry row. This
    dict is PTC's own variables only — never a merged copy of os.environ (Trust model +
    T18 Decision Log). The codex backend faces the opposite problem (another vendor's
    binary has no business seeing those credentials) and builds its child env instead.
    """
    return child_ptc_env(o)


#: How a profile's `settings` axis reaches the CLI.
_SETTING_SOURCES = {"none": [], "user": ["user"], "all": ["user", "project", "local"]}

#: Either one means the child can authenticate under `--bare`, which reads neither the
#: keychain nor an OAuth login (CLI help; the agent-host spec's measurements).
_BARE_CREDENTIALS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


#: A child whose tool list names this server, or one of its tools, gets the ptc MCP server.
_PTC_SERVER = "mcp__ptc"


def _names_ptc(tool: str) -> bool:
    return tool == _PTC_SERVER or tool.startswith(_PTC_SERVER + "__")


def _wants_ptc(p: Profile) -> bool:
    """Whether an `agent.*` child gets the ptc MCP server: with Claude Code's default tool
    set (today's child), or when its explicit list names the server. `--tools` does not
    filter MCP tools (measured on 2.1.305: `--tools Read,mcp__ptc__exec` still listed all
    six `mcp__ptc__*`), so a server attached to an explicit list would add its ~2.5k tokens,
    and `exec`, to every reader. Naming one of its tools attaches the whole server."""
    return p.tools == "default" or any(_names_ptc(t) for t in p.tools)


def _profile(o: AgentOpts) -> Profile:
    """The option set's profile, resolved. `_Agent._opts` already resolved it for agent
    calls; any other caller's `AgentOpts` (`llm()`, `web.py`, a test) gets the same rule."""
    return resolve(o.profile, default="full", system=o.system, tools=o.tools)


def _system_prompt(system, memory: str):
    """The profile's system axis as the SDK's `system_prompt`, memory text appended."""
    if isinstance(system, str) and system != "claude_code":
        return "\n\n".join(t for t in (system, memory) if t)
    append = system[1] if isinstance(system, tuple) else ""
    append = "\n\n".join(t for t in (append, memory) if t)
    preset = {"type": "preset", "preset": "claude_code"}
    return {**preset, "append": append} if append else preset


def _bare(p: Profile, tools, env: dict) -> bool:
    """Whether the child runs `--bare`: settings off, a plain system prompt, NO tools, and
    a credential in its environment.

    `--bare` takes a settings-off `llm()` child from about 400 prompt tokens to about 60,
    but it is not prompt trimming alone (measured on CLI 2.1.305): it cuts the default tool set to Bash, Edit and
    Read, cuts any explicit list to its part of those three (`reader`'s Read/Grep/Glob
    became Read), and replaces Claude Code's system prompt with a ~40-token stub. The one
    child it cannot change is a plain-prompt child with no tools to cut. And it signs in
    only from the environment, so without a credential there it would fail to start.

    `env` is checked as the SDK will pass it — the kernel's environment with `env` laid
    over it — and only for presence: it is never copied out, logged or audited (the trust
    note in `_child_env`).
    """
    if p.settings != "none" or tools != [] or not isinstance(p.system, str) \
            or p.system == "claude_code":
        return False
    return any((env[k] if k in env else os.environ.get(k)) for k in _BARE_CREDENTIALS)


def _sdk_options(o: AgentOpts, *, resume: str | None = None, fork: bool = False,
                 bare_llm: bool = False):
    from claude_agent_sdk import ClaudeAgentOptions
    env = _child_env(o)
    p = _profile(o)
    if p != PRESETS["full"]:
        # A profile's prompt is the same on both routes: the host runs with this too. `full`
        # stays exactly today's child, git context included.
        env["CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS"] = "1"
    if o.max_output_tokens is not None:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(o.max_output_tokens)
    kw: dict = dict(
        model=o.model,
        cwd=env["PTC_CWD"],
        permission_mode=o.permission_mode,
        env=env,
        # S1: the default (None) loads the host user's settings, CLAUDE.md and HOOKS into
        # every child — measured 4.7x the cost of an isolated call, plus foreign hook
        # execution inside our child. PTC configures children explicitly instead, and a
        # profile's `settings` axis is the only way back in.
        setting_sources=list(_SETTING_SOURCES[p.settings]),
        system_prompt=_system_prompt(p.system, memory_text(p.memory, env["PTC_CWD"])),
    )
    if o.max_turns:
        kw["max_turns"] = o.max_turns
    if o.effort:
        kw["effort"] = o.effort
    if o.allowed_tools is not None:
        kw["allowed_tools"] = o.allowed_tools
    if p.tools != "default":
        # ptc's names select its server (below); `--tools` takes the built-in set.
        kw["tools"] = [t for t in p.tools if not _names_ptc(t)]
    if o.output_schema:
        kw["output_format"] = {"type": "json_schema", "schema": o.output_schema}
    if bare_llm:
        kw["tools"] = []
        kw["max_turns"] = 1
    else:
        # Children do not inherit --plugin-dir, so the ptc capability is passed explicitly.
        launcher = os.environ.get("PTC_LAUNCHER")
        if launcher and _wants_ptc(p):
            kw["mcp_servers"] = {"ptc": {"type": "stdio", "command": launcher}}
    if _bare(p, [] if bare_llm else p.tools, env):
        kw["extra_args"] = {"bare": None}
    if resume:
        kw["resume"] = resume
        kw["fork_session"] = fork
    return ClaudeAgentOptions(**kw)


#: The one `is_error` terminal that is NOT a failure. S1's interrupt contract: an
#: interrupted turn does not raise — it ends NORMALLY with this reason and whatever partial
#: text it managed, and handing that partial back is the whole point of
#: `AgentHandle.interrupt`. Every other error terminal is a turn that did not do its job.
ABORTED_STREAMING = "aborted_streaming"


def terminal_failure(m) -> str | None:
    """The legible reason this terminal message reports, or None if it reports success.

    The CLI signals a rate limit, a max-turns stop or an execution error in band, with the
    same message type a successful turn ends on: only `is_error` separates them, and the
    error text sits where the answer normally would.
    """
    if not getattr(m, "is_error", False):
        return None
    reason = getattr(m, "terminal_reason", None)
    if reason == ABORTED_STREAMING:
        return None
    where = ", ".join(f"{k}={v!r}" for k, v in
                      (("subtype", getattr(m, "subtype", None)),
                       ("terminal_reason", reason),
                       ("api_error_status", getattr(m, "api_error_status", None)))
                      if v is not None)
    detail = "; ".join(str(e) for e in (getattr(m, "errors", None) or [])) \
        or (getattr(m, "result", None) or "").strip()
    return (f"the claude turn ended in error ({where or 'is_error'})"
            + (f": {detail[:2000]}" if detail else ""))


def message_session_id(m) -> str | None:
    """The child's own session id, from whichever message shape happens to carry it.

    The SDK announces it on the init SystemMessage (inside `.data`) and repeats it on the
    ResultMessage; other shapes may carry it as a plain attribute. `_fold` reads the same
    two places, but only once the stream has ENDED — this is the reading that can be taken
    while it is still running, which is the only one a turn that dies mid-stream leaves
    behind.
    """
    sid = getattr(m, "session_id", None)
    if sid:
        return sid
    data = getattr(m, "data", None)
    return data.get("session_id") if isinstance(data, dict) else None


def _fold(messages: list, o: AgentOpts, t0: float) -> tuple[AgentResult, str | None]:
    """Collapse one turn's message stream into an AgentResult, plus the reason it FAILED.

    The session id is taken from the ResultMessage when there is one: it is the CHILD's own
    id, which for a fork is the only link back that exists anywhere (S2 — a forked
    transcript carries no back-pointer to its parent).

    The failure reason is returned rather than raised so both callers can record the
    session id they just learned before they surface it — the id of a failed turn is still
    the id its transcript is filed under.
    """
    text_parts, session_id, cost, turns, structured = [], None, None, None, None
    result_text, failure = None, None
    for m in messages:
        tn = type(m).__name__
        if tn == "AssistantMessage":
            for b in getattr(m, "content", []):
                t = getattr(b, "text", None)
                if t:
                    text_parts.append(t)
        elif tn == "ResultMessage":
            session_id = getattr(m, "session_id", session_id)
            cost = getattr(m, "total_cost_usd", None)
            turns = getattr(m, "num_turns", None)
            result_text = getattr(m, "result", None)
            structured = getattr(m, "structured_output", None)
            failure = terminal_failure(m)
        elif tn == "SystemMessage" and getattr(m, "subtype", "") == "init":
            session_id = (getattr(m, "data", None) or {}).get("session_id", session_id)
    text = "\n".join(text_parts) or (result_text or "")
    if structured is None and o.output_schema and result_text:
        try:
            structured = json.loads(result_text)
        except (json.JSONDecodeError, TypeError):
            structured = None
    return AgentResult(text, session_id, structured, cost, turns,
                       int((time.time() - t0) * 1000)), failure


async def run_once(task: str, o: AgentOpts, *, resume: str | None = None,
                   fork: bool = False, bare_llm: bool = False) -> AgentResult:
    from claude_agent_sdk import ResultError, query
    t0 = time.time()
    msgs = []
    try:
        async for m in query(prompt=task, options=_sdk_options(o, resume=resume, fork=fork,
                                                               bare_llm=bare_llm)):
            msgs.append(m)
    except ResultError:
        # A one-shot CLI that reported a failed result then exits non-zero on purpose, and
        # the SDK raises for the exit after yielding that result (a reply cut at its output
        # cap ends this way). The result already says what failed: fold it below.
        if not any(type(m).__name__ == "ResultMessage" for m in msgs):
            raise
    r, failure = _fold(msgs, o, t0)
    if failure:
        raise AgentFailed(failure, r)
    return r


class Session:
    def __init__(self, client, o: AgentOpts, *, pending: bool):
        self._client = client
        self._o = o
        #: a turn is in flight and its stream is still unread
        self._pending = pending
        self.session_id: str | None = None
        #: Whoever wants the child's session id the moment this session learns it, rather
        #: than when the turn settles — the agent registry, in production. Called at most
        #: once per id, with the id. Set by the owner after `open_session` returns; nothing
        #: here requires a listener.
        self.on_session_id = None
        self._messages: list = []

    def _learn_session_id(self, m) -> None:
        """Bind the child's id from a message that carries one, and say so once.

        The id is issued on the SDK's init message, turns before the stream ends, and used
        to be assigned only after the async-for COMPLETED. A turn that failed mid-stream —
        or a kernel that died under one — therefore left this None for a child whose id had
        already been issued, and the transcript it is filed under, plus every resume of it,
        became unreachable. Read it as it arrives; `_fold`'s terminal assignment stays as
        the fallback for a stream that carried it nowhere else.
        """
        sid = message_session_id(m)
        if not sid or sid == self.session_id:
            return
        self.session_id = sid
        if self.on_session_id is not None:
            try:
                self.on_session_id(sid)
            except Exception:      # noqa: BLE001 — a listener's bookkeeping never breaks a turn
                pass

    async def _collect(self) -> AgentResult:
        t0 = time.time()
        batch = []
        async for m in self._client.receive_response():
            batch.append(m)
            self._messages.append({"type": type(m).__name__, "repr": repr(m)[:500]})
            self._learn_session_id(m)
        self._pending = False
        r, failure = _fold(batch, self._o, t0)
        # The id first: a failed turn is still filed under it, and `session_id` is what
        # the registry row and a later resume() are built from.
        self.session_id = r.session_id or self.session_id
        if failure:
            raise AgentFailed(failure, r)
        return r

    async def wait_result(self) -> AgentResult:
        if not self._pending:
            # A resumed session was opened without an initial turn: there is no stream to
            # drain, and receive_response() on an idle client would block forever.
            return AgentResult("", self.session_id, None, None, None, 0)
        return await self._collect()

    async def send(self, msg: str) -> AgentResult:
        await self._client.query(msg)
        self._pending = True
        return await self._collect()

    async def interrupt(self) -> None:
        """Ask the CLI to abort the turn. S1: the turn then ends with a NORMAL
        ResultMessage (terminal_reason 'aborted_streaming'), so the drain still completes —
        it just takes 4-13 s. The caller bounds that wait; we do not swallow it here."""
        await self._client.interrupt()

    async def close(self) -> None:
        await self._client.disconnect()

    def messages(self) -> list:
        return list(self._messages)


async def open_session(task: str | None, o: AgentOpts, *, resume: str | None = None) -> Session:
    from claude_agent_sdk import ClaudeSDKClient
    client = ClaudeSDKClient(options=_sdk_options(o, resume=resume))
    await client.connect()
    # connect() has spawned a `claude` CLI. From here on the caller gets a Session or an
    # exception, and on the exception path it gets no handle at all — so anything that
    # fails after connect() must disconnect here or the CLI it spawned is a leak nobody
    # holds a reference to (F6). Covers cancellation too: query() is awaited.
    try:
        s = Session(client, o, pending=bool(task))
        if task:
            await client.query(task)
    except BaseException:
        try:
            await client.disconnect()
        except Exception:                 # noqa: BLE001 — teardown never masks the fault
            pass
        raise
    return s
