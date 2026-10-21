"""Which engine runs a call (`via=`), and how a call becomes a host job.

`"cli"` is today's process per child; `"host"` sends a job to the kernel's agent host
(`host.py`); `"auto"` sends `llm()` and `agent.run()` to the host when it is enabled and the
call asks for nothing only the CLI route has, and everything else to the CLI. `agent.spawn`
always takes the CLI under `"auto"`: a spawn handle exists for `send()` follow-ups, which a
host agent cannot take. Every option either travels to the host or sends the call to the
CLI; none is dropped (the agent-host spec's option-compatibility table).

One deadline per call, fixed at entry (`deadline_of`): the host's start, the host's permit,
the job, and a fallback to the CLI all spend it, and a fallback gets only what remains.
"""
import json
import os
import re
import time

from .agents import AgentFailed, AgentOpts, AgentResult
from .host import HostUnavailable, _until, get_host
from .profiles import Profile, memory_text

VIAS = ("auto", "host", "cli")

#: Tools that start agents. A host subagent holding one would spawn agents inside the host,
#: past the `PTC_MAX_DEPTH` brake (`Task` is the Agent tool's older name).
_SPAWNING = ("Agent", "Task", "Workflow")

#: Host agent jobs are bounded even when nobody waits for them any more: an abandoned agent
#: holds its slot until it ends, and only its turn limit ends it.
DEFAULT_MAX_TURNS = 50
#: `$.model.complete`'s own cap. `llm()`'s `max_tokens=None` asks the host for the most the
#: model gives under it, as the CLI route takes the CLI's own per-model cap; a larger
#: explicit cap is the CLI's alone.
MAX_TOKENS_CAP = 64_000

SCHEMA_INSTRUCTION = ("Reply with one JSON value that validates against this JSON Schema, "
                      "and nothing else:\n")
_FENCE = re.compile(r"\A\s*```[A-Za-z0-9_-]*[ \t]*\n(.*?)\n?```\s*\Z", re.DOTALL)


# ---- deadlines ----------------------------------------------------------------------------


def deadline_of(timeout: float | None) -> float | None:
    """`guarded`'s rule, applied once at entry: `None` is no deadline, `0` one already
    passed, a negative value a caller error."""
    if timeout is None:
        return None
    if timeout < 0:
        raise ValueError(
            f"timeout must be a non-negative number of seconds or None, got {timeout!r}: "
            "a deadline in the past is not the same as no deadline")
    return time.monotonic() + timeout


def remaining(deadline: float | None) -> float | None:
    """What a later step may still spend: never negative, so a spent budget reaches
    `guarded` as `0` (a `TimeoutError`), not as its negative-argument `ValueError`."""
    return None if deadline is None else max(0.0, deadline - time.monotonic())


# ---- choosing the route --------------------------------------------------------------------


def kernel_cwd() -> str:
    from .state import STATE
    return STATE.config.get("cwd") or os.getcwd()


def _plain(system) -> bool:
    return isinstance(system, str) and system != "claude_code"


def llm_misfit(p: Profile, max_tokens: int | None = None) -> str | None:
    """Why the host cannot run this `llm()` call, or None. Its tools are `[]` and its
    settings `"none"` already: `llm()` refuses anything else."""
    if not _plain(p.system):
        return "a 'claude_code' system prompt (Claude Code's own, which the host lacks)"
    if max_tokens is not None and max_tokens > MAX_TOKENS_CAP:
        return (f"max_tokens={max_tokens:,}, past the host's {MAX_TOKENS_CAP:,}-token "
                "completion cap")
    return None


def agent_misfit(o: AgentOpts) -> str | None:
    """Why the host cannot run this agent call, or None (the option-compatibility table)."""
    if o.provider != "claude":
        return f"provider={o.provider!r}"
    p = o.profile
    if not _plain(p.system):
        return "a 'claude_code' system prompt (Claude Code's own, which the host lacks)"
    if p.tools == "default":
        return ("tools='default' (Claude Code's whole tool set, agent.run's default "
                "profile 'full'); give the call its tools, e.g. profile='reader'")
    for t in p.tools:
        if t in _SPAWNING:
            return (f"the tool {t!r}: a host agent may not start agents of its own, which "
                    "would pass the PTC_MAX_DEPTH brake")
        if t.startswith("mcp__"):
            return f"the tool {t!r}: the host carries no MCP servers"
    if p.settings != "none":
        return f"settings={p.settings!r} (the host loads no settings)"
    if o.cwd and os.path.realpath(o.cwd) != os.path.realpath(kernel_cwd()):
        return (f"cwd={o.cwd!r}, which is not the kernel's working directory: a host agent "
                "runs in the host's, whatever its spawn asks")
    if o.output_schema:
        return "output_schema= (a host agent has no structured output)"
    if o.permission_mode != "bypassPermissions":
        return (f"permission_mode={o.permission_mode!r}: the host runs bypassPermissions, "
                "which a subagent's own mode cannot override")
    return None


