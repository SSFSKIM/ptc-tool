---
name: ptc
description: Persistent per-session IPython kernel for programmatic tool calling — bulk file analysis, data transforms with state that survives across turns and compaction, long-running loops, parallel fan-out to child agents (Claude or Codex) and sub-model map-reduce, web fetch/search, and lossless recall of this session's own transcript. Use when work needs many reads, steps, or delegated agents whose intermediates belong in code, not conversation.
---

# ptc — the programmatic lane

You have a persistent IPython kernel for this session (session id: `${CLAUDE_SESSION_ID}`).
Run Python in it with the `mcp__plugin_ptc_ptc__exec` tool. If `mcp__plugin_ptc_ptc__exec`
(or its siblings `wait`/`interrupt`/`restart`/`kernels`) is not yet visible, it is deferred —
load it first with ToolSearch, e.g. `select:mcp__plugin_ptc_ptc__exec,mcp__plugin_ptc_ptc__wait`.
Variables, imports, and functions persist across calls, turns, compaction, and `--resume`,
until the kernel's idle TTL (default 24 h, `PTC_IDLE_HOURS`) or a restart. If results ever
look like another session's namespace, pass `session="${CLAUDE_SESSION_ID}"` explicitly to
`exec`.

## When to use ptc — and when not

Use the kernel when the work is programmatic: reading and filtering many files, computing
over data, or iterating with state across steps. Use native tools instead for: a single
known edit (native Edit), reading images/PDFs/notebooks (native Read), and anything the
user should see and approve step by step.

## Working discipline

- Assign large results to named variables; print compact summaries. Output truncates
  (~12k chars) with a path to the full log.
- Never poll with `time.sleep` in a cell. If a cell yields `running`, call
  `mcp__plugin_ptc_ptc__wait` with the cell id; call `mcp__plugin_ptc_ptc__interrupt` to
  stop a runaway cell — it comes back with that cell's own tail, so there is nothing left
  to wait for afterwards.
- If the kernel reports `busy`, another cell is running — wait for it or interrupt; nothing
  queues silently.
- Run a project's code in the project's own environment (its venv, its npm scripts) via
  `bash(...)`; never install project dependencies into the kernel.

## Files & shell

    text = read("src/app.py")                    # offset=, limit=, numbered= available
    write("notes/out.md", content)                # creates parent dirs
    edit("src/app.py", old, new)                  # old must match EXACTLY once; use
                                                   # replace_all=True for bulk; widen the
                                                   # snippet if it errors on multiple matches
    r = await bash("npm test", timeout=300)       # r.code, r.stdout, r.stderr, r.timed_out
    h = await bash("slow cmd", background=True)   # h.poll(), await h.wait(), h.kill()

`%%bash` cells also work: `%%bash` must be the FIRST line of the cell; each `%%bash` cell
is a throw-away subshell (its `cd`/`export` do not persist) — use `%cd` and
`os.environ["VAR"] = ...` for state that should carry to later cells.

## Agents

    r = await agent.run("summarize CHANGELOG.md")   # one-shot child; r.text, r.session_id
    hs = [agent.spawn(t, name=f"w{i}") for i, t in enumerate(tasks)]   # returns immediately
    results = await agent.gather(*hs)               # fan-in — in a LATER cell is fine
    one = await hs[0].result()                      # or await a single handle
    reply = await hs[0].send("now the risks")       # follow-up turn on the same session
    r = await agent.fork("what did we decide?")     # child inherits THIS conversation
    h = agent.resume(r.session_id)                  # reopen a past session; then h.send(...)
    agent.list()                                    # live + registry, survives restart
    r = await agent.run("review this diff", provider="codex")   # a Codex worker instead

