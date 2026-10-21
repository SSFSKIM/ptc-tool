---
name: ptc
description: Persistent per-session IPython kernel for programmatic tool calling — bulk file analysis, data transforms with state that survives across turns and compaction, long-running loops, parallel fan-out to child agents (Claude or Codex) and sub-model map-reduce, web fetch/search, and lossless recall of this session's own transcript. Use when work needs many reads, steps, or delegated agents whose intermediates belong in code, not conversation.
---

# ptc — the programmatic lane

You have a persistent IPython kernel for this session (session id: `${CLAUDE_SESSION_ID}`).
Run Python in it with the `mcp__plugin_ptc_ptc__exec` tool. If `mcp__plugin_ptc_ptc__exec`
(or its siblings `wait`/`peek`/`interrupt`/`restart`/`kernels`) is not yet visible, it is deferred —
load it first with ToolSearch, e.g. `select:mcp__plugin_ptc_ptc__exec,mcp__plugin_ptc_ptc__wait`.
Variables, imports, and functions persist across calls, turns, compaction, and `--resume`,
until the kernel's idle TTL (default 24 h, `PTC_IDLE_HOURS`; a subagent's own auto-keyed
kernel gets 1 h, `PTC_SUB_IDLE_HOURS`, and 10 min once the subagent stops) or a restart.
A kernel holding ≥ 1 GB idles out after 30 min (sooner under memory pressure); once a
result header shows `mem`, `del` what you no longer need or keep it on disk. If results ever
look like another session's namespace, pass `session="${CLAUDE_SESSION_ID}"` explicitly to
`exec`.

## When to use ptc — and when not

Use the kernel when the work is programmatic: reading and filtering many files, computing
over data, or iterating with state across steps. Use native tools instead for: a single
known edit (native Edit), reading PDFs/notebooks (native Read), and anything the
user should see and approve step by step. The harness also only tracks files it touches
natively: a file you rewrite through the kernel leaves the harness's copy stale, so the
next native Edit of it will demand a fresh Read — prefer one lane per file within a task.

## Working discipline

The MCP tool descriptions already carry the call-time contracts — the `running` yield and
`wait`, `busy` and `queue=True`, `peek`, `interrupt`, session keying, `timeout_s=` on the
tools versus `timeout=` inside the kernel. This section is only what they cannot say.

- Never poll with `time.sleep` in a cell: let the cell yield and `wait` on it. One `wait`
  with `timeout_s` above the cell's expected runtime is auto-backgrounded and comes back
  as a notification; if the budget elapses first the result says `running` — re-arm.
- Host without MCP auto-backgrounding: run the blocking CLI wait —
  `PTC_YIELD_S=3600 ptc wait <cell_id>` — in a background Bash instead; read its output,
  not just the exit code, since a `running` yield also exits 0. Subagents: never this
  fallback — the CLI resolves to the parent's kernel, not your auto-keyed one; re-arm
  the MCP wait instead.
- A `busy` that names a cell you did not submit belongs to another caller — its output is
  theirs, never your result.
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
    r = await bash(["codex", "exec", "--", prompt])   # list = no shell: args pass verbatim

Prefer the list form (or write the payload to a file and pass its path) whenever quoting
layers would stack — a Python string interpolated into a shell command is parsed twice.

`%%bash` cells also work: `%%bash` must be the FIRST line of the cell; each `%%bash` cell
is a throw-away subshell (its `cd`/`export` do not persist) — use `%cd` and
`os.environ["VAR"] = ...` for state that should carry to later cells.

To SEE an image with your own eyes — a screenshot, a capture, a plot — publish it:

    from IPython.display import Image, display
    display(Image("captures/cal-01.png"))   # up to two images ride back with the result

