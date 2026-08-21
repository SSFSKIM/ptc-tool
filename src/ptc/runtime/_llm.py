"""One-shot sub-LM call: no tools, one turn, subscription-billed via the `claude` CLI.

Not a cheap primitive: even isolated (`setting_sources=[]`), a call pays a ~5 200
prompt-token floor for the CLI's own base system prompt (S1 measurement — see the spec's
Surprises & Discoveries). Use it for real semantic work — classification, extraction,
judging — not as a tokenizer or a string op. This is the RLM `llm_query` primitive for
semantic map-reduce: `await asyncio.gather(*[llm(f"Classify:\\n{c}") for c in chunks])`.

No recursion guard here: `bare_llm=True` gives the child zero tools and no `ptc` MCP
server (`claude_backend._sdk_options`), so it structurally cannot call back into any
kernel — the depth brake exists for children that *can* spawn grandchildren, and this one
can't.
"""
from . import audit
from .agents import AgentOpts, guarded, shared_semaphore
from .claude_backend import run_once as _run_once


async def llm(prompt: str, *, model: str = "haiku", system: str | None = None,
              json_schema: dict | None = None, timeout: float = 300.0):
    """Run one turn against `model` with no tools and return the reply.

    `json_schema`, when given, is passed through as the SDK's `output_format` and the
    parsed dict is returned instead of text (falling back to raw text if the SDK could
    not parse it). Bound by the shared agent/llm/web semaphore; `timeout` is a wall-clock
    deadline covering the queue wait too, so a hung call cannot hold its permit forever.

    `llm()` shares its semaphore with `agent.*`, so it shares the audit trail too: one
    "llm" record per call, attributed to the current cell like every other SDK-spawning
    primitive (`agent.run/spawn/fork/resume` all audit under "agent").
    """
    o = AgentOpts(model=model, system=system or "Answer directly and concisely. No preamble.",
                  output_schema=json_schema, permission_mode="bypassPermissions")
    audit.append("llm", model=model, prompt=prompt[:200])
    r = await guarded(shared_semaphore(),
                      lambda: _run_once(prompt, o, bare_llm=True), timeout)
    if json_schema:
        return r.structured if r.structured is not None else r.text
    return r.text
