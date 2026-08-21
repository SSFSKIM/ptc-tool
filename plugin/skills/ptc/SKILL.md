---
name: ptc
description: Persistent per-session IPython kernel for programmatic tool calling — bulk file analysis, data transforms with state that survives across turns and compaction, long-running loops with intermediate results kept in variables. Use when work needs many reads/steps whose intermediates belong in code, not conversation.
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
  stop a runaway cell.
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

Options: `model=`, `system=`, `allowed_tools=`, `cwd=`, `max_turns=`, `effort=`,
`output_schema=` (fills `r.structured`), `timeout=` (seconds, wall-clock),
`permission_mode=` (children default to `bypassPermissions`). `provider="codex"` is coming
in M2. Handles live in the namespace, so "spawn now, gather next turn" works; awaiting a
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
concurrency cap as `agent`.

## Web (coming in M3)

Not available yet. `web_fetch`/`web_search` ship with M3. Use Claude Code's native
WebFetch/WebSearch tools until then.

## History (coming in M3)

Not available yet. `history()` (this session's full transcript, pre-compaction) and
`handle.history()` (a child's transcript) ship with M3.

## Pitfalls

- Only these names are bound in the kernel today: `read`, `write`, `edit`, `bash`,
  `agent`, `llm`, `asyncio`. Do not call `web_fetch`, `web_search`, `history`, or
  `workflow` — they are not defined in the kernel namespace yet. Do not invent wrappers
  such as `call_skill(...)` or `run_subagent(...)` either.
- The kernel is your notebook, not the project's runtime.
- A kernel restart loses variables.

## Worked example — bulk file scan

    from pathlib import Path
    files = {p: p.read_text(errors="replace") for p in Path("src").rglob("*.py")}
    todo = {p: t for p, t in files.items() if "TODO" in t}
    print(len(files), "files,", len(todo), "with TODOs")
    for p, t in list(todo.items())[:5]:
        print(p, "-", t.count("TODO"), "TODOs")