The pixels arrive in your perception like a native Read of the file. PDFs and notebooks
still need native Read.

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
`output_schema=` (fills `r.structured`), `timeout=` (seconds, wall-clock), and `via=` on
`run` and `spawn` (`fork` and `resume` are CLI-only and refuse it). Claude-only: `profile=`
(a non-`full` one with `provider="codex"` raises `ValueError`; see "Fan-out rules"),
`tools=` (the built-in tool SET the child sees — `[]` for none, or exact names),
`allowed_tools=` (pre-approval only — the child still sees every definition), `max_turns=`,
and any `permission_mode=` other than the default — passing one of these last four with
`provider="codex"` raises `TypeError` rather than being silently dropped, because
`codex app-server` has no tool set selection, no per-turn tool allowlist and no turn cap
and PTC pins its codex threads to `approvalPolicy="never"` + `sandbox="read-only"`. `system=` rides as
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
Concurrency is capped per route — `PTC_MAX_CONCURRENCY` (default 8) for CLI children,
`PTC_HOST_MAX_CONCURRENCY` (256) for host jobs — and recursion by `PTC_MAX_DEPTH` (default
1 — a child cannot spawn grandchildren). A cap is a ceiling, not the bound you run at —
see "Fan-out rules". A host agent's transcript is the `transcript` path `agent.list()`
gives it; `history()` is for CLI children.

## Fan-out rules

A child runs on one of two routes. On the **host route** it runs inside the kernel's agent
host, one long-lived `claude` process the kernel starts on first use: no process of its
own, so hundreds can be in flight. On the **CLI route** every child is a whole `claude`
process with a context window of its own (~1.7 s and 300–450 MB to start), and a fan-out
multiplies both. Before any cell that starts more than a couple of children, know its
route, decide its concurrency and pick its profile, and write the concurrency down in that
cell.

