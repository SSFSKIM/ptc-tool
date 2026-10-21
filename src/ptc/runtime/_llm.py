"""One-shot sub-LM call: no tools, one turn, subscription-billed via the `claude` CLI.

The prompt is the `bare` profile's (`profiles.py`): its one-line system prompt and nothing
else — about 60 prompt tokens for a one-line task when the CLI can run `--bare` (a
credential in the environment), and about 400 when it cannot (more in a git worktree, whose
description the CLI adds to the prompt). The cost that remains is the process: every
call is a whole `claude` CLI, seconds and hundreds of MB. Use it for real semantic work —
classification, extraction, judging — not as a tokenizer or a string op. This is the RLM
`llm_query` primitive for semantic map-reduce:
`await asyncio.gather(*[llm(f"Classify:\\n{c}") for c in chunks])`.

No recursion guard here: `bare_llm=True` gives the child zero tools and no `ptc` MCP
server (`claude_backend._sdk_options`), so it structurally cannot call back into any
kernel — the depth brake exists for children that *can* spawn grandchildren, and this one
can't. A profile cannot reopen that: one with tools, or with settings that would load the
user's plugins and MCP servers, is refused.
"""
from . import audit
from .agents import AgentOpts, guarded, shared_semaphore
from .claude_backend import run_once as _run_once
from .profiles import Profile, resolve


async def llm(prompt: str, *, model: str = "haiku", system: str | None = None,
              json_schema: dict | None = None, timeout: float = 300.0,
              profile: str | Profile = "bare"):
    """Run one turn against `model` with no tools and return the reply.

    `profile` (default `"bare"`) chooses the child's system prompt and memory; `system=`
    overrides the profile's. Its tools must be `[]` and its settings `"none"`.

    `json_schema`, when given, is passed through as the SDK's `output_format` and the
    parsed dict is returned instead of text (falling back to raw text if the SDK could
    not parse it). Bound by the shared agent/llm/web semaphore; `timeout` is a wall-clock
    deadline covering the queue wait too, so a hung call cannot hold its permit forever.

    `llm()` shares its semaphore with `agent.*`, so it shares the audit trail too: one
    "llm" record per call, attributed to the current cell like every other SDK-spawning
    primitive (`agent.run/spawn/fork/resume` all audit under "agent").
    """
    p = resolve(profile, default="bare", system=system)
    if p.tools != []:
        raise ValueError(f"llm() runs with no tools, but this profile has tools={p.tools!r}; "
                         "use agent.run() for a child that runs tools")
    if p.settings != "none":
        raise ValueError(f"llm() runs with settings='none', but this profile has "
                         f"settings={p.settings!r}: the user's settings would load their "
                         "plugins and MCP servers, tools included, into the child")
    o = AgentOpts(model=model, profile=p, output_schema=json_schema,
                  permission_mode="bypassPermissions")
    audit.append("llm", model=model, prompt=prompt[:200])
    r = await guarded(shared_semaphore(),
                      lambda: _run_once(prompt, o, bare_llm=True), timeout)
    if json_schema:
        return r.structured if r.structured is not None else r.text
    return r.text