Options that work on BOTH providers: `model=`, `system=`, `cwd=`, `effort=`,
`output_schema=` (fills `r.structured`), `timeout=` (seconds, wall-clock). Claude-only:
`allowed_tools=`, `max_turns=`, and any `permission_mode=` other than the default —
passing one with `provider="codex"` raises `TypeError` rather than being silently dropped,
because `codex app-server` has no per-turn tool allowlist and no turn cap and PTC pins its
codex threads to `approvalPolicy="never"` + `sandbox="read-only"`. `system=` rides as
`developerInstructions` on codex. `agent.fork` is claude-only (`NotImplementedError`
otherwise). Claude children default to `permission_mode="bypassPermissions"` — delegate
only work you would run yourself. Codex children are spawned `--disable hooks --disable
plugins`, so your own `~/.codex` hooks and plugins stay out of PTC's thread (set
`PTC_CODEX_INHERIT=1` to opt back in), and their environment is built from an allowlist,
so no Claude credentials cross into another vendor's binary. `read-only` there constrains
WRITES only — a codex child can still read anything this user can, and what it reads leaves
in a request to another vendor, so give it tasks whose reading you would send.

Handles live in the namespace, so "spawn now, gather next turn" works; awaiting a
handle blocks the CELL, not you. `h.interrupt()` aborts a child's turn — it waits out the
abort, so `h.result()` still gives you the partial turn — and `h.close()` ends a finished
one (its CLI stays alive for follow-up `send()`s until you do). `fork` is a one-shot
(claude only) and needs a known session id for this conversation; `agent.resume(sid)`
returns a handle that is already `done` — a `send()` target, not a running turn.
Concurrency is capped by `PTC_MAX_CONCURRENCY` (default 8) and recursion by `PTC_MAX_DEPTH`
(default 1 — a child cannot spawn grandchildren).

## Sub-LM map-reduce

    reply = await llm("Classify this as spam or not: ...", model="haiku")
    parsed = await llm("Extract the name and age.", json_schema={"type": "object", ...})
    labels = await asyncio.gather(*[llm(f"Classify:\n{c}") for c in chunks])   # fan-out

`llm(prompt, *, model="haiku", system=None, json_schema=None, timeout=300)` — a one-shot,
no-tools, single-turn call to a sub-model, useful for map-reduce classification/extraction
over many chunks. It is NOT a cheap primitive: even isolated, a call still pays a
~5 200-prompt-token floor for the CLI's own base system prompt, so it costs more than a
raw API call would — use it for real semantic work, not as a string op. Shares the same
concurrency cap as `agent`. A turn that FAILS — a rate limit, a max-turns stop, an
execution error — raises rather than returning the CLI's error notice as the reply; the
same is true of `agent.*` handles, which settle `error` and re-raise from `result()`.

## Web

    page = await web_fetch("https://example.com/docs")      # .url .status .title .text
    hits = await web_search("anthropic claude release notes")  # [SearchResult(title, url)]
    pages = await asyncio.gather(*[web_fetch(h.url) for h in hits[:5]])

`web_fetch(url, *, prompt=None, timeout=30)` GETs the page (redirects followed, 10 MB cap)
and returns the WHOLE thing as markdown in `.text` — filter it in Python rather than
asking for a summary. `prompt=` additionally fills `.summary` via `llm()`; the full text
stays either way, and a summarization turn that fails raises rather than passing its error
notice off as a summary (fetch without `prompt=` when you only want the page).

`web_search(query_text, *, allowed_domains=None, blocked_domains=None, max_results=10,
timeout=300)` returns the search tool's own hits, not a model write-up. Each
`SearchResult` carries `.title`, `.url`, `.raw`; `.snippet` is empty because the tool
returns title and url only. A search whose turn failed raises — `[]` means the search ran
and found nothing — call `web_fetch` on a url when you need the content.
`max_results` only truncates: the model decides how many hits come back (observed: 8),
PTC just slices to at most `max_results` of them — asking for more than the tool found
does not make it search harder. It runs a scoped sub-agent, so it shares the `agent`/`llm`
concurrency cap and is not free; one search then fan-out `web_fetch` beats repeated
searching.

## History (lossless memory)

    t = history()                        # this session's full transcript, pre-compaction
    t.user(); t.assistant(); t.tool_calls("Bash"); t.search(r"regex")
    child_t = hs[0].history()            # a child's transcript (any CLAUDE handle)

