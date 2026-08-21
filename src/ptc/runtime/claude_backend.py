"""claude-agent-sdk backend. Every model call in the kernel goes through here or codex_backend.

S1 promoted the direct-loop design: the SDK runs on ipykernel's own asyncio loop, no thread
shim and no nest_asyncio. The SDK is imported lazily inside the call paths so bootstrap (and
any keyless test that imports this module) never pays for it.
"""
import json
import os
import time

from .agents import AgentOpts, AgentResult, child_ptc_env
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


def _sdk_options(o: AgentOpts, *, resume: str | None = None, fork: bool = False,
                 bare_llm: bool = False):
    from claude_agent_sdk import ClaudeAgentOptions
    env = _child_env(o)
    kw: dict = dict(
        model=o.model,
        cwd=env["PTC_CWD"],
        permission_mode=o.permission_mode,
        env=env,
        # S1: the default (None) loads the host user's settings, CLAUDE.md and HOOKS into
        # every child — measured 4.7x the cost of an isolated call, plus foreign hook
        # execution inside our child. PTC configures children explicitly instead.
        setting_sources=[],
    )
    if o.system:
        kw["system_prompt"] = o.system
    if o.max_turns:
        kw["max_turns"] = o.max_turns
    if o.effort:
        kw["effort"] = o.effort
    if o.allowed_tools is not None:
        kw["allowed_tools"] = o.allowed_tools
    if o.output_schema:
        kw["output_format"] = {"type": "json_schema", "schema": o.output_schema}
    if bare_llm:
        kw["tools"] = []
        kw["max_turns"] = 1
    else:
        # Children do not inherit --plugin-dir, so the ptc capability is passed explicitly.
        launcher = os.environ.get("PTC_LAUNCHER")
        if launcher:
            kw["mcp_servers"] = {"ptc": {"type": "stdio", "command": launcher}}
    if resume:
        kw["resume"] = resume
        kw["fork_session"] = fork
    return ClaudeAgentOptions(**kw)


def _fold(messages: list, o: AgentOpts, t0: float) -> AgentResult:
    """Collapse one turn's message stream into an AgentResult.

    The session id is taken from the ResultMessage when there is one: it is the CHILD's own
    id, which for a fork is the only link back that exists anywhere (S2 — a forked
    transcript carries no back-pointer to its parent).
    """
    text_parts, session_id, cost, turns, structured = [], None, None, None, None
    result_text = None
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
        elif tn == "SystemMessage" and getattr(m, "subtype", "") == "init":
            session_id = (getattr(m, "data", None) or {}).get("session_id", session_id)
    text = "\n".join(text_parts) or (result_text or "")
    if structured is None and o.output_schema and result_text:
        try:
            structured = json.loads(result_text)
        except (json.JSONDecodeError, TypeError):
            structured = None
    return AgentResult(text, session_id, structured, cost, turns,
                       int((time.time() - t0) * 1000))


async def run_once(task: str, o: AgentOpts, *, resume: str | None = None,
                   fork: bool = False, bare_llm: bool = False) -> AgentResult:
    from claude_agent_sdk import query
    t0 = time.time()
    msgs = []
    async for m in query(prompt=task, options=_sdk_options(o, resume=resume, fork=fork,
                                                           bare_llm=bare_llm)):
        msgs.append(m)
    return _fold(msgs, o, t0)


class Session:
    def __init__(self, client, o: AgentOpts, *, pending: bool):
        self._client = client
        self._o = o
        #: a turn is in flight and its stream is still unread
        self._pending = pending
        self.session_id: str | None = None
        self._messages: list = []

    async def _collect(self) -> AgentResult:
        t0 = time.time()
        batch = []
        async for m in self._client.receive_response():
            batch.append(m)
            self._messages.append({"type": type(m).__name__, "repr": repr(m)[:500]})
        self._pending = False
        r = _fold(batch, self._o, t0)
        self.session_id = r.session_id or self.session_id
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
