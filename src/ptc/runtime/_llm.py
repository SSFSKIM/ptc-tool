"""One-shot sub-LM call: no tools, one turn, subscription-billed through Claude Code.

Two routes (`routes.py`). Under `via="auto"` (the default) the call is one `$.model.complete`
inside the kernel's agent host (`host.py`): no process of its own, so hundreds run at once
on one host under `PTC_HOST_MAX_CONCURRENCY`. `via="cli"` — and `"auto"` whenever the host
is off, cannot start, or the call wants Claude Code's own system prompt — is a whole `claude`
CLI per call, seconds and hundreds of MB, under the shared `PTC_MAX_CONCURRENCY` semaphore.
The prompt is the `bare` profile's either way (`profiles.py`): its one-line system prompt
and nothing else. Use it for real semantic work — classification, extraction, judging —
not as a tokenizer or a string op. This is the RLM `llm_query` primitive for semantic
map-reduce: `await asyncio.gather(*[llm(f"Classify:\\n{c}") for c in chunks])`.

No recursion guard here: the call runs with zero tools — `bare_llm=True` on the CLI route
(no ptc MCP server either, `claude_backend._sdk_options`), a tool-less completion on the
host — so it structurally cannot call back into any kernel. A profile cannot reopen that:
one with tools, or with settings that would load the user's plugins and MCP servers, is
refused.
"""
import time

from . import audit, routes
from .agents import AgentOpts, guarded, shared_semaphore
from .claude_backend import run_once as _run_once
from .host import HostUnavailable, get_host
from .profiles import Profile, resolve


async def llm(prompt: str, *, model: str = "haiku", system: str | None = None,
              json_schema: dict | None = None, timeout: float | None = 300.0,
              profile: str | Profile = "bare", via: str = "auto", max_tokens: int = 4096,
              effort: str | None = None):
    """Run one turn against `model` with no tools and return the reply.

    `profile` (default `"bare"`) chooses the child's system prompt and memory; `system=`
    overrides the profile's. Its tools must be `[]` and its settings `"none"`.

    `via` is `"auto"`, `"host"` or `"cli"` (module docstring). `max_tokens` caps the reply
    on the host route; the CLI route keeps the CLI's own output limit. `effort` is passed
    on both.

    `json_schema`, when given, returns the parsed value instead of text. On the CLI route
    it is the SDK's native `output_format` (raw text if the SDK could not parse it); on the
    host the schema is asked for in the system prompt and the reply validated, retried once
    with the error, then `AgentFailed` carrying the last reply.

    `timeout` is one wall-clock deadline covering everything the call waits on — the
    host's start, a permit, the call, a fallback to the CLI — so a hung call cannot hold
    its permit forever; passing it raises `TimeoutError` on either route.

    `llm()` shares the audit trail with `agent.*`: one "llm" record per call, attributed
    to the current cell, with the route it took.
    """
    p = resolve(profile, default="bare", system=system)
    if p.tools != []:
        raise ValueError(f"llm() runs with no tools, but this profile has tools={p.tools!r}; "
                         "use agent.run() for a child that runs tools")
    if p.settings != "none":
        raise ValueError(f"llm() runs with settings='none', but this profile has "
                         f"settings={p.settings!r}: the user's settings would load their "
                         "plugins and MCP servers, tools included, into the child")
    routes.check_max_tokens(max_tokens)
    deadline = routes.deadline_of(timeout)
    route = routes.choose(via, routes.llm_misfit(p))
    if json_schema is not None and route == "host":
        routes.schema_validator(json_schema)      # a bad schema is the caller's, not the host's
    route = await routes.settle(route, via, deadline)
    audit.append("llm", model=model, prompt=prompt[:200], via=route)
    if route == "host":
        try:
            return await _on_host(prompt, p, model, json_schema, max_tokens, effort, deadline)
        except HostUnavailable:
            # Raised only before the job was queued (the host went away while this call
            # waited for a slot), so nothing ran and the CLI may take it.
            if via == "host":
                raise
    o = AgentOpts(model=model, profile=p, output_schema=json_schema, effort=effort,
                  permission_mode="bypassPermissions")
    r = await guarded(shared_semaphore(),
                      lambda: _run_once(prompt, o, bare_llm=True), routes.remaining(deadline))
    if json_schema:
        return r.structured if r.structured is not None else r.text
    return r.text


async def _on_host(prompt, p, model, json_schema, max_tokens, effort, deadline):
    system = routes.system_text(p, routes.kernel_cwd())
    kw = {"model": model, "max_tokens": max_tokens, "effort": effort}
    if json_schema is not None:
        return await routes.complete_structured(prompt, system=system, schema=json_schema,
                                                deadline=deadline, **kw)
    t0 = time.time()
    res = await get_host().submit(routes.complete_job(prompt, system=system, **kw), deadline)
    return routes.host_result(res, session_id=None, t0=t0, kind="complete").text