**Know the route.** `via="auto"` (the default) sends `llm()` and `agent.run()` to the host
whenever the call asks for nothing only the CLI has: a conversation (`spawn`, `fork`,
`resume` — a host agent answers once and takes no `send()`), Claude Code's whole tool set
(`agent.run`'s default) or its system prompt, your settings, codex, a `cwd=` other than the
kernel's, `output_schema=`, a non-default `permission_mode=`, a tool that starts agents or
comes from MCP. So `agent.run(t, profile="reader")` is a host job and `agent.run(t)` a
process. `via="host"` raises `ValueError` naming what the host lacks; `via="cli"` always
works; `r.via` says where a result ran. A host that fails to start or keeps dying sends
`"auto"` to the CLI with one warning in the cell output (`PTC_HOST=off` skips the host).

**Decide the concurrency.** On the CLI route, `PTC_MAX_CONCURRENCY` bounds one kernel,
and every kernel on the machine — yours and each subagent's — has its own, so the machine
carries the sum. A finished child still holds ~200 MB and ~0.5 % of a core until `close()`,
plus ~12 MB for its ptc MCP server (measured 2026-10-21 on Claude Code 2.1.305, ten minutes
after the turn; 2026-09-09, on an earlier build: 80 children held 10.7 GB and 1.2 cores nine
hours after their work was done); a working one costs more. Choose the bound per
fan-out from what the machine can carry and what the child does, and state it: `workflow.parallel(..., limit=N)` over `agent.run`/`llm`
awaitables, or `spawn` in batches of N and `gather` a batch before starting the next. On a
laptop, 3–4 for children that run tools; up to 8 for one-shots; past 8 only on a machine
measured to carry it, by raising `PTC_MAX_CONCURRENCY` deliberately. `agent.run` and `llm`
end their process when the turn ends; a `spawn` handle keeps its CLI as a `send()` target,
so close the ones you will not talk to again — `for h in hs: await h.close()` after the
`gather`. On the host route a completion or a read-only agent costs no process (128
agents and 128 completions at once held one host at 581 MB; 64 `llm()` calls took 4 s
there against 22 s on the CLI at 8), and `PTC_HOST_MAX_CONCURRENCY` (256) is its own cap:
for those the account's rate limit binds, so state a `limit=` up to that cap through
`workflow.parallel`, which returns a rate-limited call's `AgentFailed` in place where a
bare `gather` loses the batch to the first one. A host agent's tools still run on this
machine — each `Bash` call is a process of its own — so size a fan-out of runners, builds
or tests by the machine and the workload, as on the CLI.

**Pick the profile.** A profile is what a child's prompt is made of. `profile=` takes a
preset on `llm()` and `agent.*`, and `system=` / `tools=` override its fields:

    bare     llm()'s: a one-line system prompt, no tools
    reader   a short read-only investigator prompt, Read/Grep/Glob          → host
    worker   Claude Code's system prompt, Read/Grep/Glob/Bash/Edit/Write,
             ~/.claude/CLAUDE.md                                            → CLI
    full     agent.*'s default: an empty system prompt, every built-in tool → CLI

Tool definitions are the cost (prompt tokens for a one-word reply, measured on CLI 2.1.305):

    llm(...)                                          34   host
    llm(..., via="cli")                               61   398 with no credential in the environment
    agent.run(..., profile="reader")               3 459   host, in a git repo (3 367 outside one)
    agent.run(..., profile="reader", via="cli")    3 079
    agent.run(...)                                14 708   full; 22 of its tools, ptc's among them, deferred
    a general-purpose subagent (native Agent)     26 158   this machine's median, 2026-10-20

Give each child exactly the tools its task needs — `profile="reader"` for a reader,
`tools=["Bash"]` for a runner, `llm()` for a turn that needs none — and a `system=` only
when the task needs standing instructions. A child with an explicit tool list gets no ptc
MCP server; one that needs its own kernel names `"mcp__ptc"` among its tools (which keeps
it on the CLI). `allowed_tools=` is permission, not presence: it does not shrink the
prompt. Memory and settings take a `Profile` (`from ptc.runtime.profiles import Profile`):
`memory="global" | "project" | "both"` appends those CLAUDE.md files in Claude Code's own
framing; `settings="user"` loads your hooks, plugins, MCP servers and output style into the
child — an output style's mod has rewritten a child's reply — so no preset uses it.

The same holds when you run `claude -p` yourself through `bash(...)`, where nothing is
stripped for you: the host's settings, CLAUDE.md, plugins, MCP servers and every tool load
(29 291 tokens for the same one-word reply on the machine measured). Pass
`--setting-sources ""`, `--strict-mcp-config`, `--disable-slash-commands`,
`--no-session-persistence`, `--tools ""` (or the exact names) and a `--system-prompt` of
your own — 379 tokens. `--bare` on top also skips hooks, LSP and keychain reads (52
tokens), but it signs in only from the environment
(`ANTHROPIC_API_KEY`, a gateway's `ANTHROPIC_AUTH_TOKEN`, or an `apiKeyHelper` passed with
`--settings`) and cuts any tool list to its share of Bash, Edit and Read.

## Sub-LM map-reduce

    reply = await llm("Classify this as spam or not: ...", model="haiku")
    parsed = await llm("Extract the name and age.", json_schema={"type": "object", ...})
    labels = await workflow.parallel(*[llm(f"Classify:\n{c}") for c in chunks], limit=64)

`llm(prompt, *, model="haiku", system=None, json_schema=None, timeout=300, profile="bare",
via="auto", max_tokens=None, effort=None)` — a one-shot, no-tools, single-turn call to a
sub-model, for map-reduce classification/extraction over many chunks. On the host route,
its default, it is one model call inside the agent host, 34 prompt tokens before
your text and no process, so a fan-out of hundreds is what it is for. It is still a model
call: use it for real semantic work, not as a string op. `json_schema=` returns the parsed
value — natively on the CLI; on the host the schema is asked for in the system prompt and
the reply validated, retried once with the error. `max_tokens=None` is the model's own
output cap. On the host that cap is at most 64,000: a longer reply with no cap given fails
there as `AgentFailed` carrying the partial text, and `max_tokens=` above 64,000 sends the
call to the CLI. A turn that FAILS — a rate limit, a reply cut at its cap, a max-turns
stop, an execution error — raises `AgentFailed` (`.result` holds whatever text came back)
rather than returning the CLI's error notice as the reply; the same is true of `agent.*`
handles, which settle `error` and re-raise from `result()`.

## Web

    page = await web_fetch("https://example.com/docs")      # .url .status .title .text
    hits = await web_search("anthropic claude release notes")  # [SearchResult(title, url)]
    pages = await asyncio.gather(*[web_fetch(h.url) for h in hits[:5]])

`web_fetch(url, *, prompt=None, timeout=30)` GETs the page (redirects followed, 10 MB cap)
and returns the WHOLE thing as markdown in `.text` — filter it in Python rather than
asking for a summary. `prompt=` additionally fills `.summary` via `llm()` — up to 200 000
characters of the page go into that sub-model call, so it costs a real turn on top of the
fetch. The full text stays either way, and a summarization turn that fails raises rather
than passing its error notice off as a summary (fetch without `prompt=` when you only want
the page).

`web_search(query_text, *, allowed_domains=None, blocked_domains=None, max_results=10,
timeout=300)` returns the search tool's own hits, not a model write-up. Each
`SearchResult` carries `.title`, `.url`, `.raw`; `.snippet` is empty because the tool
returns title and url only. A search whose turn failed raises — `[]` means the search ran
and found nothing — call `web_fetch` on a url when you need the content.
`max_results` only truncates: the model decides how many hits come back (observed: 8),
PTC just slices to at most `max_results` of them — asking for more than the tool found
does not make it search harder. It runs a scoped sub-agent, a CLI child under
`PTC_MAX_CONCURRENCY`, so it is not free; one search then fan-out `web_fetch` beats repeated
searching.

## History (lossless memory)

    t = history()                        # this session's full transcript, pre-compaction
    t.user(); t.assistant(); t.tool_calls("Bash"); t.search(r"regex")
    child_t = hs[0].history()            # a child's transcript (any CLAUDE handle)

`history(session=None, cwd=None)` defaults to this kernel's own Claude session id (from
`meta.json`); pass `session=` explicitly for any other session id, and `cwd=` only if that
session ran somewhere this kernel's `meta.json` does not name. Raises `RuntimeError` if no
session id is known (an alias-keyed kernel, or a subagent's auto-keyed kernel — a sidechain
has no transcript of its own here; pass `session=` for a real one) or `FileNotFoundError`
if no transcript is found on disk. Also available: `t.text()`, `t.path`, `t.messages` (the raw JSONL rows).
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
    # profile="reader": host jobs, not processes; limit=16 for the rate limit (Fan-out rules)
    drafts = await workflow.parallel(*[agent.run(t, profile="reader") for t in tasks], limit=16)
    ok = [d for d in drafts if not isinstance(d, Exception)]
    workflow.phase("synthesize")
    print(await llm("Merge these findings:\n" + "\n---\n".join(d.text for d in ok)))