`history(session=None, cwd=None)` defaults to this kernel's own Claude session id (from
`meta.json`); pass `session=` explicitly for any other session id, and `cwd=` only if that
session ran somewhere this kernel's `meta.json` does not name. Raises `RuntimeError` if no
session id is known (an alias-keyed kernel) or `FileNotFoundError` if no transcript is
found on disk. Also available: `t.text()`, `t.path`, `t.messages` (the raw JSONL rows).
Claude transcripts only — on a `provider="codex"` handle `history()` raises
`NotImplementedError` (those turns are a codex rollout, a store this build does not read);
use `h.messages()` there.

## Workflow helpers

    workflow.phase("collect")                                     # progress marker
    pages = await workflow.parallel(*[web_fetch(u) for u in urls], limit=5)
    out = await workflow.pipeline(chunks, extract, verify)        # per-item stage chain

There is no workflow *engine* here — a cell of Python is the workflow. The three helpers
add only what `asyncio.gather` lacks: `parallel(*aws, limit=8)` caps how many run at once
and returns one result per input **in input order**, with a failure returned in place as
the exception object (`[r for r in out if not isinstance(r, Exception)]` to keep the good
ones). `pipeline(items, *stages, limit=8)` runs each item through every stage with no
barrier between stages — a fast item reaches the last stage while a slow one is still in
the first — and a stage exception ends that item's chain, becoming its result.
`workflow.phase(name)` prints a marker and records it.

Write multi-phase work as ordinary cells: fan out, filter in Python, verify, synthesize.

    workflow.phase("fan out")
    drafts = await workflow.parallel(*[agent.run(t) for t in tasks], limit=4)
    ok = [d for d in drafts if not isinstance(d, Exception)]
    workflow.phase("synthesize")
    print(await llm("Merge these findings:\n" + "\n---\n".join(d.text for d in ok)))

Pass unstarted awaitables (`agent.run(...)`, `llm(...)`, `web_fetch(...)`) — `agent.spawn`
starts its child the moment you call it, so `spawn` handles belong in `agent.gather`, not
in `parallel`. Stages may be sync or async, so a parse or a filter is a fine stage.
`limit` bounds the fan-out in front of you; the global `PTC_MAX_CONCURRENCY` cap still
governs everything that spawns a child, so the tighter of the two wins. One thing not to
fan out: `agent.fork` copies the whole parent conversation into each child and pays for
it, so it is a deliberate one-shot, never a loop body.

## Pitfalls

- Only these names are bound in the kernel: `read`, `write`, `edit`, `bash`, `agent`,
  `llm`, `web_fetch`, `web_search`, `history`, `workflow`, `asyncio`. Do not invent
  wrappers such as `call_skill(...)` or `run_subagent(...)`.
- The kernel is your notebook, not the project's runtime.
- A kernel restart loses variables and handles, but not agent sessions: the on-disk
  registry survives, so `agent.list()` then `agent.resume(sid)` reopens a child.

## Worked examples

Bulk file scan — the shape most work takes here:

    from pathlib import Path
    files = {p: p.read_text(errors="replace") for p in Path("src").rglob("*.py")}
    todo = {p: t for p, t in files.items() if "TODO" in t}
    print(len(files), "files,", len(todo), "with TODOs")
    for p, t in list(todo.items())[:5]:
        print(p, "-", t.count("TODO"), "TODOs")

Spawn three, gather in a later cell — the fan-in the model never has to wait on:

    picks = list(todo)[:3]
    hs = [agent.spawn(f"summarize the TODOs in {p}", name=f"sum{i}")
          for i, p in enumerate(picks)]      # names must be unique in this kernel
    # ... a later cell, another turn, even after --resume ...
    for p, r in zip(picks, await agent.gather(*hs)):   # results in handle order
        print(p, "→", r.text[:200])

Fork to recall this conversation (a deliberate one-shot, never a loop body):

    r = await agent.fork("List every file we agreed to refactor, one per line.")
    targets = [ln.strip() for ln in r.text.splitlines() if ln.strip()]