def choose(via: str, misfit: str | None, *, spawn: bool = False) -> str:
    """The route a call takes before the host is asked to start: "host" or "cli"."""
    if via not in VIAS:
        raise ValueError(f"via must be one of {VIAS}, got {via!r}")
    if via == "cli":
        return "cli"
    if via == "host":
        if misfit:
            raise ValueError(f"via='host' cannot run this call: {misfit}")
        return "host"
    if spawn or misfit or not get_host().enabled():
        return "cli"
    return "host"


async def settle(route: str, via: str, deadline: float | None) -> str:
    """Start the host for a host-route call, within the call's deadline. Under `"auto"` a
    host that cannot start sends the call to the CLI; under `"host"` that raises."""
    if route != "host":
        return route
    try:
        await _until(deadline, get_host().ensure_started)
    except HostUnavailable:
        if via == "host":
            raise
        return "cli"
    return "host"


# ---- jobs and results ----------------------------------------------------------------------


def system_text(p: Profile, cwd: str) -> str:
    """A plain-string profile's system prompt with its memory text appended, as the CLI
    route builds it (`claude_backend._system_prompt`)."""
    return "\n\n".join(t for t in (p.system, memory_text(p.memory, cwd)) if t)


def complete_job(prompt: str, *, system: str, model: str, max_tokens: int | None,
                 effort: str | None) -> dict:
    return {"kind": "complete", "model": model, "system": system, "prompt": prompt,
            "maxTokens": max_tokens, "effort": effort}


def agent_job(task: str, o: AgentOpts) -> dict:
    """A lean subagent job. `cwd` is not carried (M1, Q9: a spawn's `cwd` does not move the
    subagent), so the field stays null and a call that needs another directory never gets
    here (`agent_misfit`)."""
    return {"kind": "agent",
            "type": {"prompt": system_text(o.profile, kernel_cwd()), "tools": list(o.profile.tools),
                     "model": o.model, "effort": o.effort,
                     "permissionMode": o.permission_mode,
                     "maxTurns": o.max_turns or DEFAULT_MAX_TURNS},
            "prompt": task, "cwd": None, "description": "ptc agent"}


def host_result(res: dict, *, session_id: str | None, t0: float, kind: str) -> AgentResult:
    """A host Result as the existing `AgentResult`: no cost (nothing reports one), one turn
    for a completion, none counted for an agent."""
    return AgentResult(res.get("text") or "", session_id, None, None,
                       1 if kind == "complete" else None, int((time.time() - t0) * 1000),
                       via="host", agent_id=res.get("agentId"))


def check_max_tokens(n) -> None:
    if n is None:
        return
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise ValueError(f"max_tokens must be a positive integer or None, got {n!r}")


def once(fn):
    """`fn` as a callable that runs it on its first call only."""
    done = []

    def call():
        if not done:
            done.append(True)
            fn()
    return call


# ---- structured output on the host ----------------------------------------------------------


def schema_validator(schema: dict):
    """The schema's validator, or `ValueError` for a schema that is not one."""
    import jsonschema
    cls = jsonschema.validators.validator_for(schema)
    try:
        cls.check_schema(schema)
    except jsonschema.SchemaError as e:
        raise ValueError(f"json_schema is not a valid JSON Schema: {e.message}") from None
    return cls(schema)


def parse_reply(text: str, validator):
    """The reply's JSON value, after stripping one surrounding code fence; `ValueError`
    naming what is wrong when it does not parse or validate."""
    m = _FENCE.match(text)
    body = m.group(1) if m else text
    try:
        value = json.loads(body)
    except json.JSONDecodeError as e:
        raise ValueError(f"the reply is not JSON ({e})") from None
    errors = sorted(validator.iter_errors(value), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        where = "/".join(str(x) for x in e.path) or "the top level"
        raise ValueError(f"at {where}: {e.message}")
    return value


async def complete_structured(prompt: str, *, system: str, schema: dict, deadline,
                              on_queued=None, **job_kw):
    """`$.model.complete` has no structured output: ask for the JSON in the system prompt,
    validate the reply, retry once with the error, then fail carrying the last reply. M1's
    Q6 measured 0 of 100 replies needing the retry."""
    validator = schema_validator(schema)
    host = get_host()
    system = "\n\n".join(t for t in (system, SCHEMA_INSTRUCTION + json.dumps(schema)) if t)
    asked = prompt
    for attempt in (1, 2):
        t0 = time.time()
        res = await host.submit(complete_job(asked, system=system, **job_kw), deadline,
                                on_queued=on_queued)
        try:
            return parse_reply(res.get("text") or "", validator)
        except ValueError as e:
            if attempt == 2:
                raise AgentFailed(
                    f"llm(json_schema=...) on the agent host: the reply did not validate "
                    f"after one retry: {e}",
                    host_result(res, session_id=host.session_id, t0=t0, kind="complete")
                ) from None
            asked = (f"{prompt}\n\nYour previous reply was:\n{res.get('text') or ''}\n\n"
                     f"It does not validate against the schema: {e}. Reply again with only "
                     "the JSON value.")