Pass unstarted awaitables (`agent.run(...)`, `llm(...)`, `web_fetch(...)`) — `agent.spawn`
starts its child the moment you call it, so `spawn` handles belong in `agent.gather`, not
in `parallel`. Stages may be sync or async, so a parse or a filter is a fine stage.
`limit` bounds the fan-out in front of you; the route's own cap still governs every
child, so the tighter of the two wins. One thing not to
fan out: `agent.fork` copies the whole parent conversation into each child and pays for
it, so it is a deliberate one-shot, never a loop body.

## Pitfalls

- Only these names are bound in the kernel: `read`, `write`, `edit`, `bash`, `agent`,
  `llm`, `web_fetch`, `web_search`, `history`, `workflow`, `asyncio`. Do not invent
  wrappers such as `call_skill(...)` or `run_subagent(...)`.
- The kernel is your notebook, not the project's runtime.
- An optional deny policy at `~/.ptc/policy.json` (env `PTC_POLICY`) trips `bash`,
  `write`, `edit`, and `web_fetch` (per redirect hop) with an audited PermissionError.
  It is the user's file — the plugin ships no rules — and it is a tripwire, not a
  sandbox: raw Python in the kernel remains ungoverned by design (audit over guard
  rails). A malformed file fails governed calls loudly rather than pretending to
  protect.
- A kernel restart loses variables and handles, but not agent sessions: the on-disk
  registry survives, so `agent.list()` then `agent.resume(sid)` reopens a child. Expiry
  does the same silently (idle TTL, a plugin upgrade rebuilding the venv), and a live
  subprocess handle is unrecoverable — when a cell starts a process you will babysit
  across turns, write its pid and log path to disk in that same cell.

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
