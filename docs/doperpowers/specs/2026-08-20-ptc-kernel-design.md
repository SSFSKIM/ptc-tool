# PTC — Programmatic Tool Calling for Claude Code on a Persistent IPython Kernel

## Purpose

Claude Code drives every tool call through one model inference round-trip, and every tool result
lands in the model's context. That shape is fine for "read this file, edit that line" work and bad
for the workloads the RLM literature measures: bulk read-and-filter over hundreds of files,
semantic map-reduce over chunks, fan-out to many subagents with programmatic fan-in, long
iterative loops whose intermediate state should live in variables rather than conversation
history.

**PTC** (Programmatic Tool Calling) gives a Claude Code session a **persistent IPython kernel** as
a tool, with Claude-Code-equivalent capabilities pre-bound as Python functions. After this change,
a user can ask Claude Code to "audit all 400 test files for flaky patterns" and watch it write one
Python cell that reads every file into variables, filters them deterministically, fans out three
`agent.spawn(...)` children over the survivors, gathers their answers, and prints a 30-line
summary — with the model's context receiving only that summary. State (variables, imports, parsed
data, child-agent handles) persists across tool calls, across turns, across compaction, and across
`claude --resume` (for as long as the kernel lives — see the idle TTL below).

To see it working end to end:

```bash
cd ptc && uv run ptc setup          # provision the kernel venv
claude --plugin-dir ./plugin        # start Claude Code with the ptc plugin
# in the session:  "use the ptc kernel: load all *.py under src/, count defs per file,
#                   keep the result in a variable, show top 10"
# later, after quitting:  claude --resume <session-id>
#                   "print the variable from before"   → same data, no re-read
```

This is the productization of the conversation in `ptc-surface/Conversation.md` (external input:
RLM harnesses, Prime Agent, Anthropic PTC): Prime Agent's programming model, transposed onto
Claude Code as an MCP server + CLI + skill instead of a whole replacement harness.

### Terms of art

- **RLM** (Recursive Language Model): an inference harness where long context and intermediate
  state live in an external REPL as variables, and the model writes code — including sub-model
  calls — to process them.
- **PTC** (Programmatic Tool Calling): the narrower primitive — the model emits one program that
  orchestrates many tool calls; intermediate results stay out of model context.
- **Kernel**: a detached `ipykernel` process speaking the Jupyter wire protocol (ZeroMQ
  shell/iopub/control channels), one per Claude Code session, holding the Python namespace.
- **Cell**: one code submission to the kernel. Identified by the kernel's **execution count**
  (the integer IPython shows as `In [n]`), broadcast on iopub `execute_input`.
- **Yield**: `exec` returning `status: running` with a `cell_id` before the cell finishes, so the
  model regains control while the cell keeps running (Codex code-mode's `exec`/`wait` pattern).
- **Session key**: the string that names a kernel — normally the Claude Code session id.
- **Depth**: how many agent-spawn levels below the user's session a process is. The user's session
  is depth 0; its kernel-spawned children are depth 1.

## Non-goals (v1)

- No security sandbox. The kernel runs model-written code with the user's OS permissions —
  same trust model as Prime Agent, stated loudly (see Trust model).
- No `ccx serve` / fleet-attach backend, and no messaging *other running Claude Code sessions*.
  The `AgentBackend` seam exists so these can be added later.
- No in-kernel `glob()`/`grep()` functions and **no search guidance in the skill** — searching
  stays wherever the model already does it (its native rg-backed tools, or whatever it runs in a
  shell).
- No direct Anthropic API client anywhere. All model calls made *from the kernel* go through the
  Claude Agent SDK → `claude` CLI, billing the user's subscription when they are OAuth-logged-in.
- No dill/state snapshots: a kernel restart loses the namespace (child agents remain resumable
  via the on-disk registry). Prime's snapshot layer is future work.
- No Codex-side installation testing. Registering the same MCP server in Codex
  (`codex mcp add ptc -- ptc-mcp`) is documented, not exercised by the acceptance suite.
- No Workflow *tool* replication: `workflow` ships as thin helpers + doctrine, because PTC itself
  is the orchestration language (a Workflow script is just a Python cell here).
- No Windows support in v1 (detached spawn, flock-based ownership, and the launcher are
  POSIX-only; macOS + Linux). Windows is future work, noted in the README.

## Architecture

```
Claude Code session  (pid P, session id S, depth 0)
 ├─ native tools (Read/Edit/Bash/Agent/…)          ← unchanged; the hybrid lane
 ├─ plugin hook  SessionStart → ~/.ptc/run/claude-<P>.json  {session_id, cwd}
 ├─ Skill  ptc  (doctrine: when & how to use the kernel)
 └─ MCP server "ptc"  (stdio adapter, child of P)
        tools: exec · wait · interrupt · restart · kernels
        │   Jupyter protocol (jupyter_client / ZeroMQ)     ┌── ptc CLI (from Bash or a human shell)
        ▼                                                   ▼
   detached ipykernel, keyed by S     ~/.ptc/kernels/<S>/{connection.json, pid, cells/, audit.jsonl, agents.json}
        namespace:  read write edit bash agent llm web_fetch web_search history workflow
        ├─ agent(provider="claude") → claude-agent-sdk (in-kernel) → `claude` subprocesses (depth+1)
        ├─ agent(provider="codex")  → `codex app-server` stdio JSON-RPC client
        ├─ llm() / web_search()     → scoped one-shot SDK queries (subscription-billed)
        └─ idle watchdog (self-exits after PTC_IDLE_HOURS without a cell)
```

Three processes total, no broker daemon: **the kernel is the long-lived process** (spawned with
`start_new_session=True`, surviving Claude Code exit and adapter crashes), and both the MCP
adapter and the CLI are thin, stateless-ish Jupyter clients over the same connection file. The
Jupyter shell channel serializes cell execution kernel-side, so two clients cannot interleave
cells. The only host→kernel channel is environment at spawn; the only kernel→host channel is
Jupyter output. There is no reverse "host bridge" (Prime's `host_request` comm) because children
are spawned *from inside* the kernel — nothing authoritative lives in the adapter.

### Prior art this design leans on (pointers, not dependencies)

- **Prime Agent** (`ptc-surface/prime-agent/`): the doctrine text
  (`packages/coding-agent/src/core/prompts/rlm.ts`), the single-tool shape
  (`src/core/tools/ipython.ts`, 64 KiB/stream truncation, no cell timeout, interrupt-based),
  `edit` skill semantics (`skills/edit/`), admission-only `rlm()` (which we deliberately do NOT
  copy — see Decision Log).
- **Codex code-mode** (`codex-rs/code-mode-protocol/src/description.rs`): the `exec`/`wait`
  yield protocol, `cell_id` resumption, per-call output token budgets.
- **CC-to-SDK** (`CC-to-SDK/`): the Claude Agent SDK capability map (probes), the
  `codex app-server` client shape (`codex-plugin-cc/plugins/codex/scripts/lib/app-server.mjs`),
  OAuth-vs-API-key billing facts (probe 28).

## The kernel layer

### Provisioning

`~/.ptc/venv`, created and refreshed by `uv` (`ptc setup`, auto-run on first use):

- Python **3.12** (`uv python install 3.12; uv venv ~/.ptc/venv --python 3.12`).
- Packages: `ipykernel`, `jupyter_client`, `nest_asyncio`, `claude-agent-sdk`, `httpx`,
  `markdownify`, `pydantic`, plus the local `ptc` package (editable when run from a
  checkout) and convenience extras `pyyaml`, `pandas`, `numpy`, `matplotlib` (matplotlib so
  the inline backend publishes figures through the display shim).
- A `~/.ptc/venv/.ptc-version` stamp (package version + python version + dependency-set hash);
  mismatch triggers re-provision. A lock directory guards concurrent provisioning.

**Not** in the venv: `anthropic` (no API billing path), project dependencies (the skill forbids
installing a target project's deps into the kernel — run project code through the project's own
environment, verbatim Prime doctrine).

### Spawn and keying

A kernel is spawned on first `exec` for a session key:

```
~/.ptc/venv/bin/python -m ipykernel_launcher -f ~/.ptc/kernels/<key>/connection.json
```

- `start_new_session=True` (detached; survives the parent), `cwd` = the session's project
  directory, stdout/stderr → `~/.ptc/kernels/<key>/kernel.log`.
- Environment: the adapter's environment, minus nothing, plus
  `PTC_SESSION=<key>`, `PTC_CWD=<cwd>`, `PTC_DEPTH=<n>`, `PTC_MAX_DEPTH`, and the tunables below.
- **Spawn ownership (race-proof).** All spawn/respawn/reap paths take a per-key `flock` on
  `~/.ptc/kernels/<key>/lock`. Under the lock, the spawner writes `owner.json`
  `{pid, proc_start_time, spawned_at, nonce}` (process identity = pid **plus** its OS start
  time, so PID reuse can never be mistaken for liveness) and publishes a `ready` marker only
  after a successful `kernel_info` round-trip. Clients treat a kernel as live only when
  `owner.json` identity matches the running process; anything else is dead state to be cleaned
  under the same lock. `connection.json` mode 0600.
- **Kernel metadata.** `meta.json` `{kernel_key, claude_session_id?, cwd, transcript_path?,
  depth}` — written at spawn from the best identity available. The kernel key and the Claude
  session id are *distinct fields*: `agent.fork` and `history()` use `claude_session_id` from
  `meta.json`, never the kernel key, so an alias-keyed kernel (fallbacks 3–5 below) degrades
  those two features explicitly (`RuntimeError("no claude_session_id known for this kernel")`)
  instead of resuming a wrong or nonexistent session.
- A bootstrap cell (executed by the client that spawned the kernel while the key lock is
  held; `ready` is written only after it succeeds, so `ready` means *bootstrapped*) binds
  the runtime API into the namespace, installs the per-cell output tee, the terminal-record
  hooks, the display shim, and the idle watchdog, and sets `NO_COLOR=1` / uncolored IPython.
  `nest_asyncio` is applied only if spike S1 proves it necessary — ipykernel supports
  top-level `await` natively and the runtime API never nests `run_until_complete`.

**Session-key resolution**, in order (first hit wins):

1. Explicit `session` argument on the MCP tool call / `-s` on the CLI.
2. The plugin's **SessionStart hook** run-file. The hook receives `session_id` and `cwd` on
   stdin (Claude Code's hook input contract) and writes
   `~/.ptc/run/claude-<claude-pid>.json = {"session_id": ..., "cwd": ..., "written_at": ...}`.
   Hooks are launched through a shell, so the hook's *immediate* parent may be a transient
   `sh` — the hook therefore **walks its ancestor chain to the nearest process whose command
   is `claude`** and keys the file by that pid. The MCP adapter, a direct child, reads
   `~/.ptc/run/claude-<its own PPID>.json` (verifying that pid is alive and named `claude`,
   else walking up the same way). The hook fires on every session start including `--resume`,
   rewriting the file. Stale files (dead pid) are garbage-collected opportunistically.
3. `PTC_SESSION` env (set for kernels' own child processes, so a child's adapter joins the
   child's key, not the parent's).
4. `CLAUDE_CODE_SESSION_ID` env (present in Bash-tool environments; makes the CLI correct by
   default).
5. Fallback: `adapter-<adapter pid>` (functional, but not resume-stable) — degraded keying is
   stated in **every** tool result header (not just the first), and `meta.json` records no
   `claude_session_id`, which disables `agent.fork`/`history()` with an explicit error rather
   than silently touching a wrong session. The CLI's newest-live-kernel default likewise always
   prints which kernel it picked.

The skill additionally interpolates `${CLAUDE_SESSION_ID}` (Claude Code substitutes it inside
SKILL.md at load), so the model itself knows the session id and can pass `session=` explicitly if
resolution ever misfires. Same key ⇒ same kernel: `claude --resume <S>` reattaches to S's live
kernel with the namespace intact.

### Per-cell capture, audit, watchdog

- **Output tee (kernel-side)**: an IPython `pre_run_cell`/`post_run_cell` hook pair tees
  `sys.stdout`/`sys.stderr` for every cell into `~/.ptc/kernels/<key>/cells/<execution_count>.log`
  (raw, uncapped). Because capture is kernel-side, it works identically for MCP and CLI clients
  and survives adapter restarts; `wait` is implemented against these files.
- **Terminal record (kernel-side)**: `post_run_cell` also writes `cells/<execution_count>.json`
  atomically (`.tmp` + rename): `{status: "ok"|"error"|"interrupted", duration_ms,
  result_repr?, error?: {ename, evalue, traceback}, images: [saved paths], mutations: [...]}`.
  Streams alone cannot reconstruct a finished cell for a client that subscribed late (iopub is
  broadcast-only); the record makes completion, result, error, and duration recoverable by a
  **fresh adapter** — a cell is "running" iff its log exists and its record does not and the
  kernel process is live. On each kernel (re)spawn the previous epoch's `cells/` directory is
  rotated to `cells-prev-<ts>/`, and the new kernel's counters are continued **above the
  highest archived cell id** (monotonic across epochs), so a cell id can never be reused;
  `wait` on an archived cell settles immediately from the archive, labeled as belonging to
  a previous kernel epoch. Display-data images are captured kernel-side (a display-publisher
  shim saves PNGs to `cells/<n>-<k>.png` and lists them in the record).
- **Audit log**: every mutation made through the runtime API appends a JSON line to
  `~/.ptc/kernels/<key>/audit.jsonl`:
  `{ts, cell, kind: "write"|"edit"|"bash"|"agent", path?, added?, removed?, command?, task?, provider?}`.
  Raw `open(...,"w")` writes are *not* captured — the audit trail is visibility for the
  cooperative path, not enforcement (Trust model).
- **Idle watchdog (kernel-side thread)**: self-exits the kernel after `PTC_IDLE_HOURS`
  (default **24**) with no cell execution. It exits **under the per-key lock**: takes the flock,
  re-checks idleness, writes an `expired.marker` (timestamp + idle duration), removes
  `owner.json`, then exits — so a concurrent client either sees the live kernel or a clean
  expiry, never a half-dead one. The next `exec` on that key reports plainly: "previous kernel
  expired after N h idle; starting a fresh namespace (agent sessions remain resumable via
  `agent.list()`)". Every persistence promise in this spec and in the skill is qualified by
  this TTL. `ptc list` shows last-used times; `ptc kill`/`ptc restart` are the manual controls.

## The MCP adapter (`ptc-mcp`)

A stdio MCP server (Python `mcp` SDK), registered by the plugin's `.mcp.json`. It declares
server-level `instructions` (a ~10-line condensation of the skill: persistent namespace, assign
don't print, yield/wait, `session=` escape hatch) so the basics survive even when the skill is not
loaded. Five tools:

| tool | params | behavior |
|---|---|---|
| `exec` | `code: str` (required), `session?: str`, `timeout_s: int = 300`, `max_output_chars: int = 12000` | Run a cell. The idle-check **and** submit happen under the per-key submit `flock` (all local clients share it), so exactly one client wins: the winner submits and streams until done or `timeout_s`, then returns the finished result or yields `status: running` + `cell_id` + partial output + `next_offset`; every loser gets `status: busy` + the running `cell_id` + guidance (`wait`, `interrupt`, or resubmit later). Nothing is ever silently queued. |
| `wait` | `cell_id: int`, `session?`, `timeout_s: int = 300`, `max_output_chars: int = 12000`, `since: int = -1`, `until?: str` | Return output produced after offset `since` in `cells/<id>.log` (default `-1` = the offset this adapter last served, else 0), plus the cell's state from the terminal record. Returns `next_offset` — a caller-held cursor, so retries are idempotent and two waiters cannot consume each other's output. Completion (status, result repr, error, duration, images) comes from `cells/<id>.json`, so a **fresh adapter** can settle a cell it never started. May yield again. `until` is a Python regex that makes the wait an **event**: it returns `status: running` with `matched` as soon as NEW output first matches, rather than at settle — a long budget plus `until=` turns the completion notification into a trigger. First match wins; the window is one bounded read; completion supersedes the scan; an uncompilable pattern is a tool error. |
| `interrupt` | `session?` | `interrupt_request` on the control channel, then SIGINT after 2 s if still busy. Returns the interrupted cell's tail. |
| `restart` | `session?` | Shut down + respawn + re-bootstrap. States plainly that the namespace was lost and child agents remain resumable via `agent.list()`. |
| `kernels` | — | List known kernels: key, pid, alive?, cwd, last-used, depth. |

### Result shaping (shared by MCP and CLI)

```
[cell 14 · ok · 1.8s]
<stream output — stdout and stderr interleaved in arrival order, as the kernel-side tee
 captured them (one merged log per cell; IPython's own tracebacks appear here)>
<→ result: repr, if the cell's last expression produced one>
<on error: ename+evalue appended when the traceback text is not already in the stream>
edited src/a.py (+3/−1) · wrote notes/out.md · ran: npm test · spawned agent "api-reviewer"
```

- Header states cell id, status (`ok | error | running | busy | interrupted`), duration, and —
  when keying is degraded (fallback 5) — a `[keying: adapter-local]` note. A wait that returned
  early on its `until=` pattern reads `running · matched`, and its guidance line quotes what
  matched and says the cell is still running.
- **Truncation**: head+tail slices totaling `max_output_chars`, elision marker
  `… [truncated N chars — full output: ~/.ptc/kernels/<S>/cells/14.log]`. `max_output_chars`
  is clamped server-side to 50 000; an aggregate response budget (~4 MB text+images) bounds the
  whole reply regardless of caller arguments. Both keep results far under Claude Code's default
  25 000-token MCP output ceiling (`MAX_MCP_OUTPUT_TOKENS`).
- **Mutation footer** built from the cell's `audit.jsonl` entries (see above) — one line, only
  when mutations occurred.
- **Images**: `display_data` with `image/png`/`image/jpeg` (matplotlib, PIL) becomes an MCP image
  content block after the text block, capped at 2 per cell and ~1.5 MB each; always also saved to
  `cells/<id>-<k>.png` — the file is on disk and listed in the kernel dir, but the shaped text
  does **not** name it. That saved-path line was designed and never implemented; T28 re-confirmed
  it parked (recorded residual — see Surprises & Discoveries and the M3 close-out). (Spike S5
  verifies Claude Code renders the block; per S5's verdict the saved file is the human-side half
  of the same result, not a fallback for the block.)
- **No `structuredContent` in v1.** Claude Code's handling of `structuredContent` can take
  precedence over the content array (stringifying it and discarding formatted text/image
  blocks), which would defeat the shaped result. The MCP reply is the content array alone; the
  machine-readable form lives in `ptc wait/exec --json` on the CLI, which does not cross MCP.

Long-cell safety: Claude Code's MCP tool timeout defaults to ~27.8 h (`MCP_TOOL_TIMEOUT`), so the
adapter's own `timeout_s` yield is the binding limit, never the host's.

## The CLI (`ptc`)

Console script in the venv, also invocable as `uv run ptc`. Shares every code path with the
adapter (same client library, same result shaping, text output).

```
ptc setup                       # provision/refresh ~/.ptc/venv
ptc exec [-s KEY] [-t SECS] [--json] [CODE | -]   # run a cell (CODE arg, or stdin with -)
ptc wait  -s KEY CELL_ID [-t SECS] [--since N] [--until REGEX] [--json]   # --json: the machine-readable result object
ptc interrupt [-s KEY]
ptc restart   [-s KEY]
ptc list                        # kernels + last-used + alive
ptc kill [-s KEY | --all]
ptc doctor                      # venv state, versions, run-file visibility, claude/codex on PATH
```

`--json` is accepted by **every** subcommand and honored by every one of them: `exec`/`wait`
emit the result object above, and the rest emit one line of JSON describing what they did
(`{"kernels": [...]}`, `{"venv": …}`, `{"key": …, "killed": …}`, `{"key": …, "interrupted":
true}`, `{"key": …, "pid": …, "cwd": …, "namespace_lost": true}`; `doctor`'s report is its
human output on one line). Human output stays the default and is unchanged. A flag a shared
parser advertises on every command has to work on every command — advertised-and-ignored is
a contract break for exactly the caller who read the help text (final review r4, finding 7).

Default `-s`: `$PTC_SESSION`, else `$CLAUDE_CODE_SESSION_ID`, else the newest live kernel (with a
notice). The CLI exists for humans debugging a kernel and for harnesses without MCP; the skill
does not teach it.

## The in-kernel runtime API

Pre-bound by bootstrap (also importable: `from ptc.runtime import *`). Local, instant operations
are **sync**; anything crossing a process or the network is **async** (top-level `await` works in
IPython, and `asyncio.gather` is the fan-out idiom). All functions raise normal Python exceptions.

### Files

```python
read(path, offset: int | None = None, limit: int | None = None, numbered: bool = False) -> str
write(path, content: str) -> str            # creates parents; audits {kind:"write", path, added=<lines>}
edit(path, old: str, new: str, replace_all: bool = False) -> str
```

`edit` is Prime/Claude-Code-exact: `old` must match **exactly once** (unless `replace_all`);
0 matches → `ValueError("string not found …")`; >1 → `ValueError("found N occurrences …— widen
the snippet")`. Returns `"Edited <abspath> (+a/−r)"` and audits with line-delta counts.

### Shell

```python
await bash(cmd, timeout: float = 120, cwd=None, env=None, background: bool = False)
    -> BashResult(code, stdout, stderr, timed_out)          # background=False
    -> BashHandle(.pid, .poll(), await .wait(), .output(), .kill())   # background=True
```

`asyncio.create_subprocess_shell` under the kernel's loop; audits `{kind:"bash", command}`.
IPython's own `%%bash` cells and `%cd` keep working and the skill carries Prime's verbatim
mechanics (first-line rule; each `%%bash` is a throw-away subshell; `%cd` + `os.environ` persist).

### Agents

```python
await agent.run(task, *, provider="claude",           # "claude" | "codex"
                model=None, system=None, allowed_tools=None,
                permission_mode="bypassPermissions", cwd=None,
                max_turns=None, effort=None, output_schema=None,
                timeout=None) -> AgentResult
agent.spawn(task, *, name=None, **same_options) -> AgentHandle      # starts immediately, sync return
await agent.fork(task, **same_options) -> AgentResult               # child inherits THIS conversation
await agent.gather(*handles) -> list[AgentResult]
agent.list() -> list[AgentInfo]        # live + registry (survives kernel restart)
agent.resume(session_id, **options) -> AgentHandle
```

- `AgentResult(text, session_id, structured, cost_usd, num_turns, duration_ms)`;
  `structured` is populated when `output_schema` (a JSON Schema dict) was given.
- `AgentHandle`: `.name`, `.session_id`, `.status` (`running|done|error|interrupted`),
  `await .result()`, `await .send(msg)` (a follow-up turn on the same session — this is the
  SendMessage equivalent), `.messages()` (transcript so far), `.history()` (T25: the child's
  own `Transcript`, resolved by the child's session id — the handle never tracked the child's
  cwd, so it passes none, but `history()` still probes the KERNEL's `meta.json` cwd first and
  that direct path hits whenever the child ran in the kernel's own directory, which is the
  default; the glob covers a child spawned with a different `cwd=`), `.interrupt()`,
  `.close()`. **`.interrupt()` follows S1's terminal shape**: it signals the session and then
  *waits the drain out*, because an interrupted turn ends with a normal `ResultMessage`
  (`terminal_reason='aborted_streaming'`, no exception, 4–13 s). The handle therefore settles
  through its ordinary completion path — `result()` returns the aborted turn's partial result —
  and only its `.status` and registry row become `interrupted`. A drain that outlives the
  interrupt budget is treated as wedged: the driver is cancelled and `result()` raises instead.
  Two honest consequences of waiting the drain out: `.status` (and the registry row, so
  `agent.list()`) reads `running` for the whole drain — up to the 30 s budget — and only flips
  to `interrupted` once the drain ends; and if the caller's own `timeout` expires *during* a
  drain, the turn ends `interrupted` while `result()` raises the driver's `TimeoutError` rather
  than an interrupt error — a known asymmetry, one settlement and one row either way.
  A handle with **no session yet** (queued on the concurrency semaphore, or mid-connect) has
  nothing draining, so `interrupt()` pre-empts its driver at once and `result()` raises;
  waiting the budget out there would let the queued turn run — and bill — to completion.
- `agent.resume(session_id)` opens a session with no turn in flight: the handle is `done` on
  arrival and exists to be `send()`-ed to. It is subject to the same depth brake as
  `run`/`spawn`/`fork` (it can drive unbounded turns), writes the same audit record, and its
  registry row carries the resumed id — the row is what makes a session resumable after a
  kernel restart, so it must never be null.
- **Kernel death reaps the kernel's children.** Agent CLIs are spawned without a session of
  their own, so they live in the kernel's process group; `kill_kernel`/`restart` and the idle
  watchdog therefore kill the *group*, not the pid (SIGKILL and `os._exit` both bypass the
  SDK's `atexit` reaper). Background `bash()` children get their own session and are reaped by
  the shell layer instead.
- **Claude backend**: `claude-agent-sdk` in-kernel, version-pinned in the venv. `run` =
  `query(...)`; `spawn` = `ClaudeSDKClient` with its own session (streamed to a registry entry
  as it goes); `fork` = `resume=<claude_session_id from meta.json>` + `fork_session=True`
  (errors explicitly when the kernel has no known Claude session id). Children get `cwd` =
  kernel cwd and the user's default model unless `model=` given. Two child-env rules are
  load-bearing: (1) **`PTC_SESSION` is always overridden** to a fresh child key — a child must
  never inherit the parent's key, or its adapter would attach to the parent's kernel; (2)
  `PTC_DEPTH=<depth+1>` is set alongside it. Children do not inherit `--plugin-dir`, so the
  ptc capability is passed explicitly: `mcp_servers={"ptc": {command: <launcher>}}` in the SDK
  options (tools without the skill; the server `instructions` carry the basics). Every SDK call
  releases the concurrency semaphore in `try/finally` and kills its CLI process tree on
  cancellation, so a hung child can be `h.interrupt()`ed without leaking permits.
- **Codex backend**: a ~200-line stdio JSON-RPC client that spawns `codex app-server` and speaks
  `initialize {clientInfo:{name,version}}` → `initialized` (a *notification*; the handshake is
  two messages and any request before it is refused) → `thread/start {cwd, approvalPolicy:
  "never", sandbox: "read-only", model?}` → `turn/start {threadId, input:[{type:"text",text}]}`
  → collect `item/completed` agentMessage items until `turn/completed`, taking the text from the
  items and only the status from `turn/completed`. `sandbox` here is the kebab-case
  `SandboxMode` enum, unlike the camelCase `SandboxPolicy` object that per-turn overrides and
  all responses use. The turn id is retained from the `turn/start` response (`result.turn.id`)
  because `turn/interrupt` needs `{threadId, turnId}`. `send` = another `turn/start` on the same
  thread; `resume` = `thread/resume`; `fork` → `NotImplementedError("codex fork; use
  provider='claude'")`. Any server→client request that arrives despite the policy is
  auto-answered with that method's own response shape (not a blanket `{"decision":"accept"}`,
  which most of them reject) and logged; a method PTC cannot service gets a real JSON-RPC
  error, never an empty `{}`, because a malformed result is indistinguishable from no reply.
  Approvals of a command or a file change accept; anything asking for user data or consent
  declines rather than inventing an answer. The child is spawned with
  `--disable hooks --disable plugins` so the user's `~/.codex` hooks and plugins stay out of it
  (T22 Decision Log), and with an environment BUILT from an allowlist rather than inherited —
  Claude-side credentials do not cross into another vendor's binary, and PTC's own
  `PTC_SESSION`/`PTC_DEPTH` are rewritten for the child exactly as they are for a claude one.
  `AgentOpts.system` rides as `developerInstructions`; `allowed_tools`, `max_turns` and a
  non-default `permission_mode` have no codex analogue and raise rather than being dropped.
  Spike S4 pins all of this live.
- **Fan-in semantics (deliberate divergence from Prime)**: results are awaited **in code** —
  `await h.result()` blocks the *cell*, not the model; the exec/wait yield keeps the model free.
  Handles persist in the namespace across turns, so "spawn now, gather next turn" works without
  any host-side message injection (which Claude Code cannot do).
- **Depth guard — a cooperative recursion brake, not a boundary.** `PTC_MAX_DEPTH` (default 1).
  At `PTC_DEPTH >= PTC_MAX_DEPTH`, `agent.run/spawn/fork` raise
  `RuntimeError("agent depth limit reached (PTC_DEPTH=1, PTC_MAX_DEPTH=1); raise PTC_MAX_DEPTH to allow grandchildren")`.
  This is an env check inside a process that exposes arbitrary Python: a determined child can
  spawn agents by other means (native Agent tool, its own SDK install). The guard exists to stop
  *accidental* recursion storms on the cooperative path; the actual boundary remains the
  `mcp__plugin_ptc_ptc__exec` allow decision (Trust model).
- **Concurrency**: one semaphore (default 8, `PTC_MAX_CONCURRENCY`) across all SDK-spawning
  calls (`agent.*`, `llm`, `web_search`) bounds subprocess storms.
- **Registry**: `~/.ptc/kernels/<key>/agents.json` — `{name, provider, session_id/thread_id,
  task_head, status, created_at, last_turn_at, parent_session_id}`; written on every state
  change; `agent.list()` merges live handles with the file so a restarted kernel can still
  `agent.resume(...)`. `parent_session_id` is populated only for `agent.fork` entries, from the
  parent sid already known to the kernel (the `resume=` argument, sourced from `meta.json`) at
  fork time — a forked child's own JSONL carries no back-pointer to its parent (S2 evidence),
  so this is the only place the relation is recoverable from disk.

### Sub-LM calls

```python
await llm(prompt, *, model="haiku", system=None, json_schema=None,
          timeout=300) -> str | dict
```

One-shot SDK `query()` with **no tools** (`tools=[]`, `max_turns=1`), `json_schema` mapped to the
SDK's `output_format={"type":"json_schema", "schema": ...}` (returns a parsed dict). This is the
RLM `llm_query` primitive for semantic map-reduce:
`await asyncio.gather(*[llm(f"Classify:\n{c}") for c in chunks])`. Subscription-billed via the
`claude` CLI's own auth. No API client exists in this codebase.

### Web

```python
await web_fetch(url, *, prompt=None, timeout=30) -> FetchResult(url, status, title, text)
await web_search(query_text, *, allowed_domains=None, blocked_domains=None,
                 max_results=10, timeout=300) -> list[SearchResult(title, url, snippet)]
                 # .raw keeps the source block
```

- `web_fetch`: `httpx` GET (redirects followed, 10 MB cap enforced *while streaming*) →
  `markdownify` when the response is HTML, otherwise the body verbatim → full text stays in
  the returned object (the PTC spirit: filter in code, don't summarize by default). `prompt=`
  runs `llm(prompt, over the text)` and fills `FetchResult.summary`. The fetch takes a permit
  from the shared pool and **releases it before summarizing**: `llm()` draws on the same pool,
  so holding across the model call would deadlock every caller at `max_concurrency`. `timeout`
  is therefore the fetch deadline (queue wait included); the optional summarization runs under
  `llm()`'s own.
- `web_search`: a scoped one-shot SDK query allowed only the `WebSearch` tool; the runtime
  **parses the `WebSearch` tool_result block out of the message stream** and returns the
  structured results, discarding the model's prose (spike S6 pins the block shape). Whatever S6
  finds, the return type is always `list[SearchResult]` — under the S6 fallback the parse is
  best-effort and `SearchResult.raw` retains the source block for the caller. Domain filters
  pass through to the tool input. Subscription-billed.
  Post-S6 corrections to this contract, all live-pinned: `.snippet` is **empty** on the shape
  the tool actually returns (title and url only) — it stays in the dataclass for the fallback
  path and any richer future payload, and callers must read it, never require it. Domain
  filters are prompt hints (the tool's own inputs are the model's to set) and are **also
  re-applied to the returned urls**, so a caller that asked for a restriction gets one whether
  or not the hint was honored. Results are correlated back to a `WebSearch` `tool_use` id, so
  another tool's output can never be scraped for urls; with no correlation available the parse
  widens to every tool result rather than returning nothing. `max_results` is
  **truncation-only**: the model decides how many hits the search tool turns up (S6 and A7 both
  observed 8) and PTC slices the list to at most `max_results` — a caller asking for more than
  the tool returned silently gets fewer, and there is no way to make the tool search harder.
  The prose-scrape fallback has **never fired against real data**: every live response seen so
  far carried a well-formed `Links:` line, so that branch is pinned only by a synthetic fixture
  in `test/unit/test_web.py` (insurance against a shape change, not an evidenced path).

### History (lossless memory)

```python
history(session: str | None = None, cwd: str | None = None) -> Transcript
Transcript: .path, .messages (list[dict]), .user(), .assistant(), .tool_calls(name=None),
            .search(regex), .text()          # convenience projections over the raw JSONL
```

Resolves `~/.claude/projects/<munged-cwd>/<session>.jsonl` (munge: non-alphanumeric → `-`);
falls back to globbing `~/.claude/projects/*/<session>.jsonl`. Default session = the kernel's
`claude_session_id` from `meta.json` (explicit `RuntimeError` when the kernel is alias-keyed
and none is known); the cwd the munge is built from comes off that same `meta.json` dict, with
the kernel process's own `PTC_CWD` as fallback — the bootstrap config carries no `cwd`, so
sourcing it there would leave the direct-path tier dead in every real kernel. This is the
PRO-LONG-style lever: pre-compaction history stays queryable as data.
`AgentHandle.history()` returns the same type for children.

### Workflow helpers

```python
await workflow.parallel(*aws, limit=8) -> list        # gather with a bound + per-item error capture
await workflow.pipeline(items, *stages, limit=8) -> list   # per-item stage chaining, no inter-stage barrier
workflow.phase(name)                                  # progress marker printed + audited
```

Doctrine, not machinery: the skill shows Workflow-tool-style patterns (fan-out → verify →
synthesize) written as plain cells.

`limit` is a **per-fan-out** bound over arbitrary awaitables, not a second concurrency
pool: `agents.shared_semaphore` bounds only what spawns a CLI subprocess, and most of
what these helpers fan out (file reads, `bash`, pure coroutines) it never covered. Both
apply when the awaitables are SDK calls and the tighter governs; acquisition is always
this permit then the shared one, never the reverse. `pipeline` carries the same bound
(keyword-only, after `*stages`) — T26 added it because per-item fan-out over a caller's
list is the identical hazard, and a `bash`/`web_fetch` stage escapes the shared bound
entirely. `pipeline` stages may be sync or async: an await-only contract would turn every
plain-function stage into an opaque `TypeError` captured as that item's result, silently
poisoning the batch. Error capture is `except Exception`, never `BaseException`, so an
interrupted fan-out unwinds instead of reporting success; awaitables still queued for a
permit when a fan-out is cancelled are closed, so an interrupt does not spray
"coroutine … was never awaited" into the cell's own output. `parallel` and `pipeline` do
not audit — every primitive they orchestrate audits itself, and only `phase` is a record
in its own right (`shape.footer_line` ignores kind `phase` by design; phases show up in
stdout).

## Skill and packaging

### Repository layout — `ptc-surface/ptc/`

```
ptc/
  pyproject.toml            # package "ptc"; console scripts: ptc, ptc-mcp
  src/ptc/
    kernel.py               # spawn/discover/lifecycle, venv provisioning, run-file GC
    client.py               # jupyter_client wrapper: execute, follow, interrupt (shared MCP/CLI)
    shape.py                # result shaping + truncation + mutation footer
    runtime/                # what gets imported INTO the kernel
      __init__.py           #   read/write/edit/bash/llm/web_fetch/web_search/history/workflow
      agents.py             #   agent.* – SDK backend, registry
      codex.py              #   codex app-server client
      bootstrap.py          #   bound at kernel start; tee, watchdog, nest_asyncio
    mcp.py                  # the MCP server (ptc-mcp)
    cli.py                  # the ptc CLI
  plugin/
    .claude-plugin/plugin.json
    .mcp.json               # {"mcpServers": {"ptc": {"command": "${CLAUDE_PLUGIN_ROOT}/bin/ptc-launch"}}}
    bin/ptc-launch          # checked-in stdlib-python launcher: provisions ~/.ptc/venv if
                            #   missing/stale, then execs <venv>/bin/ptc-mcp — so a clean
                            #   profile can start the server that installs itself
    hooks/hooks.json        # SessionStart → writes ~/.ptc/run/claude-<pid>.json (tree-walk)
    skills/ptc/SKILL.md
  test/                     # unit / integration (real kernel) / live (auth-gated) / acceptance
  README.md                 # install, trust model, Codex registration note
```

### SKILL.md content contract

Frontmatter `name: ptc`, description tuned to trigger on: bulk file analysis, many-step
programmatic work, fan-out/aggregation, long-running loops, multi-agent orchestration, "keep this
in a variable". T17 shipped a deliberately narrowed v0 description while `agent`/`llm`/web were
still stubs; T27 restored the full scope once every runtime tool had landed, so the description
now names orchestration and fan-out again — it must keep tracking what is actually bound.
Body teaches, in **eleven** sections, in order (Prime's doctrine transposed, tightened for a
hybrid harness — the count was ten until `workflow` shipped in T26 and earned §9):

1. **What the kernel is** — persistent notebook; variables/imports/handles survive calls, turns,
   compaction, and `--resume`, until the kernel's idle TTL (default 24 h) or a restart. Session
   id available as `${CLAUDE_SESSION_ID}` for explicit `session=`.
2. **When to use it vs native tools** — use PTC for bulk read/filter/transform, fan-out,
   aggregation, iterative loops with state, agent orchestration; use native Edit for a single
   known edit, native Read for images/PDFs/notebooks, native tools whenever the user should see
   and approve each step. (No search guidance — deliberate omission.)
3. **Working discipline** — assign large results to named variables and print compact summaries;
   output truncates at ~12k chars with the full log path; never poll with `time.sleep`, use
   yield/wait; project code runs in the project's own environment, never install project deps
   into the kernel.
4. **Files & shell** — `read/write/edit` semantics (uniqueness rule verbatim), `await bash(...)`,
   `%%bash` first-line rule + throw-away-subshell mechanics, `%cd`/`os.environ` persistence.
5. **Agents** — the four forms with one-line use cases: `run` (blocking one-shot), `spawn`+
   `gather` (parallel fan-out), `fork` (child inherits this conversation — "ask about our
   discussion"), `send` (follow-up on a persistent child); `provider="codex"` for a Codex
   worker, **with the per-provider option matrix stated** (T22: `allowed_tools`, `max_turns`
   and a non-default `permission_mode` raise on codex; `system=` becomes
   `developerInstructions`; `model`/`effort`/`output_schema`/`cwd`/`timeout` work on both;
   `fork` is claude-only) — an option silently dropped is worse than one that raises, and the
   skill must not let the model discover that at runtime; depth and concurrency limits;
   children run under `bypassPermissions` — delegate only work you'd run yourself.
6. **llm map-reduce** — chunk → `gather(llm(...))` → synthesize; `json_schema` for structured
   labels; the ~5.2k prompt-token floor, so it is not a string op.
7. **Web** — `web_fetch` keeps full text in the variable (filter in code); `web_search` returns
   structured results, with `.snippet` empty and `max_results` truncation-only.
8. **History** — `history()` for anything pre-compaction; `handle.history()` for children.
9. **Workflow helpers** — `parallel`/`pipeline`/`phase` as *doctrine, not an engine*: a cell of
   Python is the workflow; the bound and the in-order per-item error capture are what they add;
   `spawn` handles belong in `agent.gather`, not `parallel`; `fork` is never a loop body.
10. **Pitfalls** — do not invent wrappers that don't exist (verbatim Prime's negative
   instruction, adapted); the kernel is not the project's runtime; restart loses variables but
   not agent sessions.
11. **Worked examples** — short end-to-end cells: bulk audit; spawn-and-gather; fork-recall.

### Installation modes

- Dev: `claude --plugin-dir ptc-surface/ptc` (r11: the package IS the plugin root).
- Settings snippet (README): allow `mcp__plugin_ptc_ptc__*` in `permissions.allow` for
  prompt-free use (plugin installs); a directly registered server is `mcp__ptc__*`.
- First run: the launcher provisions `~/.ptc/venv` inside Claude Code's 30 s MCP startup
  window (3.75 s measured, warm uv cache). On a cold cache run `ptc setup` once first.
- Codex (documented only): `codex mcp add ptc -- <plugin>/bin/ptc-launch` — works because the
  adapter is plain stdio MCP; session keying degrades to explicit `session=`/`PTC_SESSION`.

## Trust model (README + skill, stated identically)

Allowing `mcp__plugin_ptc_ptc__exec` **is** the security decision: from then on, model-written
Python runs with your OS permissions, outside Claude Code's per-tool permission prompts and
sandbox, and children spawned from the kernel default to `bypassPermissions`. The audit footer
and `audit.jsonl` give visibility, not enforcement. Use a worktree/container for untrusted work.
The kernel env should not carry `ANTHROPIC_API_KEY` if you want subscription billing — the key
shadows OAuth in the `claude` CLI and silently flips billing to metered API. The kernel inherits
the adapter's process environment verbatim, which includes credential-bearing `CLAUDE_*`
variables (a Claude Code tool environment carries an OAuth bearer token, `sk-ant-oat…`, among
them — S2 evidence). Any PTC surface that logs or forwards environment (debug output, audit
records, the T20/T21 child-env plumbing) must redact credential-bearing variables before writing
them anywhere durable: match by name (`KEY|TOKEN|BEARER|SECRET`) and by value prefix
(`sk-ant-`), not pass the environment through unfiltered.

The same boundary covers `provider="codex"`, and one consequence of it is worth naming
outright: a codex child is pinned to `sandbox: "read-only"`, and read-only in codex means
*no writes*, not *no reads* — `SandboxPolicy::has_full_disk_read_access` returns `true` for
every policy including `ReadOnly` (`codex-rs/protocol/src/protocol.rs:1152-1153`). With the
real `HOME`/`CODEX_HOME` forwarded so subscription auth works, such a child can read any file
this user can, `~/.claude` included, and anything it reads leaves through the model request —
so this one crosses the VENDOR boundary that the child-env allowlist otherwise holds. That is
the same trust class as the kernel itself, which runs arbitrary `bypassPermissions` Python:
the honest boundary is the `mcp__plugin_ptc_ptc__exec` allow decision, and the answer is
excellent visibility rather than a pretence of enforcement. The candidate hardening — a
read-restricted permission profile or a workspace-scoped sandbox for codex children — is
recorded as a residual, not shipped.

## Configuration reference

| env (config file mirrors) | default | meaning |
|---|---|---|
| `PTC_HOME` | `~/.ptc` | root for venv/kernels/run |
| `PTC_SESSION` | — | session key override; set on kernel + children |
| `PTC_DEPTH` / `PTC_MAX_DEPTH` | `0` / `1` | agent-spawn depth guard |
| `PTC_MAX_CONCURRENCY` | `8` | SDK-subprocess semaphore |
| `PTC_YIELD_S` | `300` | default `exec`/`wait` timeout_s |
| `PTC_MAX_OUTPUT_CHARS` | `12000` | default result cap |
| `PTC_IDLE_HOURS` | `24` | kernel self-reap (the TTL qualifying all persistence promises) |
| `PTC_CODEX_INHERIT` | unset | `1` restores the user's full Codex surface in `provider="codex"` children — PTC otherwise spawns `codex app-server --disable hooks --disable plugins`, which also removes plugin-provided skills. Credential stripping from the codex child's environment is unconditional and this knob does not affect it. |

## Delegated unknowns → spikes

Each spike is a small runnable probe committed under `ptc/test/spikes/`; promote/discard criteria
are binding.

- **S1 — SDK inside ipykernel's loop.** `claude-agent-sdk` (anyio) running on ipykernel's
  asyncio loop, including: two concurrent `ClaudeSDKClient`s under `asyncio.gather`;
  cancellation mid-stream; the CLI subprocess being killed mid-stream (the SDK has a reported
  indefinite-hang mode here — verify our timeout + process-tree kill unsticks it); semaphore
  release on every path; whether `nest_asyncio` is necessary at all (apply it only if the spike
  proves it is). *Promote* if all complete and the kernel stays responsive. *Fallback*
  (pre-designed): run all SDK I/O on one dedicated background thread with its own loop — the
  spike must then also exercise the cross-thread handle protocol (`spawn` on the kernel loop,
  `.result()` awaited from a cell).
- **S2 — live-session fork.** `resume=<parent session>, fork_session=True` while the parent
  Claude Code session is mid-turn (its JSONL partially flushed). *Promote* if the fork child
  answers a parent-only fact. *Fallback*: document fork as sound between turns; mid-turn the
  child sees the transcript up to the last flushed message (acceptable; note in skill).
  *Verdict (T18, live on 2.1.238): PROMOTE.* The child, forked from inside a Bash tool call
  the parent was still running, answered both a parent-only fact from the user prompt and a
  random phrase the parent assistant had invented *in that same in-flight turn*. Fallback
  wording is unnecessary: mid-turn *is* up-to-the-last-flushed-message, and Claude Code
  flushes per message, so the boundary is one message back, not one turn back.
- **S3 — hook discovery.** The SessionStart hook's ancestor tree-walk lands on the same
  `claude` pid the MCP adapter sees, in release Claude Code 2.1.236, including `--resume`
  (hook fires per-start and rewrites the run-file) and two concurrent windows in one cwd
  (each keyed to its own pid → its own session). *Promote* if `kernels()` shows the right key
  with zero configuration in all three scenarios. *Fallback*: chain already includes
  `${CLAUDE_SESSION_ID}` (skill) and `CLAUDE_CODE_SESSION_ID` (CLI; live-verified present in
  this project's own session on 2.1.236).
  *Verdict (T11, live on 2.1.238): PROMOTE.* Hook tree-walk pid and adapter-side walk agreed
  in four scenarios — fresh, `--resume` (same key, same kernel pid, namespace intact), two
  concurrent sessions in one cwd (distinct keys, no cross-talk), and a resumed session whose
  kernel had to be respawned. `kernels()` showed the right key with zero configuration in
  every one. The run-file path was verified by running T13's resolver algorithm against the
  live adapter's ancestry; `CLAUDE_CODE_SESSION_ID` independently agreed in all four.
- **S4 — headless codex approvals.** Exact `thread/start` params (`approvalPolicy`, `sandbox`)
  that produce zero server→client approval requests on current `codex app-server`
  (codex-cli 0.146.0). *Promote* when a trivial turn completes unattended. *Fallback*:
  client-side auto-accept of approval requests.
  *Verdict (T19, live on codex-cli 0.146.0): PROMOTE.*
  `thread/start {cwd, approvalPolicy: "never", sandbox: "read-only"}` — copy verbatim —
  completed a trivial turn in 5.2 s with **zero** server→client requests of any kind.
  `approvalPolicy: "never"` is proven (source-read, not live) to short-circuit exec,
  apply-patch, sandbox-escalation, `request_permissions`, and server-originated-elicitation
  approvals before any client request is emitted — but **not** MCP tool-call approvals: under
  `never` + `read-only`, `PermissionProfile::read_only()` fails the auto-approve check in
  `codex-mcp/src/mcp/mod.rs:85-105`, so a tool with no annotations (the default) still reaches
  `request_mcp_server_elicitation` or `request_user_input` — a real server→client request. T22's
  auto-accept responder is therefore **load-bearing, not belt-and-braces**, and must implement
  per-method valid replies: a blanket `{"decision": "accept"}` is invalid for eight of the ten
  server→client methods, and an invalid reply is indistinguishable from no approval arriving.
  For methods PTC cannot satisfy honestly (`attestation/generate`,
  `account/chatgptAuthTokens/refresh`), T22 should reply with a JSON-RPC error object, not an
  empty result.
- **S5 — MCP image blocks.** Claude Code 2.1.236 renders an image content block returned by an
  MCP tool. *Promote* → plots visible inline. *Fallback*: text mentions the saved PNG path only.
  *Verdict (T12, live on 2.1.238): PROMOTE.* The image block survives the host intact: the
  `tool_result` in `--output-format stream-json` carried a real base64 image block in the
  Anthropic content form, after the text block, whose decoded bytes hash to the same sha256
  as the PNG the display shim wrote. The terminal transcript marks it `[Image]` in the
  ctrl+O detail view — a marker, not a rendered plot. `_content` keeps emitting
  `ImageContent`; the PNG on disk stays as the durable copy, and M1 adds its path to the
  shaped text so a human reader can reach the file. Runbook: `test/spikes/s5_image_block.md`.
- **S6 — WebSearch tool_result shape.** The structured results block is reachable in the SDK
  message stream and parseable. *Promote* → clean field mapping into `SearchResult`.
  *Fallback*: best-effort extraction with `SearchResult.raw` retaining the source block —
  the return type is `list[SearchResult]` either way (A7 holds under both outcomes).
  *Verdict (T24, live on SDK 0.2.142): PROMOTE, with the carrier corrected.* The results
  are reachable and cleanly field-mappable, but they do **not** arrive as a structured
  block: `ToolResultBlock.content` is a plain **string**, and the machine-readable payload
  is a single `Links: [ … ]` JSON array embedded in it, carrying `title` and `url` per hit
  and no snippet. So the mapping is a JSON parse of that one line — deterministic, not a
  regex over prose — and the prose fallback is genuinely a fallback. `SearchResult.snippet`
  is honestly empty on this shape rather than backfilled from the model's write-up. Runbook:
  `test/spikes/s6_websearch_shape.py`; the observed block is the fixture in
  `test/unit/test_web.py`.

## Acceptance

All commands run from `ptc-surface/ptc/`. "A session" means `claude --plugin-dir ./plugin` (or
`claude -p` for scripted checks) with `mcp__ptc__*` allowed.

Two tiers, and the tiering is part of the contract:

- **Keyless tier (non-skippable).** Everything that needs only a real kernel — exec/wait/
  interrupt/restart, the busy protocol under two simultaneous clients, fresh-adapter recovery
  (kill the adapter after a yield, settle the cell from a new one), spawn-race (two concurrent
  first execs → one kernel), truncation, terminal records, audit, read/write/edit/bash, CLI
  parity — runs against the real ipykernel with **no Claude auth** and cannot skip. Agent
  registry/semaphore/timeout logic runs keyless against a fake backend (DI). A milestone's
  exit criteria are satisfied only by this tier plus the live tier actually passing —
  a skipped live test satisfies nothing.
- **Live tier (auth-gated).** A-cells as phrased below run inside a Claude Code session, so
  they are live-tier; they skip cleanly without auth, and milestone sign-off requires running
  them on an authenticated machine. A1, A3, A6, A10, and A12 additionally have keyless
  equivalents in the keyless tier (same behavior, driven straight over the Jupyter protocol),
  so the kernel substrate is provable without auth. A15 needs `codex` login as well.

- **A1 State persists across calls.** In one session: `exec("x = 42")`, later `exec("print(x)")`
  → `42`.
- **A2 State survives resume (within the TTL).** *live.* Quit; `claude --resume <S>
  --plugin-dir ./plugin`; `exec("print(x)")` → `42` on the same kernel (pid unchanged via
  `kernels`). Companion keyless case: with `PTC_IDLE_HOURS` set to a test-small value, an
  expired kernel's next `exec` reports the expiry notice and a fresh namespace.
- **A3 Yield/wait/interrupt + fresh-adapter recovery.** `exec("import time; time.sleep(600)",
  timeout_s=5)` returns `status: running` + cell_id within ~5 s; `wait(cell_id, timeout_s=5)`
  yields again; **kill the adapter, start a new one**, `wait(cell_id)` from it still tracks the
  cell; `interrupt()` settles it as interrupted (terminal record status `interrupted`); a
  following `exec("1+1")` returns `2`. While the cell runs, a second client's `exec` gets
  `status: busy` (never silent queueing).
- **A4 Fan-out/fan-in.** One cell: `hs = [agent.spawn(t) for t in tasks3]; print(len(hs))` ends;
  next cell `await agent.gather(*hs)` returns 3 results; `agent.list()` shows all three;
  after `restart()`, `agent.list()` still shows them and `agent.resume(<id>)` + `.send()` gets a
  reply.
- **A5 Fork recalls the parent conversation.** Mid-session (after discussing a marker fact),
  `await agent.fork("what marker fact did we establish? answer only the fact")` → the fact.
- **A6 Audit footer.** A cell calling `edit()` on a repo file returns a footer containing
  `edited <path> (+a/−r)`; `audit.jsonl` has the matching entry.
- **A7 web_search structured.** *live.* `await web_search("anthropic claude release notes")`
  returns ≥1 `SearchResult` with real URLs (holds under either S6 outcome). The test asserts
  the kernel env carries no `ANTHROPIC_API_KEY`; billing-mode itself follows the CLI's auth
  resolution and is an environment discipline (Trust model), not something this test can prove.
- **A8 llm map-reduce.** `await asyncio.gather(*[llm(f"one word for: {w}") for w in five])` →
  5 non-empty strings; with `json_schema` → parsed dicts.
- **A9 history().** After ≥2 turns, `history().user()` contains the first user prompt verbatim
  (and still does after a `/compact`).
- **A10 CLI shares the kernel.** From the session's Bash tool: `ptc exec 'print(x)'` → `42`
  (same kernel as A1). The CLI reaches that kernel through the session-discovery chain, whose
  top rung is the hook run-file — it outranks the `CLAUDE_CODE_SESSION_ID` env rung precisely
  because that variable goes stale across `--resume`, and the run-file is the rung that fired
  in T28's live run.
- **A11 Skill triggers.** `claude -p --plugin-dir ./plugin "analyze all python files under
  src/ for TODO density; keep intermediate data in variables"` → the transcript contains a
  `mcp__plugin_ptc_ptc__exec` call (the model chose the kernel unprompted).
- **A12 Truncation.** `exec("print('y'*100000)")` → result ≤ ~12k chars, contains the elision
  marker and a `cells/<n>.log` path whose file holds the full 100k.
- **A13 Images.** A matplotlib cell yields an image block visible in Claude Code (S5), or —
  fallback documented — the saved-PNG path.
- **A14 Depth guard.** A spawned child asked to use *its* ptc kernel to spawn a grandchild gets
  the depth RuntimeError (its env: `PTC_DEPTH=1`).
- **A15 Codex worker.** `await agent.run("print exactly DONE", provider="codex")` →
  `AgentResult.text` containing `DONE`, no human interaction.

## Milestones

Spikes come **before** the milestones whose architecture they decide, as an explicit gate.

- **M0 — spike gate + kernel spine.** Run S1, S3, S5 as committed probes and record verdicts
  in this spec (Surprises & Discoveries). Build the minimal explicit-session spine: venv
  provisioning + launcher, race-proof spawn/ownership, exec/wait/interrupt with terminal
  records and bounded text shaping, keyless integration suite covering the A1/A3/A12
  equivalents. Exit: keyless tier green; S1/S3/S5 verdicts written (each promoting its design
  or switching to its named fallback).
- **M1 — surface.** Automatic discovery (hook tree-walk, run-file, keying chain, meta.json);
  `restart`/`kernels`; CLI parity; read/write/edit/bash + audit + mutation footer; images per
  the S5 verdict; skill v0 + plugin packaging. Exit: A1–A3, A6, A10, A12 (live phrasing).
- **M2 — agents + llm.** Spikes S2, S4 first, then: SDK backend
  (run/spawn/fork/gather/send/resume/registry, child env rules, semaphore/timeout hardening),
  codex backend, depth brake, `llm()`. Exit: A4, A5, A8, A14, A15.
- **M3 — web + history + polish.** S6 first, then web_fetch/web_search, history(), workflow
  helpers, skill final wording, README + trust model, full acceptance run incl. A7, A9, A11,
  A13.

**M3 closed 2026-08-22 (T28), and with it M0–M2.** The acceptance run recorded in Surprises &
Discoveries executed A1–A15 as written — 15 pass, A13 in its documented S5 form — over a
keyless tier of 209 passed / 0 skipped and a live tier of 8 passed / 0 skipped. Nothing is
carried forward as failing.

**Open residuals at close (the full ledger).** Ten behaviors ship recorded-but-unclosed —
each proven only against a fake, or not proven at all, and each written up in its own section
above. None is an acceptance failure:

1. **Images — the saved-path line.** The PNG is written to `cells/<id>-<k>.png`, but the shaped
   text still does not name it. Designed, never implemented; T28 re-confirmed it parked.
2. **`AgentHandle.interrupt()` — the no-session `result()` contract.** Still fake-only: the live
   T28 run took the session-open branch (now live-verified), so the branch that raises
   `RuntimeError` because nothing was draining has never run against a real SDK client.
3. **Codex server→client replies.** No server→client reply has ever been field-proven; the eight
   per-method replies are shape-tested against the strict fake only, and the one request kind
   S4 showed that the `never` + `read-only` policy does *not* short-circuit — an MCP tool-call
   approval — remains unexercised in production.
4. **The web prose-scrape fallback.** Every live WebSearch response seen so far (S6 and A7)
   carried a well-formed `Links:` payload, so the no-payload branch is pinned only by a
   synthetic fixture.
5. **The fork tight-race.** S2's flush-per-message model was read off record timestamps with a
   ~7.5 s gap; a fork issued milliseconds after the preceding message completes is untested.
6. **The TTL watchdog race window.** Explicitly labelled a known residual where it is recorded:
   a sub-millisecond window between an `execute_request` landing and `pre_run_cell` stamping the
   cell can theoretically still race the watchdog (worst case is an honest kernel-died abort,
   never silence).
7. **`history()` on a codex handle.** A codex handle's `session_id` is a thread id whose turns
   are written as a codex rollout under `$CODEX_HOME/sessions`, not as Claude JSONL; v1 reads
   the Claude store only and raises `NotImplementedError` rather than glob-matching an
   unrelated transcript (final review r3, finding 8). Rollout parsing is the honest v1
   boundary, deliberately not attempted; `h.messages()` carries the turns the handle saw.

8. **A codex child can read the whole disk.** PTC pins its codex threads to
   `sandbox: "read-only"`, which in codex constrains WRITES only —
   `SandboxPolicy::has_full_disk_read_access` is `true` for every policy, `ReadOnly`
   included (`codex-rs/protocol/src/protocol.rs:1152-1153`) — while `_child_env` forwards
   the real `HOME`/`CODEX_HOME` because that is where subscription auth lives. A codex task,
   or a prompt injection reaching one, can therefore read `~/.claude` or anything else this
   user can and carry it out through the model request, across the vendor boundary the
   child-env allowlist exists to hold. Adjudicated as the kernel's own trust class rather
   than a defect (final review r4, finding 1): see Trust model. The candidate hardening is a
   read-restricted permission profile or a workspace-scoped sandbox for codex children —
   `app-server/README.md` already prefers permission-profile selection over the `sandbox`
   shorthand, so the seam exists; it is future work, unbuilt and unmeasured.

9. **CLOSED (2026-08-24).** A real marketplace install has now been run end to end: the
   package was published as the standalone single-plugin marketplace
   `github.com/SSFSKIM/ptc-tool` (subtree split, 131 commits, tree-identical to the
   monorepo copy), registered (`claude plugin marketplace add SSFSKIM/ptc-tool`), installed
   (`ptc@ptc-tool`, cache `~/.claude/plugins/cache/ptc-tool/ptc/0.1.0`), and a fresh
   headless session ran `exec` through the installed plugin — venv provisioned from the
   cache path, kernel spawned, `print(6*7)` → `[cell 2 · ok]` `42`. The CLI (`ptc doctor`)
   and a raw MCP `initialize` handshake against the cached `bin/ptc-launch` also pass from
   the cache path.

10. **A host-side kill cannot reach codex's detached tool children.** codex detaches shell-tool
    processes into their own sessions (`codex-rs/core/src/spawn.rs`) and its parent-death
    SIGTERM is Linux-only, so on macOS `ptc kill`/`ptc restart` from another process cannot
    enumerate or signal those grandchildren. r13 added the kernel-side half — TTL/watchdog
    exits release agent backends (interrupt + close, bounded grace) before `os._exit`, a
    path that exists because ipykernel keeps its loop serving ZMQ between cells (verified
    live) — and even there, a subtree a tool spawned inside its own further session is
    reachable only by codex's own group-kill branches. Recorded in source at the
    `kill_process_tree` call site; never faked as enforcement.

Distinct from these, and not counted as residuals: the declared **Non-goals** (Codex-side
installation testing, Windows, dill/state snapshots, Workflow-tool replication) and the
**accepted limitations** logged with their own decisions — chiefly the `comm`-substring
discovery gap for a wrapper-launched `claude`, deferred until a real wrapper case is observed.

## Decision Log

- Decision: Build PTC as an MCP server + CLI + skill *on top of* Claude Code, not a replacement
  harness.
  Rationale: The project goal is Claude Code gaining Prime-style PTC; the conversation's own
  verdict is that hybrid (native lane + programmatic lane) beats PTC-only. Claude Code keeps
  mutation approval, vision, and its ecosystem; the kernel adds the programmatic lane.
  Date/Author: 2026-08-20 / design session (user + Claude).

- Decision: Claude children are spawned by the **Python Agent SDK in-kernel**; Codex children by
  a stdio `codex app-server` client. Rejected: routing Claude children through CC-to-SDK's
  `ccx serve` app-server (WS-only, extra server to run, four hops), and shipping both backends in
  v1. An `AgentBackend` seam keeps fleet-attach addable later.
  Rationale: fewest processes, subscription auth for free, children are full Claude Code agents
  (including this plugin → controlled recursion).
  Date/Author: 2026-08-20 / grill round 1.

- Decision: **Detached kernel per session, surviving client restarts**; no broker daemon.
  Rejected: adapter-owned session-scoped kernel (dies with Claude Code, breaks `--resume`);
  a Conversation.md-style broker daemon (a fourth process whose jobs — multi-client access,
  survival, discovery — the detached kernel + connection files already cover).
  Rationale: the kernel is the state; keep exactly one long-lived process.
  Date/Author: 2026-08-20 / grill round 1.

- Decision: **Full mutation tools + audit trail** in the kernel. Rejected: kernel-side guard
  rails (path confinement + elicitation prompts — friction, and trivially bypassed by raw
  Python); read-only default lane (kills half the value; the conversation's strict-mutation-lane
  idea remains available by simply using native tools for mutations).
  Rationale: the honest boundary is the `mcp__ptc__exec` allow decision; make visibility
  (footer + audit.jsonl) excellent instead of pretending to enforce.
  Date/Author: 2026-08-20 / grill round 1.

- Decision: Extras = `llm()` and `history()`; **no in-kernel glob/grep**.
  Rationale: user call — the model already has native rg-backed search; `llm()` is the RLM core
  primitive; `history()` is the PRO-LONG lever with the strongest evidence.
  Date/Author: 2026-08-20 / grill round 1 (user).

- Decision: **Fan-in is awaited in code** (`run`/`spawn+gather` + exec/wait yield). Rejected:
  Prime's admission-only contract.
  Rationale: Prime's contract depends on its host injecting agent messages into the parent's
  next turn; Claude Code offers no such channel to an MCP server. Blocking the *cell* while
  yielding the *model* reproduces the useful half (parent context stays clean, model stays free)
  without fighting the harness.
  Date/Author: 2026-08-20 / grill round 2.

- Decision: Children default to **`permission_mode="bypassPermissions"`**, overridable per call.
  Rejected: `acceptEdits` default (children stall headlessly on shell work); elicitation-routed
  approvals (blocks cells on human input; candidate for a later option).
  Rationale: consistent with the full-tools stance; the depth guard bounds the blast radius.
  Date/Author: 2026-08-20 / grill round 2.

- Decision: `llm()` and `web_search()` are **SDK-one-shot only (subscription-billed)**; the
  `anthropic` API client is not a dependency; `web_fetch` is native httpx. Rejected: Anthropic
  API `web_search` server tool and API-first `llm` (metered billing); third-party search APIs
  (Serper/Brave — an extra key, Prime's choice).
  Rationale: user call — never bill the API for kernel-originated model calls; one backend, one
  auth story. `web_search` stays structured by parsing the WebSearch tool_result from the
  stream instead of trusting prose.
  Date/Author: 2026-08-20 / user revision after design presentation.

- Decision: **No search guidance in the skill** (no `bash("rg …")` examples).
  Rationale: user call — the model already routes search well natively; teaching it here adds
  noise.
  Date/Author: 2026-08-20 / user revision after design presentation.

- Decision: Packaging as `ptc-surface/ptc/` — package/server/skill all named `ptc`, Claude Code
  plugin dir in-repo, Codex registration documented-not-tested.
  Rationale: approved as proposed; short tool names (`mcp__ptc__exec`) matter for prompt economy.
  Date/Author: 2026-08-20 / grill round 2.

- Decision: `exec`/`wait` **yield protocol** copied from Codex code-mode (cell_id + timeout_s),
  with `wait` implemented over kernel-side tee files so it survives adapter restarts; `exec` on a
  busy kernel returns `status: busy` rather than queueing silently.
  Rationale: proven in-repo pattern; kernel-side capture makes MCP and CLI equivalent clients;
  explicit busy beats invisible queuing for a model deciding what to do next.
  Date/Author: 2026-08-20 / design composition.

- Decision: Session discovery via **SessionStart-hook run-file keyed by shared PPID**, with
  explicit-arg > run-file > `PTC_SESSION` > `CLAUDE_CODE_SESSION_ID` > adapter-local fallback,
  and `${CLAUDE_SESSION_ID}` in the skill as the model-visible escape hatch.
  Rationale: no Claude Code API exposes "the session id of the conversation calling this MCP
  tool"; the PPID bridge is the least-magic mechanism available, and the fallback chain keeps
  every layer functional if it breaks (spike S3).
  Date/Author: 2026-08-20 / design composition (flagged review-carefully; approved).

- Decision: **Controlled track** (spec → writing-plans → subagent execution), milestones M0–M3.
  Rejected: autonomous execplan; direct implementation.
  Rationale: mid-size build with taste-heavy surfaces (skill wording, API ergonomics) and six
  spikes worth human-visible checkpoints.
  Date/Author: 2026-08-20 / grill round 3 (user).

- Decision: Adversarial-review revisions (Codex gpt-5.6-sol, xhigh): kernel-side per-cell
  **terminal records** + caller-held `wait` cursors; per-key **flocks** for spawn ownership
  (pid + start-time identity) and atomic busy-check-plus-submit; hook discovery by
  **ancestor tree-walk** (hooks launch through a shell, so raw PPID is a transient `sh`);
  `meta.json` separating kernel key from Claude session id (fork/history fail explicitly on
  alias-keyed kernels); children get explicit `mcp_servers` + a **fresh `PTC_SESSION`** (never
  inherited — inheriting would attach the child's adapter to the parent's kernel); depth guard
  restated as a cooperative brake; **no `structuredContent`** in v1 (Claude Code may prioritize
  it over formatted content); server-side output caps (50k clamp + ~4 MB aggregate); idle TTL
  raised to 24 h with an expiry notice and TTL-qualified promises; `.mcp.json` →
  checked-in **launcher** that provisions the venv before exec'ing the adapter; keyless
  real-kernel suite made the non-skippable milestone gate; spikes pulled into an M0 gate;
  Windows explicitly out of scope.
  Rejected from the same review: a per-session coordinator process and supervisor-enforced
  depth/concurrency quotas (over-machinery — the honest boundary is the `mcp__ptc__exec` allow
  decision, and per-key flocks already serialize local clients); fail-closed session discovery
  (an explicit degraded-keying notice on every result preserves usability without silent
  wrong-namespace attaches); the claim that `CLAUDE_CODE_SESSION_ID` is gated to internal
  (`USER_TYPE=ant`) users — live-disproven in this project's own external-account session.
  Date/Author: 2026-08-20 / independent spec review.

- Decision: Plan-review revisions (Codex gpt-5.6-sol, xhigh, over the execution plan):
  cell numbering derives from IPython's pre-incremented counter (`count-1` when history is
  stored) with an end-to-end alignment test as the guard; the submit lock is held until the
  kernel publishes the cell (fail-closed with a pending marker — no 3 s escape hatch);
  cell ids are **monotonic across kernel epochs** and `wait` settles archived cells from
  `cells-prev-*/`; the spawn transaction covers bootstrap and kills the kernel on any
  failure (and a live-but-never-ready owner is reaped before respawn); the watchdog calls
  `os._exit` while still holding the flock; agent handles retain their driver task, take
  the shared semaphore and deadline on `send`, and tear down the SDK client on
  interrupt/error; spike S1 exercises real `ClaudeSDKClient` lifecycle (two clients,
  follow-up sends, interrupt, leaked-process check); the codex client implements the full
  initialize/initialized handshake, camelCase sandbox enums, and turn-id-carrying
  interrupts against a fake that rejects invalid shapes; `ptc.runtime` exports its API at
  module level; and acceptance A11 asserts an actual `mcp__ptc__exec` tool_use event on a
  prompt that never mentions ptc.
  Rationale: all ten review findings verified against IPython/app-server sources and
  accepted; none were rebutted.
  Date/Author: 2026-08-20 / independent plan review.

- Decision: The shipped install mode is the PLUGIN (`--plugin-dir` / marketplace): long tool names `mcp__plugin_ptc_ptc__*`, SessionStart hook included (the hook IS the primary discovery channel). Direct MCP registration (`claude mcp add ptc`) is documented as secondary: short `mcp__ptc__*` names but NO hook, so keying falls to `CLAUDE_CODE_SESSION_ID`/explicit `session=`.
  Rationale: T11's S3 spike proved the hook+run-file path is the only keying that cannot inherit a foreign session id; the naming cost of the plugin prefix is documentation-only.
  Date/Author: 2026-08-21 / controller adjudication of T11 concern 1.

- Decision: `ptc/src/ptc/discovery.py`'s process-tree walk (in `resolve()`) is a second,
  independent copy of `find_claude_ancestor()` in `hooks/session_start.py`, not a
  shared helper. A cross-reference comment sits in both files, pointing each file at the
  other and naming the "keep the two predicates in sync" obligation.
  Rationale: the hook runs under system Python, before `~/.ptc/venv` exists and outside the
  `ptc` package's own import path, and must stay stdlib-only/single-file per T11's contract —
  it cannot `import ptc`. `discovery.py` is package-side and free to depend on `.paths`.
  Sharing would require either the hook importing the package (breaks the stdlib-only,
  pre-venv contract) or the package vendoring hook code as a subprocess call (adds a process
  spawn to every resolve() for no benefit) — both worse than two short, independently tested
  copies kept honest by the same predicate (see next entry).
  Correction: the initial T13 cut only cross-referenced one-directionally
  (`discovery.py`'s docstring named the hook; the hook said nothing back) — the task-13
  review caught this as an Important finding against the "both ways" claim in the original
  report. Fixed in the T13 review-fix pass by adding the comment to
  `find_claude_ancestor()` in `session_start.py`; both files now name each other.
  Extension (r15): the two-copy structure — and the sync obligation — now covers the
  process IDENTITY the two sides exchange through the run file as well as the walk. The
  hook records the `claude` ancestor's birth stamp when it writes the file
  (`birth_identity`), and `resolve()` re-reads it (`ownership.hook_birth_identity`) and
  refuses a file whose stamp names a different incarnation of that pid. The two readings
  must come from the SAME source to be comparable, so both are `/proc/<pid>/stat` field 22
  on Linux and the pinned `ps -o lstart=` string elsewhere — libproc's finer stamp, which
  kernel ownership uses, is deliberately absent because no stdlib-only hook can produce it.
  A run file with no stamp keeps today's acceptance; the hook rewrites every file on every
  SessionStart, so that window ages out with no migration step.
  Date/Author: 2026-08-23 / r15 finding 3.

- Decision: Both walks match a candidate ancestor by **substring** — `"claude" in
  os.path.basename(comm)` — not exact equality. `discovery.resolve()`'s walk was written to
  match this predicate exactly.
  Rationale: the actually-shipped hook (`session_start.py:47`, from T11, verified unchanged
  since) has always used the substring form, and `test/spikes/s3_probe.py`'s in-kernel model
  of "T13's own walk" (`claude_ancestor_of`, written during T11) already assumes substring
  too — so substring is the pre-existing, already-load-bearing contract, not a new choice.
  T13's task brief carried a premise that the hook used exact `==` matching and asked T13 to
  widen discovery to match it; that premise did not hold against the code (confirmed via
  `git log -p` on `session_start.py`, which shows only the substring form since T11's first
  commit) — aligning discovery to exact match would have made the two walks resolve
  *different* ancestors on the same tree, exactly the failure item 4 sought to prevent. T13's
  live S3 re-run (see task-13-report.md) confirms the substring walk resolves the real
  session end-to-end. Named limitation carried forward from T11: a wrapper-launched `claude`
  whose `comm` is something unrelated (e.g. a `node` shim) is invisible to either substring or
  exact matching on `comm` alone — closing that gap needs `ps -o args=` (full argv), which
  risks false positives from any argv mentioning "claude" and is deferred until a real wrapper
  case is observed. The converse risk is also accepted, not just the false-negative one: an
  unrelated ancestor whose `comm` merely contains "claude" as a substring (e.g. a hypothetical
  `claude-monitor` binary) would false-positive-match. This is accepted because both walks are
  nearest-first (return on the first match walking from self outward), so a real `claude`
  ancestor always wins over a more-distant decoy, and the behavior is inherited unchanged from
  T11's already-shipped hook rather than newly introduced by T13.
  Date/Author: 2026-08-21 / T13 executor; false-positive risk noted 2026-08-21 / T13 review-fix.

- Decision: T20/T21's `agent` child-env plumbing (and any other PTC surface that dumps or logs
  the kernel's environment) must redact credential-bearing variables before the value leaves
  the process — match by name (`KEY|TOKEN|BEARER|SECRET`) and by value prefix (`sk-ant-`).
  Rejected: passing the inherited environment through unfiltered on the assumption that only
  `ANTHROPIC_API_KEY` is sensitive.
  Rationale: S2's live run recorded `CLAUDE_ROUTINE_BEARER`, an `sk-ant-oat…` OAuth token,
  among the kernel's inherited `CLAUDE_*` variables; the PTC kernel inherits the adapter's
  environment verbatim, so any env-dumping surface (debug output, `audit.jsonl`, child-env
  plumbing) is a credential leak unless it redacts. The S2 probe itself implements this
  pattern (`_secretish(name, value)`) and its artifact writer redacts accordingly.
  Date/Author: 2026-08-21 / T18 review.
  Amendment (T20): the rule binds *surfaces that log or forward environment*, and T20 shipped
  none. `_child_env` builds four `PTC_*` variables from scratch and forwards nothing else; the
  SDK's own merge over the inherited environment is what pays for subscription auth and must
  NOT be filtered (redacting it breaks the OAuth bearer the child needs). No audit record,
  registry row, cell log or CLI output enumerates the environment — `ptc doctor` prints two
  named non-credential variables and nothing else — so there is no redaction *call site* yet,
  and a helper used by nothing would be dead code. `test_child_env_is_ptc_only_and_carries_no_
  secrets` states the predicate as an assertion on the actual seam and fails the moment someone
  widens `_child_env` to an `os.environ` merge. The redaction helper lands with the first
  surface that logs or forwards inherited env (a debug dump, an env-carrying audit record).
  Date/Author: 2026-08-21 / T20 review-fix.

- Decision: **OPEN** — whether/how to isolate a PTC-spawned `codex app-server` thread from the
  user's `~/.codex` configuration (MCP servers, hooks, model, auth) is deferred to T22, not
  decided here. *(Settled by the T22 entry below: the spawn-time seam works, the
  `thread/start` overlay does not, and isolation ships default-on.)*
  Rationale: S4's live capture shows a PTC-spawned thread silently inherits the user's whole
  Codex surface — four of the user's MCP servers started by name (`node_repl`, `context7`,
  `discord`, `openaiDeveloperDocs`), with `discord` failing its handshake twice *inside the
  PTC-spawned thread*; a plugin `session-start` hook and a `stop` hook fired from
  `~/.codex/plugins/cache/claude-plugins-official/explanatory-output-style/…/hooks.json` and
  `~/.codex/hooks.json`; the user's configured model `gpt-5.6-sol` at
  `reasoningEffort: "xhigh"`; and `instructionSources` pulling both `~/.codex/AGENTS.md` and the
  repo's `AGENTS.md`. The cost is measurable and real: `thread/tokenUsage/updated` reports
  25,647 total input tokens for an 8-token answer. Two candidate mechanisms exist and **neither
  was tested**: `thread/start`'s free-form `config` overlay object (`additionalProperties:
  true`), and `-c key=value` at `codex app-server` spawn. The discord-failure observation is
  concrete evidence that a user's broken MCP server becomes PTC's problem absent isolation.
  Date/Author: 2026-08-21 / T19 review.

- Decision: PTC isolates its Codex children at **spawn time**, with
  `codex app-server --disable hooks --disable plugins`, **on by default**;
  `PTC_CODEX_INHERIT=1` restores the user's full Codex surface. Of the two candidate
  mechanisms the T19 review left untested, exactly one works.
  Rationale: measured on codex-cli 0.146.0, no model tokens spent (`thread/start` starts a
  thread's MCP servers without calling a model, so the whole comparison is free). The two
  feature flags took a PTC thread from six inherited MCP servers to three — every
  plugin-provided one (`context7`, `discord`, `dataAnalyticsWidgets`) gone, including the
  `discord` server that fails its handshake inside our thread — and no hook fired at all
  (`hook/started` count 0 on the live turn, against S4's session-start + stop pair). Token
  cost fell from S4's 25,647 input tokens to **19,113** for the same 8-token answer.
  The three seams that do **not** work, each a trap worth naming: (a) `thread/start`'s
  free-form `config` overlay is **inert** — `{"mcp_servers": {}, "features": {...}}` changed
  nothing, so the object being `additionalProperties: true` in the schema says nothing about
  it being honoured; (b) `-c mcp_servers={…}` **merges** rather than replaces, so an empty
  table is a no-op and a non-empty one only adds a server; (c) `-c
  mcp_servers.<name>.enabled=false` makes `codex app-server` **exit silently before writing a
  single line** — from a client's side indistinguishable from a hung server, which is why the
  live suite keeps a free regression test that the flags PTC ships are still accepted.
  What stays inherited, and is accepted: the user's own `[mcp_servers]` entries, their model
  and reasoning effort, and `instructionSources` (both AGENTS.md files). No `-c` seam clears
  the first, and the only mechanism that would — redirecting `CODEX_HOME` — also moves
  `auth.json` and would break subscription auth. This is the deliberate residue, not an
  oversight; `AgentOpts.model` overrides the model per call.
  Date/Author: 2026-08-21 / T22.

- Decision: `AgentOpts` is one option set for both providers, so `provider="codex"` either
  **honours** a field or **refuses** it — never accepts and drops it. `system` maps to
  `thread/start`/`thread/resume`'s `developerInstructions`; `allowed_tools`, `max_turns`
  and a non-default `permission_mode` raise `TypeError` naming the option.
  Rationale: T22 correctly refused to put `effort` on `thread/start` because 0.146.0 ignores
  unknown params, making a misplaced field an invisible no-op — and then reproduced exactly
  that failure at PTC's own API boundary, where four fields the spec advertises for both
  providers were consumed by `agents._opts()` and dropped by the backend. `system` is
  directly mappable, and the two candidate fields are not interchangeable: `base_instructions`
  is an override of codex's own core prompt ("Base instructions override",
  `codex-rs/core/src/config/mod.rs:677`) and would take the apply-patch and tool-use
  instructions with it, while `developer_instructions` is "injected as a separate message"
  (`:680`) — the second is what a caller-supplied system prompt means. Both
  `ThreadStartParams` and `ThreadResumeParams` declare it as `["string","null"]` on 0.146.0.
  The other three have no codex analogue: the app-server exposes no per-turn tool allowlist
  and no turn cap, and the thread is pinned to `approvalPolicy: "never"` + `sandbox:
  "read-only"` regardless of `permission_mode` — a deliberate binding, now stated rather than
  silently applied. `permission_mode` carries a dataclass default every caller passes whether
  they meant it or not, so only an explicit non-default value raises.
  Date/Author: 2026-08-21 / T22 review-fix.

- Decision: the `codex app-server` child's environment is **built from an allowlist**, not
  inherited — the one place the codex backend deliberately diverges from `claude_backend`.
  Rationale: the kernel inherits the MCP adapter's environment verbatim, credential-bearing
  `CLAUDE_*` variables included (an `sk-ant-oat…` OAuth bearer among them, per the Trust
  model). For a *claude* child that inheritance is what pays for subscription auth and must
  stay; for another vendor's binary it is a cross-vendor credential leak that also reaches
  every user MCP server that binary starts inside PTC's thread (`--disable plugins` removes
  plugin-provided servers; the user's own `[mcp_servers]` entries are accepted residue).
  Verbatim inheritance had a second cost: `PTC_SESSION` and `PTC_DEPTH` passed through
  unrewritten, so a codex child reaching PTC's own MCP server (`codex mcp add ptc` is a
  documented install path) would attach to the PARENT's kernel at the parent's depth and
  defeat the depth brake — latent, since the live capture shows no `ptc` entry among the
  inherited servers, but it is the exact scenario `claude_backend` guards. The forwarded set
  is what codex needs and nothing more: `PATH`, `HOME`/`CODEX_HOME` (subscription auth lives
  in `~/.codex/auth.json`), the `USER`/`SHELL`/`TMPDIR`/`TERM`/locale basics, proxy and CA
  variables for egress, plus PTC's four own variables rewritten through the same helper both
  backends now share (`agents.child_ptc_env`). `OPENAI_API_KEY` is deliberately **not**
  forwarded: PTC's codex path is subscription auth, which an API key would shadow. Accepted
  consequence: a user MCP server that expects some other inherited variable will not see it
  inside a PTC codex child; widening the allowlist is the remedy, not re-inheriting.
  `PTC_CODEX_INHERIT=1` restores the user's Codex *surface* (hooks, plugins) and does not
  restore credential inheritance — the two are separate concerns.
  Date/Author: 2026-08-21 / T22 review-fix.

- Decision: `CodexProc.close()` settles every pending request and the in-flight turn before
  it cancels the stdout reader.
  Rationale: EOF from the child is what normally drives `_fail()`, and `close()` is the one
  path that suppresses that EOF — it cancels the reader while the child is still alive. A
  `close()` landing mid-turn therefore left `turn()` waiting on an event nothing would ever
  set, for the full 1800 s `_TURN_S` budget, holding its `max_concurrency` permit the whole
  time; eight of those wedge the agent namespace. `AgentHandle.close()` is public and
  documented, so "call close() while result() is outstanding" is a caller's prerogative, not
  a misuse. Two keyless regression tests pin it — one on the backend, one through
  `_Agent` under `max_concurrency=1`, which only passes if the permit really came back.
  Date/Author: 2026-08-21 / T22 review-fix.

- Decision: PTC's **model-facing size budgets stand as specified** — `max_output_chars`
  default 12 000 with the 50 000 server clamp, and `skills/ptc/SKILL.md` at ~12 KB.
  Rejected: applying the codex-rs harness's ">1K tokens of model context is a P0" review
  ceiling (`AGENTS.md:104-106`) to either of them.
  Rationale: that ceiling governs the RUST harness's own additions to a Codex turn's context;
  PTC is a Claude Code MCP server plus a skill, and its two budgets are already measured
  against the norms that do govern it. (a) A cell result is a TOOL RESULT, and Claude Code's
  own native tools return more: Bash up to ~30 000 characters, Read up to 2 000 lines. The
  host's MCP output cap is 25 000 tokens (Surprises & Discoveries above), which 12 000
  characters sits far under; the value is user-configurable per call and by
  `PTC_MAX_OUTPUT_CHARS`, and the 50 000 clamp is the ceiling on what a caller may ask for,
  not a default anyone gets. A12 acceptance measured a live truncated result at 12 137
  characters against a 100 001-character log. (b) A skill is progressive disclosure:
  `SKILL.md` is loaded when the model reaches for the skill, not resident in every turn, and
  its ~12 KB measures ~2.2k tokens — ordinary for a Claude Code skill. T27's review already
  adjudicated this exact length once ("update the target, not cut content"), and cutting
  operational instructions into the README would move them out of the only document the
  model reads at the moment it needs them. Recorded so the question does not reopen for a
  fourth reviewer: the honest lever if these budgets ever prove too large is measurement of
  real cells and real skill loads, not a threshold borrowed from another product in this
  repository. The same review's "split the feature into reviewable stages" finding is the
  round-1 adjudication recurring unchanged (this work WAS staged as 28 independently
  reviewed tasks; the whole-branch diff is the plan, not one review unit) and stands.
  Round 8 raised all three again — that reviewer is not steerable and re-derives this
  repository's own ceiling from `AGENTS.md` on every pass, so recurrence is the behaviour
  of the instrument rather than new evidence — and added a third member of the same family:
  `web_fetch(..., prompt=…)` putting fetched content into an `llm()` call. That one is the
  deliberate analog of Claude Code's native WebFetch, which is a model-summarized fetch by
  definition; its existing 200 000-character content cap is ~50k tokens, comfortably inside
  the sub-model's own context, and paying for the page is what asking for a summary of it
  means — SKILL.md carries that cost at the call site, and `.text` is always there for
  callers who would rather filter in Python. All three families stand.
  Date/Author: 2026-08-22 / controller adjudication, final review r6; extended r8.

- Decision: foreground `bash()` output is BOUNDED (same 4 MB head/tail + elision notice as
  background handles), overturning the earlier contract that a foreground command returns
  its whole output. A pre-existing test pinned the old contract and was deliberately
  reversed, not extended.
  Rationale: r12 finding 3 — the unbounded lists let `yes` OOM-kill the persistent kernel
  well inside its own 120 s timeout; a time limit does not bound bytes. The full stream was
  never recoverable anyway once it out-sized memory, and background results already taught
  callers the head+notice+tail shape. Cost accepted: a caller that relied on a large
  foreground result arriving whole now sees an elision notice; the full text was never
  written to disk on the foreground path, so the notice is honest about the loss.
  Date/Author: 2026-08-23 / r12 fix wave, controller-adopted.

- Decision: a kernel's runtime build identity is the hash of the venv's own `.ptc-version`
  stamp FILE, not of the launcher's live `stamp_payload()`. On attach, a mismatch between
  the identity recorded at spawn and the stamp now on disk recycles the kernel with an
  expiry-style notice; no stamp on either side attaches as before (dev runs).
  Rationale: r13 finding 4 asked for a live-payload comparison, but `stamp_payload()`
  changes the moment package SOURCES change, rebuilt or not — that would destroy a live
  namespace on every source edit. `.ptc-version` changes exactly when the venv under a
  running kernel was actually replaced, which is the stranding the finding describes.
  Fixer deviation, controller-adopted.
  Date/Author: 2026-08-23 / r13 fix wave.

- Decision: registry rows whose kernel died mid-turn reconcile at bootstrap to a NEW status
  `orphaned` (vocabulary was running/done/error/interrupted), session id preserved for
  resume.
  Rationale: nobody interrupted those turns — the kernel died under them; a reader of the
  row is owed the distinction. Fixer choice, controller-adopted.
  Date/Author: 2026-08-23 / r13 fix wave.

- Decision: on restart, meta.json's kernel-lifetime fields (idle_hours, max_concurrency,
  depth, max_depth) override even an EXPLICITLY passed `config=`; the explicit config
  supplies everything else and any lifetime field meta does not record. Rule: what the
  kernel LIVES under travels with the kernel; what the caller RENDERS with stays the
  caller's.
  Rationale: r16 finding 3. "Explicit config wins wholesale" was never true — r8/r15
  already overlaid depth onto explicit configs — and making it true would reopen the
  recursion brake through `restart_kernel(key, config=Config.from_env())`, the exact
  construction r15 closed in env form. Fixer deviation, controller-adopted.
  Date/Author: 2026-08-23 / r16 fix wave.

- Decision: PTC ships from a standalone public repo (`SSFSKIM/ptc-tool`) that is itself the
  single-plugin marketplace, not from the codex_somersault monorepo. The repo was produced
  by `git subtree split -P ptc-surface/ptc` (history preserved, tree verified identical),
  then gained `.claude-plugin/marketplace.json`, an MIT LICENSE, and marketplace-first
  install docs. Rationale: a marketplace install clones the whole repo, and the monorepo is
  a multi-GB codex fork; every existing plugin of this account already follows the
  one-repo-per-plugin pattern. The monorepo copy remains the dev workspace for now;
  divergence is visible because the split branch (`ptc-tool-export`) makes the tree hashes
  comparable.
  Date/Author: 2026-08-24 / Claude (approved by owner)

## Surprises & Discoveries

- Observation: Prime Agent's model surface is exactly one tool (`ipython`) with **no cell
  timeout** and 64 KiB/stream truncation; `rlm()` is admission-only, and results return solely
  via host-injected agent messages — the one mechanism Claude Code cannot host.
  Evidence: `prime-agent/packages/coding-agent/src/core/tools/ipython.ts` (schema, no timeout),
  `src/core/kernel/index.ts` (`DEFAULT_MAX_OUTPUT_CHARS = 65536`), `docs/rlm.md`.

- Observation: Claude Code's MCP defaults are unusually PTC-friendly: tool timeout ≈ 27.8 h
  (`DEFAULT_MCP_TOOL_TIMEOUT_MS = 100_000_000`) and a 25 000-token output cap — long cells
  cannot be killed by the host, and our 12k-char cap sits far under the ceiling.
  Evidence: `Claude Code Src/src/services/mcp/client.ts:211`, `src/utils/mcpValidation.ts:16`.

- Observation: Claude Code's MCP client advertises `roots` and `elicitation` but **not
  `sampling`** — an MCP server cannot ask the host model for completions, which forecloses the
  cheap `llm()` implementation and forces the SDK-subprocess route.
  Evidence: `Claude Code Src/src/services/mcp/client.ts:994` (capabilities object).

- Observation: `${CLAUDE_SESSION_ID}` is substituted inside SKILL.md at load, and
  `CLAUDE_CODE_SESSION_ID` is exported into Bash-tool environments — two free session-identity
  channels that make the fallback chain robust without any host API.
  Evidence: `Claude Code Src/src/tools/SkillTool/SkillTool.ts:1070`, `src/utils/Shell.ts:325`.

- Observation: `ANTHROPIC_API_KEY` **shadows** OAuth subscription auth in the `claude` CLI —
  "subscription-only billing" is an environment discipline, not a code path we can force.
  Evidence: CC-to-SDK probe 28 (`accountInfo() → apiProvider:"firstParty"` only with the key
  absent); `CC-to-SDK/CLAUDE.md`.

- Observation: the exec/wait/cell_id yield protocol already exists twice in this repo family —
  Codex code-mode (JS isolate) and CC-to-SDK's app-server turn model — so the adapter surface is
  a port, not an invention. Public "IPython kernel MCP" servers exist too; the kernel layer is
  commodity, and this project's differentiated work is the in-kernel runtime library, the agent
  bridge, and the skill.
  Evidence: `codex-rs/code-mode-protocol/src/description.rs` (EXEC/WAIT templates); web survey
  (ipython-mcp, jupyter-kernel-mcp, ipybox).

- Observation: CC-to-SDK's `ccx serve` app-server is WebSocket-only (stdio/UDS spec-named but
  unbuilt), and real Codex is driven by `codex app-server` over stdio with the same JSON-RPC
  vocabulary — which is why the Codex backend costs ~200 lines while a v1 Claude backend over
  the same protocol would have cost a transport.
  Evidence: `CC-to-SDK/docs/parity/appserver.md`;
  `CC-to-SDK/codex-plugin-cc/plugins/codex/scripts/lib/app-server.mjs`.

- Observation: live check in this project's own session (external account, Claude Code
  2.1.236): `CLAUDE_CODE_SESSION_ID` **is** present in the Bash-tool environment and matches
  the session id, and the Bash process's parent is the `claude` process itself — contradicting
  the independent review's claim that the variable is internal-only, and directly supporting
  keying fallbacks 3–4.
  Evidence: `echo $CLAUDE_CODE_SESSION_ID` → this session's UUID; `ps -o ppid=` → `claude`.

- Observation: the independent review surfaced three host behaviors treated as design
  constraints, none previously in the spec: Claude Code hooks launch through a shell (raw
  `$PPID` in a hook is a transient `sh`, hence the tree-walk); `structuredContent` in an MCP
  reply can take precedence over the content array and be JSON-stringified (hence dropping it);
  and the Python SDK has a reported indefinite-hang mode when its CLI subprocess dies
  mid-stream (hence S1's kill-and-recover case and per-call deadlines).
  Evidence: adversarial-review run of commit 20b6da563f (job log under
  `~/.claude/doperpowers/codex-companion`).

- Observation: The plan's own F4 remediation (spawn-transaction kill-on-failure) shipped with a narrower try than intended — `write_owner`/`write_meta`/`ready` sat outside the handler, so a failure there orphaned a live, ownerless kernel that `_clean_stale` can never reap (no owner record to find).
  Evidence: T4 execution live-reproduced it — a `NameError` at the `write_owner` call site (missing import in the plan's code) left 4 real orphaned ipykernel processes. Fixed in commit 03774f38ce (uniform kill-and-clean under `BaseException`, regression test `test_spawn_failure_leaks_no_process` proven RED against the stashed fix).

- Observation: ipykernel never writes tracebacks to stdout/stderr — `ZMQInteractiveShell._showtraceback` publishes on the iopub `error` channel only, so the kernel-side tee alone leaves an error cell's log empty. The bootstrap now mirrors IPython's rendered traceback into the cell log by wrapping `_showtraceback` (the narrowest seam; preserves stdout-vs-traceback ordering and leaves the terminal record's `error` field unchanged).
  Evidence: T5 RED run — fully populated error record, empty `cells/N.log`; fixed in commit 48a4c2413f.

- Observation: "idle" for the watchdog must exclude in-flight cells — the spec's TTL as first implemented measured only time-since-last-cell-boundary, so a computation outrunning the TTL was os._exit'd mid-cell. Idleness is now (no cell in flight) AND (TTL exceeded). Accepted tradeoff: a permanently hung cell never idle-expires; the TTL is a bound on idle lifetime, not total lifetime. Known residual: a sub-millisecond window between a client's execute_request landing and `pre_run_cell` stamping the cell can theoretically still race the watchdog; if hit, the client sees an honest kernel-died abort, never silence.
  Evidence: T5 review finding I1 + fix commit c055c28ae0 (`test_watchdog_spares_running_cell` overlaps a 14.4s cell against a 3.6s TTL).

- Observation: `jupyter_client`'s BlockingKernelClient heartbeat channel is a liability for short-lived control connections — its teardown raced the interrupt call with `ZMQError: Too many open files` from the hb thread, and the heartbeat's accidental startup delay was the only reason the control-channel `interrupt_request` ever flushed before teardown (SIGINT quietly did the real interrupting). The client now connects hb-less and awaits `interrupt_reply` before teardown; a wall-clock test bound (settle < the 2s SIGINT grace) guards the control path against regressing to SIGINT-only.
  Evidence: T6 execution (commit 1fa23ae189) — hand-found; measured 0.13s control-channel settle vs 4.34s with the control send sabotaged. Fail-closed admission also gained the sent-but-unacknowledged marker (commit a49c46ae80): any failure after `execute_request` leaves `pending.json` under the submit lock, closing the crash-mid-submit and slow-joiner silent-queue routes.

- Observation: [S1 verdict — PROMOTE] claude-agent-sdk ran cleanly inside ipykernel's own
  asyncio loop — one-shot `query()`, two concurrent `ClaudeSDKClient` sessions with follow-up
  sends, mid-stream cancellation, `interrupt()`, and a SIGKILL of the CLI under a live stream,
  all with the kernel still executing cells afterwards. `nest_asyncio` was NOT needed and is
  not even imported (ipykernel 7.3.0 awaits a top-level-`await` cell on its own loop, so
  nothing ever nests `run_until_complete`); the spec's conditional at "nest_asyncio is applied
  only if spike S1 proves it necessary" therefore resolves to *not applied*. CLI-death raised
  `ProcessError` in ~1.5 s — the SDK's reported indefinite-hang mode did NOT reproduce on
  0.2.142 + bundled CLI 2.1.237 — and a fresh query in the same kernel afterwards succeeded.
  The pre-designed background-thread fallback and its cross-thread handle protocol are NOT
  built. Two caveats for T20: cancellation/interrupt UNWIND is slow and highly variable
  (0.6 s–5.7 s to await a cancelled task, 4.0 s–13.0 s to drain after `interrupt()`), so
  teardown needs its own budget separate from the call deadline; and an interrupted turn does
  not raise — it ends with a normal `ResultMessage` carrying
  `terminal_reason='aborted_streaming'` / `is_error=True` (a completed one carries
  `terminal_reason='completed'`).
  Evidence: `test/spikes/s1_sdk_in_kernel.py`, two live passes —
  `one_shot: ok / ONE_SHOT_OK ResultMessage`;
  `two_concurrent: ok / CONCURRENT_OK True True`;
  `cancel_midstream: ok / CANCEL_OK + STILL_ALIVE 2`;
  `client_lifecycle: ok / CLIENTS_OK (True, True) (True, True) + LEAKED []`;
  `client_interrupt: ok / INTERRUPT_OK`;
  `kill_cli_midstream: ok / KILL_RESULT RAISED ProcessError + STILL_ALIVE_2 4` (cell 1620 ms);
  `loop_identity: NEST_ASYNCIO_IMPORTED False / LOOP asyncio.unix_events._UnixSelectorEventLoop
  ... patched False`; `cancel_reaps_child: CANCEL_CHILDREN before/during/after 0 1 0 /
  CANCEL_ORPHANS []`; `recover_after_cli_death: subtype 'success' after the kill`;
  `interrupt_terminal_reason: subtype 'error_during_execution', terminal_reason
  'aborted_streaming'`.
  T20 follow-up (2026-08-21, review-fix): `AgentHandle.interrupt()` implements exactly this
  shape — signal, then wait the drain out and let the driver's normal completion settle the
  handle, cancelling only past the drain budget. Proven against the fake (whose `interrupt()`
  now produces the aborted result after a delay, as the CLI does) and RED-proven against the
  pre-empting order. **Live-verified in T28** (2026-08-22, acceptance run), closing the
  deferral: a real `agent.spawn` on haiku was interrupted 10 s into a 500-line counting turn,
  and the session-open contract held exactly as designed — `PRE_STATUS: running`,
  `POST_STATUS: interrupted drain_s: 0.6`, `RESULT_OK: len=1435 head='1\n2\n3\n…'`,
  `REGISTRY: [('slow','interrupted')]`. So the drain ended normally and the driver's own
  completion delivered the aborted turn's PARTIAL text through `result()` (the literal
  `"(aborted)"` string is the fake's stand-in for that text, not a live wire value), and the
  status/registry flip to `interrupted` landed after the drain rather than pre-empting it.
  The no-session contract — `result()` raising `RuntimeError` because nothing was draining —
  was not the path this run took and remains fake-only. One live datum on drain length: 0.6 s,
  below the 4.0–13.0 s band S1 measured, so `_INTERRUPT_DRAIN_S` is generous, not tight.

- Observation: `ClaudeAgentOptions(setting_sources=None)` — the default — means "load
  everything the CLI would": the user's `~/.claude/settings.json`, project settings, their
  hooks, and CLAUDE.md. An in-kernel agent therefore inherits the host user's whole Claude Code
  configuration unless it opts out, and the user's own hooks execute inside the child. Measured
  on one identical trivial prompt: inherited $0.252102 / 4 HookEventMessages / 12 514
  cache-write tokens versus `setting_sources=[]` $0.053303 / 0 hook events / 5 217 cache-write
  tokens — ~4.7x the cost plus foreign hook execution. PTC's SDK backend should pass
  `setting_sources=[]` and configure children explicitly. Even isolated, a trivial call still
  costs ~5 200 prompt tokens of CLI base system prompt, so `llm()` is not a cheap primitive.
  Evidence: `test/spikes/s1_sdk_in_kernel.py::ADDENDUM["settings_isolation"]` —
  `SETTINGS_INHERITED cost_usd 0.252102 hook_msgs 4 in 2 cache_read 1452 cache_write 12514` /
  `SETTINGS_ISOLATED cost_usd 0.053302999999999996 hook_msgs 0 in 2 cache_read 0 cache_write 5217`.

- Observation: a plugin-provided MCP server's tools are named
  `mcp__plugin_<plugin>_<server>__<tool>`, not `mcp__<server>__<tool>` — ptc's exec tool is
  `mcp__plugin_ptc_ptc__exec`. Plugin servers are registered under the scoped name
  `plugin:<plugin>:<server>` and every non-`[a-zA-Z0-9_-]` character is normalized to `_`.
  Only a directly registered server (`claude mcp add ptc -- <plugin>/bin/ptc-launch`) gets
  the short `mcp__ptc__*` form, so the README's `permissions.allow` snippet and any skill
  text naming tools must carry both.
  Evidence: live S3 runs on 2.1.238 — reproduced by the committed instrument (scenario-1 re-run captured `TOOL_USE_NAMES: ["ToolSearch", "mcp__plugin_ptc_ptc__exec", "mcp__plugin_ptc_ptc__kernels"]`; plugin MCP tools arrive DEFERRED behind ToolSearch, so the scoped long name is the string skills/README must print — it is what the model searches against);
  `Claude Code Src/src/utils/plugins/mcpPluginIntegration.ts:350`;
  `src/services/mcp/normalization.ts`.

- Observation: `CLAUDE_CODE_SESSION_ID` is present in the **MCP adapter's** environment on
  2.1.238 (not only in Bash-tool environments), and claude overwrites any inherited value
  with its own session id — verified by launching a nested session with an outer session's
  id in the environment and reading what the adapter saw. Keying fallback 4 is therefore
  stronger than assumed and cannot be poisoned by an enclosing session. It stays *below*
  the run-file: an adapter started with no `claude` ancestor at all (the documented Codex
  registration path, `codex mcp add ptc -- .../ptc-launch`) does inherit whatever
  `CLAUDE_CODE_SESSION_ID` its launcher had, which keys the kernel to a foreign session.
  Evidence: spike S3 scenarios 1–4 (`env.CLAUDE_CODE_SESSION_ID` equals the nested
  session's id in every case); a bare `ptc-launch` driven over stdio from a Bash tool came
  up keyed to the enclosing session.

- Observation: a kernel outlives the adapter that spawned it and is re-parented to `launchd`,
  so a kernel's own ancestry is not a session-discovery channel — only the adapter's is.
  Anything in-kernel that needs the session identity must read it from the environment or
  `meta.json` written at spawn, never from `ps`.
  Evidence: spike S3 scenario 2 (`adapter_pid: 1, adapter_comm: "/sbin/launchd"` on a
  kernel adopted from the previous session).

- Observation: Claude Code's MCP **startup** timeout is 30 s (`MCP_TIMEOUT`, default
  30000) — unlike the ~27.8 h tool timeout. A first-ever `ptc-launch` must provision the
  venv inside that window; measured 3.75 s cold with a warm `uv` cache, but a cold cache
  (pandas/numpy/matplotlib wheels) can exceed it, and the failure mode is a plugin whose
  server silently fails to connect on the very first session.
  Evidence: `Claude Code Src/src/services/mcp/client.ts:457`; T11 timing of
  `plugin/bin/ptc-launch` against an empty `~/.ptc`.

- Observation: `uv venv` (0.11.27) refuses to create a virtual environment where one already
  exists — the exact state every stamp-mismatch upgrade starts from — so the provisioning
  call needs `--clear`. Both provisioner twins had this defect and neither's tests could see
  it: the unit tests inject a fake `run` that always succeeds.
  Evidence: `uv venv ~/.ptc/venv --python 3.12 --seed` → exit 2, "A virtual environment
  already exists"; fixed in `plugin/bin/ptc-launch` and `src/ptc/venv.py`, regression-covered
  by `test/integration/test_provision_upgrade.py`.

- Observation: [S5 verdict — PROMOTE] Claude Code 2.1.238 accepts an MCP `ImageContent`
  block and carries it through to the model's transcript unaltered. The host converts the MCP
  wire form (`{type: "image", data, mimeType}`) into the Anthropic content form
  (`{type: "image", source: {type: "base64", media_type, data}}`) and hands it to the model
  *after* the text block, preserving `_content`'s order; the base64 decodes to bytes that
  hash identically to the file the display shim wrote (18 897 bytes, 25 196 base64 chars,
  sha256 `f4deb1b0…927e46`) — byte identity, not an inference from length. That mechanical
  wire evidence is what carries the verdict: correct Anthropic image form, correct position
  after the text, byte-identical payload, `is_error` unset. The fallback branch exists for a
  block the host drops or mangles, and none of those happened. The assistant's own sentence
  about the plot is *consistent with* pixel reading but is not evidence for it — the title
  and the four data points were both in the tool input the model wrote one turn earlier, so
  the description is derivable without looking at the image at all. A future spike wanting to
  prove pixel reading must ask something answerable only from the rendering (line colour,
  y-axis tick values, gridline count) or plot data that never appears in the submitted code.
  `_content` stands as built in T8: image block first-class, PNG on disk as the
  durable copy. Two limits worth knowing: in a terminal "inline" means an `[Image]` marker,
  not pixels — the collapsed transcript shows only `Called plugin:ptc:ptc`, and the ctrl+O
  detailed transcript shows the text block plus a second `⎿ [Image]` row — and the shaped
  text does **not** yet name the saved PNG path, so a human reading the transcript has no
  route to the file. M1's image work should add that path line (the spec's *Images* row
  already calls for it); it is not a fallback, it is the human-side half of the same result.
  Evidence: runbook `test/spikes/s5_image_block.md`, run live on 2.1.238.
  Headless (`claude -p --output-format stream-json`, plugin dir, session
  `0d9b7cbf-…`): `TOOL_USE_NAMES ["ToolSearch", "mcp__plugin_ptc_ptc__exec"]`; the
  `tool_result` content array was
  `[{"type":"text","text":"[cell 2 · ok · 0.3s]\nS5_PLOTTED"},
  {"type":"image","source":{"type":"base64","media_type":"image/png","data":<25196 chars>}}]`,
  `is_error` unset; decoding that `data` yields 18 897 bytes, sha256
  `f4deb1b01495f328098e8389cc652b7849c0cc2f876453d5c1c4da71fe927e46`, equal to the sha256 of
  `~/.ptc/kernels/<key>/cells/2-0.png` in all three runs. The assistant then wrote "the
  rendered line plot titled \"s5\" with points 1, 4, 2, 8" — recorded for completeness, and
  non-probative, since both facts were in the tool input it submitted.
  Interactive (tmux, `claude --plugin-dir ./plugin`): detailed transcript
  rows `⎿ [cell 2 · ok · 0.3s] / S5_PLOTTED` then `⎿ [Image]`. On-disk shim output present in
  all three runs (keyless CLI, headless, interactive): `~/.ptc/kernels/<key>/cells/2-0.png`,
  PNG 534×434 RGBA.

- Observation: [S2 verdict — PROMOTE] `resume=<parent sid> + fork_session=True` works from
  *inside a tool call the parent is still running*, and the fallback the spec pre-designed is
  not needed. The mechanism, read off the on-disk transcripts rather than inferred from the
  answer: Claude Code appends each message to `~/.claude/projects/<slug>/<sid>.jsonl` **as it
  completes**, so at fork time the parent's file already held the turn's user message *and* the
  assistant text emitted seconds earlier in that same turn — only the tool_result of the
  still-running Bash call was missing (it cannot exist yet). "Partially flushed" is therefore
  one *message* back, not one turn back, and the useful framing for the skill is not "fork
  between turns" but "a fork sees everything except the cell it is being called from". This
  flush-per-message model is read off record *timestamps*, not measured write latency directly;
  in this run the fork query was issued ~7.5s after the last flushed record, so a tight race —
  a fork issued milliseconds after the preceding message completes — is untested. This does not
  threaten the mechanism (the fork point is always inside a tool call, whose `tool_use` record
  is written before the tool runs), but a future spike wanting write-latency evidence would need
  to narrow that gap deliberately.
  Three mechanism facts M2 should build on. (1) **The dangling `tool_use` is elided.** The
  parent's last flushed record was an `assistant`/`tool_use:Bash` block with no matching
  tool_result; the child's transcript copies the content-bearing prefix *up to but not
  including* it (the session-scaffolding records — `queue-operation`, `last-prompt`,
  `atis-latch` — are not among the copied ones; the child re-derives its own instead). That
  pruning is why a mid-turn fork is well-formed at all — nothing else resolves the orphaned
  call — and it means the child cannot see the arguments of the very tool call that forked it.
  (2) **The copied records keep their original `uuid`s and timestamps, but four fields are
  rewritten to the child's own identity, not just `sessionId`**: `sessionId`, `entrypoint`
  (the forking client's, e.g. `sdk-cli` → `sdk-py`), `version` (the forking client's CLI
  version, not the version that actually produced the historical message), and `promptId`.
  There is no fork-provenance field anywhere in the child file — none of those four rewritten
  fields points back at the parent — so `agent.fork` must record the child `session_id` from
  the SDK `ResultMessage` itself if the registry is to link a child back to its parent.
  (3) **A fork is not a cheap primitive, and `setting_sources=[]` does not make it one.** The
  child paid a cold cache write — `cache_creation 15 321`, `cache_read 0` — for a two-line
  answer, at a cost of $0.308 on `claude-fable-5` pricing (cost scales with model tier; a
  cheaper model would cost proportionally less for the same token count). Roughly half of
  those 15,321 tokens is inherited parent context (the parent's at-fork payload — skill
  listing, an agent-listing delta, two hook attachments, the user message — is on the order of
  7–9k tokens by character count), not all of it; the rest is the child's own baseline (system
  prompt) that any fresh session would also pay. `setting_sources=[]` stops the child loading
  *its own* config and hooks, but does not stop the parent's skill listing and hook-output
  attachments riding along inside the resumed transcript, and a real mid-session parent is far
  larger than this 14-record one — so `agent.fork` cost grows with parent size *and* model
  tier. The one number that *is* mechanism rather than extrapolation is `cache_read_input_
  tokens: 0`: a fork never reuses the parent's prompt cache, so every fork pays a full cold
  write regardless of how many prior forks touched the same parent. That is the real reason to
  price `agent.fork` in the skill as a deliberate one-shot, never a loop body.
  Evidence: `test/spikes/s2_live_fork.py`, one live pass on 2.1.238 (parent `claude -p`,
  session `46422f60-…`, 25.4 s, rc=0). Snapshot taken inside the running Bash call —
  `FORK_SNAPSHOT records=14 bytes=39462`, last three records `user str[544]` (MARKER) /
  `assistant text[36]` = `"INVENTED: vorbulent-skreeth-omnidrax"` / `assistant tool_use:Bash`.
  Child (`45e8987d-…`, CLI 2.1.237) replied `MARKER: quokka-basilisk-42\nINVENTED:
  vorbulent-skreeth-omnidrax`, `subtype 'success'`, `terminal_reason 'completed'`,
  `total_cost_usd 0.30844`; the invented phrase appears nowhere in the parent's prompt, so it
  was recallable only from the mid-turn assistant record. Parent's own file was untouched by
  the fork (14 records before and after; it then appended its tool_result at 07:24:11 and
  finished normally).

- Observation: [S4 verdict — PROMOTE] `thread/start {cwd, approvalPolicy: "never", sandbox:
  "read-only"}` completed a trivial turn on `codex app-server` with **zero** server→client
  requests of any kind — not one approval, elicitation, or permission prompt. Turn status
  `completed`, agent text exactly `CODEX-OK`, 5.2 s of model time.
  Request and response, verbatim off the wire:
  `→ {"id":2,"method":"thread/start","params":{"cwd":"…/ptc-surface/ptc","approvalPolicy":"never","sandbox":"read-only"}}`
  `← {"id":2,"result":{"thread":{"id":"01a0234c-c405-7ba2-b09c-c5ad4e545052",…},"approvalPolicy":"never","approvalsReviewer":"auto_review","sandbox":{"type":"readOnly","networkAccess":false},"model":"gpt-5.6-sol","modelProvider":"openai","cwd":"…"}}`
  `→ {"id":3,"method":"turn/start","params":{"threadId":"01a0234c-c405-…","input":[{"type":"text","text":"Reply with exactly this and nothing else: CODEX-OK"}]}}`
  `← {"id":3,"result":{"turn":{"id":"01a0234c-c513-77e1-9ca1-d39ce2ec31d6","items":[],"itemsView":"notLoaded","status":"inProgress",…}}}`
  `← {"method":"turn/completed","params":{"threadId":"01a0234c-c405-…","turn":{"id":"01a0234c-c513-…","items":[{"type":"agentMessage","id":"msg_0981835f…","text":"CODEX-OK","phase":"final_answer"}],"itemsView":"summary","status":"completed","durationMs":5228}}}`
  The verdict generalizes past this no-tool turn for five of the six approval classes: exec,
  apply-patch, sandbox-escalation, `request_permissions`, and server-originated elicitation all
  short-circuit on `AskForApproval::Never` *before* any client request is emitted, and none of
  them consults `approvalsReviewer` — `core/src/tools/sandboxing.rs:203` (exec) and `:354`
  (sandbox escape), `core/src/tools/runtimes/apply_patch.rs:187`, `core/src/session/mod.rs:2435`
  (`request_permissions` satisfied server-side), `codex-mcp/src/elicitation.rs:409`
  (elicitations rejected by policy). That also disposes of the one confound on those five paths:
  this machine's `~/.codex/config.toml` sets `approvals_reviewer = "guardian_subagent"`, so
  absent those branches one could argue a server-side reviewer absorbed the prompts rather than
  none being raised. It is source reading, not a second live run — corroboration, not evidence.
  **MCP tool-call approvals are the sixth class, and they do not short-circuit the same way.**
  `core/src/mcp_tool_call.rs:1295` gates on `mcp_permission_prompt_is_auto_approved`, which under
  `Never` (`codex-mcp/src/mcp/mod.rs:85-105`) returns true only for
  `PermissionProfile::Disabled`/`External` or a `Managed` profile with full disk-write access —
  **not** for `PermissionProfile::read_only()`, the profile this spike's own params select. A
  tool with no annotations defaults to requiring approval
  (`core/src/mcp_tool_call.rs:2161-2178`); the guardian branch only handles
  `OnRequest`/`Granular` (`mcp_tool_call.rs:1353`); so `Never` + `read-only` falls through to a
  real server→client request — either `mcpServer/elicitation/request`
  (`core/src/session/mcp.rs:455`) or `item/tool/requestUserInput`
  (`core/src/session/mod.rs:2638`, which has no policy check at all). This spike's own thread
  inherited four of the user's MCP servers (below), so that path is realistically reachable.
  **Consequence: T22's client-side responder for server→client requests is load-bearing, not
  optional** — the trivial-turn PROMOTE verdict and the promoted `thread/start` params stand,
  but "every approval path short-circuits on Never" does not extend to MCP tool calls.
  Four wire-shape corrections T22's sketch needs, all checked against the schema the
  *installed* binary generates (`codex app-server generate-json-schema --out DIR`) rather than
  against the in-repo protocol crate, which is a fork and may lead or lag it.
  (1) **`clientInfo.version` is required** alongside `name`; `title` is optional.
  (2) **The handshake is two messages** — `initialize` request, then an `initialized`
  *notification* (`ClientNotification` has exactly that one variant, no params). Any other
  request before it is refused with `"Not initialized"`.
  (3) **`sandbox` and `sandboxPolicy` are different types, and only one is camelCase.**
  `thread/start.sandbox` is `SandboxMode`, a kebab-case string enum
  (`"read-only" | "workspace-write" | "danger-full-access"`). `turn/start.sandboxPolicy` — and
  the `sandbox` field in every *response* — is `SandboxPolicy`, an internally-tagged object
  with camelCase tags (`{"type":"readOnly","networkAccess":false}`, `workspaceWrite`,
  `dangerFullAccess`, `externalSandbox`). The live exchange shows both halves: we sent
  `"read-only"` and read back `{"type":"readOnly",…}`. The external review's `workspaceWrite`
  correction was right for the per-turn override and wrong for `thread/start`. Forward-compat
  note: `app-server/README.md:142` documents `thread/start.sandbox` itself as a **legacy
  shorthand** — "prefer experimental `permissions` profile selection by id; the legacy
  `sandbox` shorthand is still accepted but cannot be combined with `permissions`" — and the
  schema description echoes it ("retained for compatibility... prefer
  `activePermissionProfile` for profile provenance"). Proven on 0.146.0 and right to promote
  now, but T22 is on a deprecation track; this is the same permissions-profile machinery that
  decides the MCP tool-approval behavior above.
  (4) **A blanket `{"decision":"accept"}` is invalid for eight of the ten server→client
  methods** — the legacy v1 `execCommandApproval`/`applyPatchApproval` use `ReviewDecision`,
  whose accept value is `"approved"`; `item/permissions/requestApproval` wants `{permissions}`;
  `mcpServer/elicitation/request` wants `{action}`; `item/tool/requestUserInput` wants
  `{answers}`; `item/tool/call` wants `{contentItems, success}`; `attestation/generate` wants
  `{token}`; `account/chatgptAuthTokens/refresh` wants `{accessToken, chatgptAccountId}`. Only
  `item/commandExecution/requestApproval` and `item/fileChange/requestApproval` take
  `{"decision":"accept"}`. This matters precisely on the fallback path: an invalid reply
  looks exactly like no approval having arrived.
  Three things T22 should build on. (a) **The final text arrives three times and only one
  copy is safe.** It streams as `item/agentMessage/delta`, lands authoritatively as
  `item/completed` with `item.type == "agentMessage"` / `item.phase == "final_answer"`, and
  appears again inside `turn/completed.params.turn.items`. Accumulate from `item/completed`
  and take only `status`/`durationMs` from `turn/completed`: that notification carried
  `"itemsView": "summary"`, an explicit signal its `items` array is a projection that may
  elide items on a longer turn. The `userMessage` is emitted as an item pair too, so filter
  on `item.type` rather than counting. (b) **`turn/interrupt` requires both `threadId` and
  `turnId`** (`TurnInterruptParams`), so the turn id must be retained from the `turn/start`
  *response* (`result.turn.id`) — not from the `turn/started` notification, which a client not
  yet draining can miss. Success is `{}` and the turn ends `status: "interrupted"`.
  (c) **`thread/start` inherits the user's entire `~/.codex` configuration**, which is an
  unbudgeted cost this spec had not accounted for: the run started four of the user's MCP
  servers (`node_repl`, `context7`, `discord`, `openaiDeveloperDocs`), fired a plugin
  `session-start` hook and a `stop` hook inside the PTC-spawned thread, and took the user's
  configured model `gpt-5.6-sol`. The wire shows MCP startup spanning 0.94–2.40 s and the
  session-start hook at 4.19–4.21 s, but the 2.40–4.19 s window carries no notification at all
  and is not attributable to a specific cause — do not read a precise "N of 6.2 s" split into
  this. The harder datum: `thread/tokenUsage/updated` reports **25,647 total input tokens**
  (11,008 cached) for a turn whose output was 8 tokens — that overhead, not the wall-clock
  split, is the durable argument for isolating PTC's codex children. Two untested seams exist
  for isolating it: `thread/start` accepts a free-form `config` overlay object, and
  `codex app-server` accepts `-c key=value` at spawn.
  Evidence: `test/spikes/s4_codex_headless.py`, one live pass on codex-cli 0.146.0
  (macOS 26.5.2 arm64, ChatGPT subscription auth). Full 43-message bidirectional transcript at
  `/tmp/ptc-s4-codex-headless/wire.jsonl`; `run.json` records `"server_request_methods": []`.
  Notification order observed: `remoteControl/status/changed` (first inbound notification) →
  `thread/started` → `mcpServer/startupStatus/updated` ×4 → `thread/status/changed` →
  `turn/started` → `mcpServer/startupStatus/updated` ×12 (16 total) →
  `hook/started`/`hook/completed` → `item/started`/`item/completed` (userMessage) →
  `item/started` → `item/agentMessage/delta` ×4 → `item/completed` (agentMessage) →
  `thread/tokenUsage/updated` → `account/rateLimits/updated` →
  `hook/started`/`hook/completed` (stop) → `thread/status/changed` → `turn/completed`.

- Observation: [T22, live on codex-cli 0.146.0] Three wire facts the S4 corrections implied but
  did not test, plus one the fidelity of a real transport forced.
  (1) **A misplaced param is silently inert, not an error.** `thread/start {cwd,
  approvalPolicy: "never", sandbox: "read-only", effort: "high"}` succeeded and came back with
  `reasoningEffort: "xhigh"` untouched — the user's setting. So the sketch's habit of putting
  `effort` on `thread/start` would never have raised anything; it would simply have never
  applied. `effort` and `outputSchema` are `turn/start` fields, where the schema declares them.
  (2) **The camelCase sandbox tag is refused outright**, confirming S4's correction live:
  `thread/start {"sandbox": "workspaceWrite"}` → `-32600 unknown variant \`workspaceWrite\`,
  expected one of \`read-only\`, \`workspace-write\`, \`danger-full-access\``.
  (3) **asyncio's default 64 KiB line limit is not enough for this protocol.** The first live
  run died with `ValueError('Separator is not found, and chunk exceed the limit')` on a single
  `mcpServerStatus/list` result carrying the user's MCP tool catalogs (>100 KB on one line).
  NDJSON has no continuation, so one over-long line kills the session; the client passes an
  explicit 16 MiB `limit` to `create_subprocess_exec`, and a keyless test drives a 200 KB line
  through the fake so the regression cannot come back.
  (4) **A15 met**: `agent.spawn(..., provider="codex")` in a real kernel returned exactly
  `CODEX-DONE`, turn `status: completed`, **zero** server→client requests, zero hooks, a real
  thread id in `agents.json`, and 19,113 input tokens for 8 output.
  The corollary of that zero, which reads as good news and is also a coverage gap: **no
  server→client reply has ever been field-proven.** The eight per-method replies are
  schema-correct and shape-tested against the strict fake only; the live turn never drew a
  single request, so the one path S4 showed does *not* short-circuit on `never` +
  `read-only` — an MCP tool-call approval — remains unexercised in production. The live
  tier's free schema-drift test (`test_the_fakes_hand_transcribed_enums_still_match_the_
  installed_schema`) narrows this to "the shapes are still the right shapes"; it cannot show
  the server ever accepted one.
  Evidence: `test/live/test_codex_live.py` (one billed turn, subscription auth), the kernel's
  own cell log for that run, and `test/unit/test_codex_backend.py` (30 keyless tests against a
  fake that enforces the handshake, the sandbox enum, per-method reply shapes and the turn id).

- Observation: [S6 verdict — PROMOTE, carrier corrected] WebSearch's results are reachable and
  cleanly mappable, but the block that carries them is **not structured**. The live stream is
  `AssistantMessage[ToolUseBlock(name='WebSearch')]` → `UserMessage[ToolResultBlock]` →
  `AssistantMessage[TextBlock]` → `ResultMessage`, and the `ToolResultBlock.content` is a
  **`str`**, not the `list[dict]` the type union also permits. Inside that string the
  machine-readable payload sits on one line — `Links:` followed by a JSON array of
  `{"title", "url"}` objects, **no snippet field at all** — with the model's prose after it and
  a trailing `REMINDER: You MUST include the sources above …`. So the promoted path is a JSON
  parse of that single line (deterministic; the prose scrape is a true fallback, and the two
  must not both run or prose urls dilute a good answer), and `SearchResult.snippet` is honestly
  empty rather than backfilled from the write-up. Two design consequences beyond the parse:
  (1) domain filters are re-applied to the returned urls, because a prompt hint is the only way
  to reach the tool's own `allowed_domains`/`blocked_domains` and a hint is not a guarantee;
  (2) results are correlated to a `WebSearch` `tool_use` id so another tool's output is never
  scraped for links, degrading to "take every tool result" rather than to silence if ids ever
  stop lining up.
  Evidence: `test/spikes/s6_websearch_shape.py`, one live run 2026-08-22 (bundled CLI via SDK
  0.2.142), verbatim head of the block —
  `ToolResultBlock(tool_use_id='toolu_01FXvPF29sbj6R72HyphiBRv', content='Web search results
  for query: "anthropic claude agent sdk release notes"\n\nLinks: [{"title":"Claude Platform
  release notes - Claude Platform Docs","url":"https://platform.claude.com/docs/en/release-
  notes/overview"},{"title":"Releases · anthropics/claude-agent-sdk-python","url":"https://
  github.com/anthropics/claude-agent-sdk-python/releases"}, … 8 entries]\n\nBased on the search
  results, …')`. The same block (abridged to three entries, otherwise verbatim, prose tail and
  REMINDER included) is the fixture pinning the parse in `test/unit/test_web.py`. A7's engine
  ran live afterwards: 8 results, all 8 titled, every url absolute, first hit
  `https://support.claude.com/en/articles/12138966-release-notes`, one `web_search` audit
  record written for the cell. The prose-fallback branch — the path that runs when no
  `Links:` payload is present at all — has never fired against real data: every WebSearch
  response observed so far, S6 and A7 alike, carried a well-formed `Links:` line, so that
  branch is pinned only by a synthetic fixture in `test/unit/test_web.py`.

- Observation: the shared concurrency pool cannot wrap `web_fetch` end to end. `web_fetch`'s
  optional `prompt=` summarization calls `llm()`, which takes a permit from the *same* pool, so
  a `web_fetch` holding its own permit across that call self-deadlocks — at `max_concurrency=1`
  immediately, and at any bound once that many summarizing fetches run at once. The permit now
  covers the HTTP fetch only and is released before the model call; `timeout` is the fetch
  deadline and the summarization runs under `llm()`'s own. Pinned by
  `test_web_fetch_summary_does_not_deadlock_against_llm`, which runs the whole path at
  `max_concurrency=1` under a wall-clock bound.

- Observation: acceptance run 2026-08-22 (T28, Claude Code 2.1.238, codex-cli 0.146.0):
  A1–A15 → **15 pass**, of which one (A13) passes in its documented S5 fallback-qualified
  form and none failed or needed a code change. A13: the image content block reaches the
  model on the wire (`['text','image']`, `is_error` unset, base64 25 196 chars decoding to
  18 897 bytes, sha256 `f4deb1b0…927e46`, byte-identical to the kernel's own
  `cells/9-0.png` and to T12's recorded hash) and the ctrl+O transcript shows a `⎿ [Image]`
  row, while the collapsed view shows only `Called plugin:ptc:ptc` — a terminal renders no
  pixels, so "visible" means the marker, and the shaped text still does not name the saved
  PNG path (the Images-row residual, re-confirmed, still parked). Everything else was
  observed first-hand: A1/A2 in one real `--plugin-dir` session (`x = 42`, the CLI process
  exited, `--resume` re-entered session `2ea20cbd…` and `print(x)` → `42` on the same
  kernel pid 81470); A3 live end to end including fresh-adapter recovery across two separate
  `claude -p` processes (cell 12 yielded `running` in one adapter, a brand-new adapter's
  `wait` still tracked it, `interrupt` settled the terminal record to
  `status: "interrupted"` with `KeyboardInterrupt`, and the next `exec("1+1")` → `2`);
  A4 fan-out/fan-in plus the post-`restart` half (namespace gone, registry still listing
  t0/t1/t2 `done`, `agent.resume(<sid>).send()` → `RESUMED`); A5 `agent.fork` recalling
  `ORCHID-42` from turn 1 of the parent conversation, three turns and one resume later;
  A6 footer `edited /private/tmp/ptc-t28/repo/note.txt (+1/−1)` in the tool result the model
  saw, with the matching `audit.jsonl` row; A9 across a REAL `/compact`
  (`compact_boundary`, trigger `manual`, 45 821 → 4 378 tokens) with `history().user()`
  still holding the first prompt verbatim afterwards; A10 the session's own Bash tool running
  `~/.ptc/venv/bin/ptc exec 'print(x)'` → `42` on the same kernel and cell counter;
  A12 in live phrasing — 12 137-char tool result carrying
  `… [truncated 88001 chars — full output: …/cells/15.log]` over a 100 001-byte log;
  A14 the child pasting back `agent depth limit reached (PTC_DEPTH=1, PTC_MAX_DEPTH=1)` from
  its own kernel. A7/A8/A11/A15 are their live tests, run in the suite below.
  Two mechanics worth keeping: `claude -p --resume <S>` preserves the session id, so A1/A2's
  "quit and resume" is honestly scriptable without a terminal; and a child agent only gets
  its own ptc MCP server when the kernel was spawned by the plugin launcher (which is what
  exports `PTC_LAUNCHER`), so A14 driven from a bare `ptc exec` kernel finds no tool and must
  run inside a real `--plugin-dir` session.
  Evidence: keyless suite 209 passed 0 skipped (92.6 s); live suite 8 passed 0 skipped
  (121.6 s, `PTC_LIVE=1`, subscription auth, no `ANTHROPIC_API_KEY` in the environment or in
  the kernel's own env per A7's in-cell check); per-cell notes above.

- Observation: a fail-open rule masks defects in its consumers — closing bgroups' unknown-identity hole (r10) immediately failed an existing shell test and exposed that `BashHandle` held a SNAPSHOT of its registry row (`dict(...)` at registration) while `_retire` marks `leader_exited` on the row in place, so the handle never saw the mark and, under the new fail-closed rule, `kill()` on a daemonizer read its own orphan group as unidentifiable. The old fail-open bias had been answering "unknown → signal anyway", which happened to give the right kill for the wrong reason. Fixed by holding the row object itself.
  Evidence: `test_killing_a_daemonizers_handle_reaps_the_group_its_row_was_kept_for` failed only after `_recycled` failed closed ("h.kill() never reached descendant … — its row named it"); passes with the shared-row fix. Commit 47ef638345.

- Observation: the shipped marketplace install path could never have started — and dev installs are what hid it. `ptc-launch` resolved the Python package as the plugin root's PARENT, but a marketplace cache copies only the directory holding `.claude-plugin/plugin.json`; every acceptance run used `--plugin-dir` against the checkout, where the parent exists, so nothing keyless or live ever exercised the layout the Decision Log declares shipped. r11 inverted the root (the package directory IS the plugin root) with a structural containment test so the package cannot silently walk back out.
  Evidence: r11 finding 1 (P1); commit 818c573297; `test_the_plugin_root_contains_everything_the_launcher_hashes`. No live marketplace install has been performed — recorded as residual 9.
  Date: 2026-08-22.

- Observation: on Linux a dead kernel could pass every liveness check as a zombie — `/proc/<pid>/stat` keeps serving the original start ticks and `kill(pid, 0)` succeeds, and `_start_ticks` never read the STATE field; the spawning adapter holds no `Popen`, so nothing reaps it. macOS had been answering "dead" for such pids only BY ACCIDENT (libproc refuses the read, and the `ps lstart` fallback string can never equal a `btime=` record) — the r13 fix makes both platforms answer for the right reason, the macOS half measured live against a real zombie rather than assumed from headers.
  Evidence: r13 finding 1; commit 6996c5c64e; fabricated zombie stat line RED test + live macOS probe in the r13 fix report.
  Date: 2026-08-23.

- Observation: Claude Code auto-backgrounds an in-flight MCP tool call at ~2 minutes
  (`CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`, default 120000) and delivers the tool's full
  result verbatim as a task notification when it completes — measured live, not just read
  from docs: `wait(cell_id=2, timeout_s=3600)` over a 150 s cell was moved to a background
  task at exactly 120 s and the notification carried `[cell 2 · ok · 150.0s]`. This makes
  the async-PTC pattern a single long-budget `wait` call; the background-Bash + blocking
  CLI wait composition demotes to a fallback for hosts without MCP auto-backgrounding.
  Evidence: live probe 2026-08-24, session 56c9ce21; SKILL.md doctrine rewritten same day.

- Observation: the first full-migration dogfooding session surfaced a result-misattribution
  trap that was pure RENDER, not attribution: the submission path holds the submit lock from
  busy-check to current.json publish, so a cell id can never bind to the wrong submission —
  but the Busy text said "Use wait(cell_id=N) for its output", and on a kernel shared with
  subagents the caller waited on a cell it never submitted and adopted another caller's
  result as its own. Same session: Python-string→shell double-escaping silently broke an
  inline prompt (empty output, exit ok), and `timeout_s` (MCP) vs `timeout` (in-kernel bash)
  had to be recovered via `inspect.signature`.
  Evidence: live feedback 2026-08-30; fixed in 5954301 (Busy render names whose cell it may
  be), 5f61ff5 (`bash()` argv-list form — no shell, one parser), 64e7f6f (SKILL.md shared-
  kernel/session isolation, argv-form, timeout-vocabulary doctrine). The Monitor-counterpart
  gap (no mid-run event stream, only completion) is issue #1 on the repo.

## Outcomes & Retrospective

Written 2026-08-22 at finish.

**What was achieved against the purpose.** The feature shipped whole: every Claude Code
session can now get a detached, persistent IPython kernel — provisioned on first use by the
plugin launcher, discovered through the hook run-file → env → adapter-local chain, driven
through the five MCP tools (`mcp__plugin_ptc_ptc__exec/wait/interrupt/restart/kernels`) and
the `ptc` CLI, with `read/write/edit`, `bash()`, the `agent` namespace (Claude and Codex
backends, fork/resume), `llm()`, `web_fetch`/`web_search`, `history()`, and the `workflow`
helpers pre-bound in the kernel namespace, and the skill teaching agents to route bulk work
there unprompted. All six spikes (S1–S6) returned PROMOTE in qualified forms recorded in
this tail. The acceptance section was executed as written: A1–A15 all pass (A13 in S5's
qualified form), with 12 of 15 cells independently re-derived from on-disk artifacts by the
task-28 reviewer.

**How it was built and verified.** 28 tasks under subagent-driven execution, each with a
fresh executor and a two-stage review; every task closed review-clean (most after one fix
round, T20 after two). The external whole-branch review (codex, gpt-5.6-sol) then ran nine
rounds against a scoped synthetic base, yielding 77 further findings fixed in nine
single-fixer waves — none of them ever recurred once fixed, and severity declined
monotonically from architecture-class (unbounded model-visible output, event-loop
monopolization, orphaned process trees, a fail-open admission marker) to peripheral
precision items, at which point convergence was declared. The keyless suite grew from 209
at task close to 370 at campaign end, all green with zero skips; the live tier (8 tests)
passed under PTC_LIVE=1.

**What remains.** The residual ledger above is the authoritative list (eight recorded
residuals, including the codex-transcript `history()` v1 boundary, the no-session
`interrupt()` contract being fake-proven only, and the prose-fallback path never having run
on real data). Three operational notes from the final round: a stale `uv.lock` is now a
hard provisioning failure by design; the macOS process-birth identity is PTC's one C-ABI
dependency (it degrades to an explicit refusal, never a guess); and `_EOF_GRACE_S` for
codex shutdown is a judgment value pending one live measurement. The adjudicated
instrument-mismatch family (the reviewer's repo-local token ceiling vs PTC's spec-recorded
output caps) is documented in the Decision Log and should be cited rather than re-litigated.

**Lessons.** (1) Controller carry-forward claims about shipped code must be repo-verified
before binding executors — the one wrong premise this plan issued (T13's predicate) was
caught only because the executor read `git log -p` first. (2) Honesty rounds pay for
themselves: the reviews that attacked evidence quality (T12's "model saw pixels", T18's
derivable marker, T19's absence claims) repeatedly turned testimony into mechanical proof.
(3) An external reviewer that cannot be steered will re-derive its own house rules every
round; adjudicate once in the Decision Log and extend that entry on recurrence instead of
re-arguing. (4) Merge-base tooling needs the synthetic scoped base to be an ancestor —
build base-then-replay, not base-from-head. (5) Long external jobs outlive harness
background wrappers; a self-owned tmux session with file-polled output is the reliable
detachment.


**Extended campaign addendum (r10–r16, 2026-08-22..23).** After the nine-round campaign
closed, a further seven whole-branch rounds ran at the user's invocation on the same
scoped-synthetic-base method. They raised 37 findings — 36 fixed (35 fully, 1 partially by
design with its unreachable half recorded as residual 10), 1 the standing split-the-feature
adjudication, cited — with ZERO recurrences of any fixed item across all sixteen rounds.
Severity rose mid-campaign (r13: five P1, including zombie-liveness and a ZeroMQ
subscription race) then declined monotonically to r16's single refinement-grade P1;
convergence was declared when two consecutive rounds returned only refinements of the
campaign's own hardening. The keyless suite grew 370 → 454. Structural changes of note:
the package root became the plugin root (marketplace-capable), foreground output was
bounded (a deliberate contract overturn, in the Decision Log), interrupted cells exit 130,
and the residual ledger grew from eight to ten. The r9 lesson sharpened: on a ~17k-line
surface, each external round samples rather than exhausts — "a quiet round" means the
reviewer's marginal yield fell, and only a declining multi-round trend, not one clean
round, supports stopping.

## Revision Notes

- 2026-08-23 (r16, campaign close): round 16 confirmed all three findings (1 P1). Confirmed-dead kernels reap their recorded process group under the existence gate (1efe150061); interrupt binds to the cell captured at entry (647a30bd7b); kernel-lifetime config travels with the kernel — Decision Log entry (3dc369e16b). Extended campaign r10–r16 declared converged; addendum at the end of Outcomes & Retrospective.

- 2026-08-23 (r15): round 15 confirmed all five findings (2 P1, refinement-grade). Bootstrap client synchronizes its SUB channel (7b8c01c0f9); meta persists and restart restores max_depth beside depth (9bdd6a41f7); run files bind to the Claude ancestor's birth incarnation — hook records, discovery verifies, absent-field migrates (fdb029087d); explicit since=0 stays authoritative over archived cursors (0b888807d6); sanitized session keys carry a 64-bit digest — dirty-alias kernels re-key on upgrade, stated migration (a6f070e4ba).

- 2026-08-23 (r14): round 14 confirmed all six findings (2 P1, both refinements of earlier campaign hardening — first severity decline). Birth-stamp records are refuted only same-source (728f27a09e); the middle registration path carries the setsid construction proof (2f049799de); record-then-log rotation windows re-settle from the archive (1cc4ca2aff); interrupted cells exit 130 — new documented code beside 0/1/3 (32e551dd8c); generated agent-handle names are 48-bit and claimed against persisted rows (619ad859e1); an image moved mid-reply costs a note, not the reply (cfa4ae9cf9).

- 2026-08-23 (r13): round 13 confirmed all seven findings (5 P1). Zombie-state-aware identity (6996c5c64e); SUB-handshake race closed two-layer — wait_for_ready plus disk-derived cell id (ce79e4c024); interrupt fallback bound to its entry incarnation (294d74586b); expiry notice survives failed respawns (7fcadffff3); venv-upgrade recycle via .ptc-version identity — Decision Log (05219fe77e); bootstrap reconciles orphaned registry rows — Decision Log (2763c81ac1); codex-teardown kernel-side release + residual 10 (f5a2cd6fc8).

- 2026-08-23 (r12): round 12 confirmed all seven findings (3 P1). Admission/settlement decisions now use the settled tri-state identity (unknown discharges nothing and settles nothing, a3e42a70b8); unregistered background handles signal their own child only (a571561bd3); foreground output bounded — Decision Log entry above (c62946d354); `ptc kill` reports the signal not the record (b2956f9220); child key entropy 24→48 bits and `live` derived from real session state (a0158eb270); UTF-8 straddle at the read boundary preserved (0a9a78ce9c).

- 2026-08-22 (r11): external round 11 confirmed 5 of 6 findings (the sixth is the standing split-the-feature adjudication). Headline: the marketplace install path was structurally broken — root inverted, package root == plugin root (818c573297); dev-install line 607 and the two standing path references updated, historical Surprises mentions kept. Also fixed: leader_exited staleness bound + existence-gated exemption and construction-known pgid for leaders that race registration (220fd6c2fe), epoch rotation now migrates caller cursors (8e36d7146e), child session id captured mid-stream (b4857ac414). Residual ledger grows to nine (live marketplace install untested).

- 2026-08-22 (post-close r10): a tenth external-review pass surfaced three findings past r9's convergence — the archive-settle epoch guard, the bgroups unknown-identity fail-open (r9's parked item, elevated), and the pending marker's missing nonce. All three fixed (commit 47ef638345); Surprises gains the masked BashHandle row-snapshot defect the fail-closed rule exposed. The drift test's currentTime/read entry was reclassified a declared forward loan over the installed binary's schema (commit 7eb05a6fbc).

- 2026-08-20: Initial spec from the approved design (brainstorming session over
  `Conversation.md`, Prime Agent source, CC-to-SDK, Claude Code 2.1.236 reference source).
  Includes the post-presentation user revisions: SDK-only `llm`/`web_search` (no `anthropic`
  dependency), and no search guidance in the skill.
- 2026-08-20 (same day, post-review): Applied the independent adversarial review — terminal
  records + wait cursors, flock-based spawn ownership and atomic busy-check, hook tree-walk +
  `meta.json` key/session separation, child env + `mcp_servers` rules, cooperative depth-brake
  wording, `structuredContent` dropped, output caps, 24 h TTL + expiry notice, provisioning
  launcher, two-tier testing with a non-skippable keyless gate, M0 spike gate, Windows
  non-goal. Rejections and the live counter-evidence are recorded in the Decision Log and
  Surprises & Discoveries.
- 2026-08-20 (planning pass): five drifts found while writing the execution plan and fixed
  here — `nest_asyncio` is now conditional on spike S1 (ipykernel awaits natively);
  the shaped result body is the kernel-side merged stream log (stdout/stderr interleaved in
  arrival order), replacing the per-line "stderr:" prefix which a single tee cannot produce;
  `llm()` loses its `max_tokens` parameter (the Agent SDK exposes no such knob); the venv
  extras gain `matplotlib` (the display shim needs the inline backend to see figures); and
  `cells/` rotates to `cells-prev-<ts>/` on every kernel respawn so execution counts never
  collide across kernel processes. Plan: `docs/doperpowers/plans/2026-08-20-ptc-kernel.md`.
- 2026-08-20 (plan review): the independent plan review's ten accepted findings are folded
  into the plan and reflected here — atomic admission wording under the exec row, monotonic
  cell ids + archived settlement in the capture section, and the plan-review Decision Log
  entry above.
- 2026-08-24: residual 9 closed — standalone repo `SSFSKIM/ptc-tool` published (subtree split, tree-identical), single-plugin marketplace.json added there, live `claude plugin marketplace add` + `install` + fresh-session `exec` all verified from the marketplace cache. Decision Log gains the standalone-repo entry; the monorepo copy stays the dev workspace.
- 2026-08-24: spec and plan moved from the codex_somersault monorepo (`ptc-surface/docs/doperpowers/`) into this standalone repo, following the code. Historical `ptc-surface/ptc/...` paths and commit hashes in this document refer to the monorepo layout and history at campaign time; in this repo the package root is the repo root, and the pre-split history lives in the monorepo.
- 2026-08-24: async doctrine simplified — a long-budget `wait` auto-backgrounds at the harness's 2-minute threshold (measured) and its result returns as a task notification; SKILL.md leads with that, CLI-in-background-Bash kept as the no-auto-background fallback. Also: MCP server declaration moved inline into plugin.json (root `.mcp.json` doubled as broken project-scope config in checkout sessions); v0.1.1.
- 2026-08-30: dogfooding wave 1 — Busy render no longer invites adopting another submitter's output; `bash()` accepts an argv list (runs without a shell); SKILL.md gains shared-kernel session isolation, argv-form, and timeout-vocabulary doctrine; Monitor-counterpart `wait(until=)` sketched as issue #1; v0.1.2.
