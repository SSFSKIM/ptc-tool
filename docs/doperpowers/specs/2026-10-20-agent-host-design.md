# ptc agent host — mass fan-out without a process per call

Today every `llm()` call and every `agent.*` child the kernel starts is its own `claude`
CLI process: 300–450 MB and about 1.7 s of start-up each, measured below. A fan-out is
therefore bounded by the machine, not by the model's rate limits. `PTC_MAX_CONCURRENCY`
defaults to 8 for that reason. On 2026-10-20 one session's 8-wide `llm()` classification
fan-out held 8 CLIs at once, about 114 % of a core and 2.5 GB between them, every one of
them living a few seconds.

After this change a kernel runs **one** long-lived headless `claude`, the *agent host*,
and runs `llm()` calls and lean subagents *inside* it. Hundreds can be in flight on one
process. Every child, on either route, also gets a *profile* that chooses exactly what
goes into its prompt. At the floor that is 52 tokens where a call pays 379 today, and a
read-only investigator pays about 2.6 k tokens where a `general-purpose` subagent pays
about 26 k.

How to see it working: in a ptc cell, `await asyncio.gather(*[llm(f"Reply with exactly
the number {i}.") for i in range(64)])` returns 64 correct replies, and while it runs
`ps` shows one host process for the kernel instead of a rotating set of CLIs.

This builds on the kernel design in `docs/doperpowers/specs/2026-08-20-ptc-kernel-design.md`
(the agent namespace, `llm()`, the shared semaphore, the trust model). Read its
"Agents" and "Sub-LM calls" sections first; this spec changes what they say about
routes and concurrency and nothing else.

## Progress

- [x] (2026-10-20) Design discussed and approved with the owner; measurements below taken.
- [x] (2026-10-20) Spec written.
- [ ] SDK pin to `claude-agent-sdk==0.2.169` (bundled CLI 2.1.305) shipped on `main` as v0.5.5 — a separate change, in flight; M1 needs it.
- [ ] Spec review (adversarial spec review + buildability review of the execution section).
- [ ] M1 — spike: host feasibility.
- [ ] M2 — prompt profiles on the CLI route.
- [ ] M3 — the host route.
- [ ] M4 — doctrine, acceptance, ship.
- [ ] Whole-branch review (review-code, high).
- [ ] Outcomes & Retrospective.

## Terms of art

- **Agent host** (or *host*): a headless `claude` process the kernel starts the first
  time a call takes the host route. It loads only the host plugin. Its main loop never
  runs a model turn. It holds the model calls and subagents the kernel sends it, and
  dies with the kernel.
- **Host plugin**: a Claude Code plugin shipped in this repo at `host-plugin/`, loaded into
  the host through the SDK's `plugins` option (`--plugin-dir`). Its *mod* does the work.
  It is not the ptc plugin itself: loading ptc into the host would start the ptc MCP server
  inside it.
- **Mod** / **function hooks**: a Claude Code plugin module (`hooks/hooks.json` `modules` →
  a `.tsx` file) that registers `on(event, …)` handlers and calls the engine's `$` API. It
  is enabled by `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1`. The API's types are laid by the
  engine into a plugin's gitignored `.claude-plugin/types/` when the plugin is loaded from
  `--plugin-dir`. Worked examples: the doperpowers repo's `hooks/mods/` and `sminos/hooks/mods/`
  (`/Users/new/Developer/GitHub/doperpowers`). Authoring guidance: the `plugin-authoring`
  skill.
- **Channel**: HTTP/1.1 over a Unix socket that the kernel serves. The mod long-polls it
  for jobs and posts results back.
- **Job**: one unit of work sent to the host. `complete` is one model call; `agent` is one
  lean subagent run.
- **Lean agent type**: an agent definition the mod registers with `$.agent.register`, built
  from a profile: its own short system prompt, an exact tool list, and `omitClaudeMd: true`.
- **Profile**: what a child's prompt is made of. The axes are system, tools, memory and
  settings; the presets are `bare`, `reader`, `worker` and `full`. Defined under "Profiles"
  below.
- **Route** (`via=`): which engine runs a call. `"host"` sends it to the agent host, `"cli"`
  runs today's process per child, and `"auto"` picks by the rule under "Routes".

## Measured facts this design rests on

All measured 2026-10-20 on the MacBook. CLI 2.1.305, model haiku, the reply "OK". Token
counts are the first request's prompt (`input + cache_creation + cache_read`).

| child | prompt tokens | process |
|---|---|---|
| CLI, `--bare`, own system prompt, no tools | **52** | 289 MB max RSS, 1.79 s |
| CLI, settings off (`setting_sources=[]`), own system prompt, no tools — today's `llm()` | 379 | 366 MB, 1.70 s |
| CLI, same, `--tools Read,Grep,Glob` | 3,023 | — |
| CLI, Claude Code's default system prompt, no tools | 3,085 | — |
| CLI, default system prompt + default tools | 20,312 | — |
| CLI, `--setting-sources user` (global CLAUDE.md, output style, user plugins) | 7,952 | the to-human output style's mod rewrote the reply itself |
| in-process subagent, lean type, no tools, `omitClaudeMd` | **744** | none (inside its parent) |
| in-process subagent, lean type, Read/Grep/Glob | **2,583** | none |
| in-process `general-purpose` subagent (median of 252 on this machine) | 26,158 | none |
| in-process `Explore` subagent (median of 5) | 16,236 | none |

What the table shows, and what the design takes from it:

- Tool definitions are the cost. The default tool set adds about 17 k tokens, while
  Claude Code's whole default system prompt adds only about 2.7 k over a one-line one. So
  profiles choose the tool list precisely and do not try to pick sections of Claude Code's
  system prompt. The owner chose native options over section picking.
- `--exclude-dynamic-system-prompt-sections` moves the cwd, env and git sections into the
  first user message and saves nothing (3,083). It is not a profile option.
- `--bare` normally authenticates only with `ANTHROPIC_API_KEY` or an `apiKeyHelper`
  (CLI help). On this machine it was served through the local gateway
  (`ANTHROPIC_BASE_URL=http://127.0.0.1:8641` with the gateway's `ANTHROPIC_AUTH_TOKEN`
  marker in the environment), billed to a subscription account: the gateway log reads
  status 200, outcome `served`. Without credentials in the environment `--bare` cannot
  authenticate.
- Two ways of getting only CLAUDE.md both failed. `--bare --add-dir ~/.claude` did not
  load `~/.claude/CLAUDE.md` (52 tokens), and `omitClaudeMd` on a main-thread `--agent` did
  not strip anything (8,019). The memory axis therefore injects the file's text itself.
- A lean in-process subagent costs about what a CLI child with the same tools costs, and
  no process. That is the whole case for the host.

The engine's mod API (2.1.305) has what the host needs. Signatures are in any laid
`.claude-plugin/types/claude-code/index.d.ts`, for example
`/Users/new/Developer/GitHub/doperpowers/.claude-plugin/types/claude-code/index.d.ts`:

- `$.agent.register(spec)` defines an agent type for the session: `prompt`, `tools`,
  `omitClaudeMd`, `model`, `effort`, `permissionMode`, `maxTurns`, and the rest of an agent
  definition.
- `$.agent.spawn({ subagentType, prompt, description, model? })` starts a subagent from
  code, with no model turn deciding to, in the background. Its answer is that subagent's
  `turn.complete` (`e.agentId`, `e.answer`, `e.reason`, `e.usage`).
- `$.model.complete({ model, system?, prompt, maxTokens?, effort?, timeoutMs? }, { signal })`
  makes one tool-less call through the session's own API client, after the CLI's identity
  block. The prompt cache can be marked per text block. It has no structured-output
  option. `maxTokens` defaults to 1024 and is capped at 64,000.
- `$.http.fetch(url, { method, body, socketPath })` talks HTTP over a Unix socket. Each
  call is bounded at 30 s, and a body can be at most 4,194,304 characters (sminos measured
  this on 2.1.300: `sminos/hooks/mods/bridge-stream.tsx`).
- `$.env.get(name)` reads an environment variable. The name must be a string literal.
- `$.fs.read` / `$.fs.write` read and write files; one read or write is capped at 4 MiB.

## Design

### Routes

`llm()`, `agent.run()` and `agent.spawn()` gain `via=` (`"auto"` by default). The CLI
route is today's path: one process per child, now shaped by a profile. The host route
sends a job to the agent host.

`"auto"` resolves per call:

- `llm()` and `agent.run()` go to the **host** when the host is enabled and healthy and the
  call asks for nothing only the CLI route has. Otherwise they go to the CLI.
- `agent.spawn()`, `agent.fork()` and `agent.resume()` always go to the **CLI**. A spawn
  handle's purpose is `send()` follow-ups on a live conversation, which host agents do not
  offer (non-goal below). Routing `spawn` to the host under `auto` would turn a later
  `send()` into an error the caller did not choose.
- The CLI route is required for: `provider="codex"`; a profile whose system is
  `"claude_code"`; `settings` other than `"none"`; and `json_schema=` when M1's verdict says
  host schema calls are not reliable enough (see "Structured output").

So `agent.run()` with its default profile (`full`, whose system is `"claude_code"`) stays on
the CLI route, exactly as today; a `reader` or custom-system run takes the host. A type
whose tools name `Agent` or any `mcp__*` tool is CLI-only too: the host carries no MCP
servers, and a host subagent that could spawn subagents would bypass the `PTC_MAX_DEPTH`
brake.

`via="host"` with a call the host cannot serve raises `ValueError` naming the reason.
`via="cli"` always works.

Why two routes and not host-only: the CLI route keeps everything the host lacks
(conversations, native structured output, codex, per-child isolation, Claude Code's own
system prompt). Rewriting those inside the host would rebuild the CLI. Why not CLI-only
with a higher cap: each slot is a 300–450 MB process, and that is the problem this
initiative exists to remove.

### The host's lifecycle

- **One per kernel**, started lazily on the first host-route call. Calls that arrive while
  it starts wait on the same start.
- **Started through `ClaudeSDKClient`**, so its CLI is the SDK's bundled one, the same
  version the CLI route runs. The options:
  - `setting_sources=[]`, so the host loads no user settings, plugins, hooks, MCP servers
    or CLAUDE.md. This is the isolation `claude_backend._sdk_options` already gives
    children, for the reasons given there.
  - no `mcp_servers`.
  - `plugins=[{"type": "local", "path": <plugin root>/host-plugin}]`. The plugin root is
    `dirname(dirname($PTC_LAUNCHER))`, the path `bin/ptc-launch` exports.
  - `cwd` = the kernel's working directory.
  - `env`: `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1`, `PTC_HOST_SOCKET=<channel path>`,
    `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS=<host cap + 8>`, plus what `child_ptc_env`
    already forwards.

  Whether `connect()` with no first query is enough for the mod to start, and which tool
  setting the main loop needs so that subagent types still get their tools, is M1's to
  settle (Q1, Q2). The executor records the answer and the launch follows it.
- **Readiness.** At `session.start` the mod posts `/hello`. The kernel waits up to
  `PTC_HOST_START_S` (30 s) for it; if none comes, the start has failed.
- **Health.** The host is *healthy* from `/hello` until its process exits. Exit of any
  kind (crash, kill, OOM) fails every in-flight job at once with
  `AgentFailed("agent host exited (code …/signal …)")`. The next host-route call starts a
  fresh host.
- **Crash loop.** If three host starts fail or three hosts exit within 5 minutes,
  `"auto"` sends everything to the CLI for the rest of the kernel's life, with one warning
  in the cell output that names `PTC_HOST`. `via="host"` keeps raising. The reason: the
  host rides the engine's mod API, which can change under an engine update, and when it
  breaks, ptc must keep working on the route it always had.
- **Teardown.** Kernel exit, idle reap and restart close the host with its children,
  through the same release path that reaps CLI children today
  (`bootstrap._release_backends`). Acceptance 5 checks that no host survives its kernel.
- **Off switch.** `PTC_HOST=off` disables the route. `"auto"` then always means the CLI.

### The channel

The kernel serves HTTP/1.1 on a Unix socket under its kernel dir. Use the symlinked short
path the peek socket uses for the 104-byte `sun_path` cap (`runtime/peek.py`
`install_peek`, `peek_client._connect_path`). The mod reads the path with
`$.env.get("PTC_HOST_SOCKET")`. Every body is JSON. The protocol is version 1.

| request | body | response |
|---|---|---|
| `POST /hello` | `{"protocol": 1, "engine": "<cli version>", "plugin": "<host-plugin version>"}` | `200 {"ok": true}`; `409 {"ok": false, "reason": "protocol"}` on a mismatch, which fails the start |
| `GET /jobs?wait=<ms>` | — | `200 {"jobs": [Job…], "cancel": ["<id>"…]}`. The kernel holds the request up to `wait` (the mod sends 25,000, under fetch's 30 s bound) and answers at once when a job or cancel is queued. |
| `POST /results/<id>` | `Result` | `200 {"ok": true}`; `404` for an id the kernel no longer waits on (cancelled or timed out); the mod drops it |

```jsonc
// Job, kind "complete"
{"id": "j-<uuid>", "kind": "complete", "model": "haiku", "system": "…", "prompt": "…",
 "promptFile": null, "maxTokens": 4096, "effort": null, "timeoutMs": 300000}
// Job, kind "agent"
{"id": "j-<uuid>", "kind": "agent",
 "type": {"prompt": "…", "tools": ["Read", "Grep", "Glob"], "model": "haiku", "effort": null,
          "permissionMode": "bypassPermissions", "maxTurns": null},
 "prompt": "…", "promptFile": null, "description": "ptc agent", "timeoutMs": 600000}
// Result
{"ok": true, "text": "…", "textFile": null, "usage": {…}, "agentId": "…"}
{"ok": false, "reason": "api-error|refusal|aborted|error|denied|timeout", "detail": "…", "status": 429}
```

- **Large payloads.** A prompt over 3,500,000 characters goes in a file:
  `<kernel_dir>/host/in/<id>.txt`, named by `promptFile`, read by the mod with
  `$.fs.read`. A prompt over 4 MiB is refused in the kernel with `ValueError` before it
  is queued, because that is `$.fs.read`'s cap. A reply over the same size comes back the
  same way, as `textFile` under `<kernel_dir>/host/out/`. The kernel reads the file and
  deletes it. The threshold sits under the fetch body cap with room for the JSON around it.
- **Dispatch in the mod.** One poll loop. Each job is started without waiting for it to
  finish, and its result is posted when it settles.
  - `complete` runs `$.model.complete` with an `AbortController` per job.
  - `agent` makes sure the job's lean type is registered (below), calls `$.agent.spawn`,
    and keeps `agentId → job id`. A `turn.complete` hook carrying that `agentId` posts the
    result: `reason: "answer"` → `ok: true` with `e.answer`; any other reason → `ok: false`
    with that reason. A spawn that comes back `{ deny }` posts `reason: "denied"` with the
    text.
  - A `cancel` aborts a `complete`'s controller. For an agent it does whatever M1 found
    stops a subagent (Q5). If nothing does, the kernel has already failed that job, the
    subagent runs out, and its result gets a 404.
- **Why long-poll on a kernel-served socket.** The mod API's outbound channel is
  `$.http.fetch`, and `$.process.spawn` writes a child's stdin only once. A socket the
  kernel serves keeps the host at exactly one process. Jobs arrive at the kernel's pace,
  so one poll loop is enough. The alternative was a bridge child process, as sminos runs.
  It lost: sminos needs one because many outside clients reach one session, while here
  there is one client, the kernel, and the child would add a process and a second hop.

### Lean agent types

For each distinct `type` in an `agent` job, the mod registers one agent type, named
`lean-<first 10 hex of sha1(canonical JSON of type)>`. Registration happens once and
later jobs reuse it. Every type gets `omitClaudeMd: true` and is kept out of any listing
with an `agent.offer` hook answering `{ isOffered: false }`. Its `prompt` is the profile's
system text with the memory text appended (see "Profiles"). Its `tools` are the profile's
tool list. The measured floor is 744 tokens with no tools and 2,583 with Read/Grep/Glob.

### Concurrency

Host jobs run under their own kernel-wide semaphore, `PTC_HOST_MAX_CONCURRENCY`. Its
default is the largest width at which M1's ramp held its promote criteria; until M1
records one it is 64. CLI children keep `PTC_MAX_CONCURRENCY`. The two are separate
because a host slot is one coroutine and one API stream inside an existing process, while
a CLI slot is a 300–450 MB process. One shared cap would either starve the host or let
the CLI route exhaust RAM. The host's own `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` is set
above the semaphore, so the kernel's semaphore is the only cap that binds. A rate limit
comes back as `api-error` with its status, and the kernel raises `AgentFailed` just as the
CLI route raises it for a failed turn today (the `llm()` contract in the kernel spec).

### Profiles

A profile is what a child's prompt is made of. The axes apply to both routes. A route
that cannot express a value falls back as described under "Routes".

| axis | values | CLI route | host route |
|---|---|---|---|
| `system` | a string; `"claude_code"`; `("claude_code", append)` | `system_prompt` string, or the SDK preset `{"type": "preset", "preset": "claude_code", "append": …}` | the type's `prompt` (string only) |
| `tools` | `[]`; a list of built-in names; `"default"` | `tools=` | the type's `tools` (`"default"` → CLI route) |
| `memory` | `None`; `"global"`; `"project"`; `"both"` | file text appended to the system prompt | file text appended to the type's `prompt` |
| `settings` | `"none"`; `"user"`; `"all"` | `setting_sources=[]` (+ `--bare`, below); `["user"]`; `["user", "project", "local"]` | `"none"` only |

- **Presets.** `ptc.runtime.profiles.PRESETS` holds four, and explicit keyword arguments
  override a preset's fields (`agent.run(t, profile="reader", tools=["Read"])`):
  - `bare`: system `"Answer directly and concisely. No preamble."` (today's `llm()`
    default), `tools=[]`, no memory, settings `"none"`. This is `llm()`'s profile.
  - `reader`: system `READER_SYSTEM`, `tools=["Read", "Grep", "Glob"]`, no memory,
    settings `"none"`. `READER_SYSTEM` is a short read-only investigator prompt: use the
    tools given, cite absolute paths, end with the findings. The executor writes it and
    keeps it under 120 words.
  - `worker`: system `"claude_code"`, tools `["Read", "Grep", "Glob", "Bash", "Edit", "Write"]`,
    memory `"global"`, settings `"none"`. Its system means it runs on the CLI route.
  - `full`: system `"claude_code"`, tools `"default"`, no memory, settings `"none"`. This
    is exactly today's `agent.run` child, and `agent.*`'s default profile, so existing
    callers see no change.
- **Memory text.** Each file appears in the same framing Claude Code uses for its own:
  `Contents of <absolute path> (<user's private global instructions for all projects |
  project instructions>):\n\n<text>`.
  - `"global"` is `~/.claude/CLAUDE.md`.
  - `"project"` is every `CLAUDE.md` from the child's working directory up to its git
    root, in Claude Code's order: root first.
  - A missing file contributes nothing.
- **The `--bare` rule.** The CLI route with settings `"none"` adds `--bare` (through
  `extra_args`) exactly when the child's environment carries `ANTHROPIC_API_KEY` or
  `ANTHROPIC_AUTH_TOKEN`. Measured: it takes `llm()` from 379 to 52 tokens on this
  machine. Without credentials in the environment it would fail authentication, so the
  route keeps the 379-token form. `--bare` also skips hooks, LSP and plugin sync, which a
  settings-`"none"` child does not load anyway; it keeps `--mcp-config`, so `agent.*`
  children keep the ptc MCP server.
- **`settings="user"` is allowed but in no preset.** It brings the user's hooks, plugins,
  MCP servers and output style. The measurement above shows an output style's mod
  rewriting a child's reply. The SKILL doctrine says so where it lists the axis.

### Structured output

`$.model.complete` has no structured output. On the host route, `llm(json_schema=…)`:

1. appends to the system prompt `Reply with one JSON value that validates against this
   JSON Schema, and nothing else:\n<schema>`;
2. parses the reply, after stripping one surrounding code fence;
3. validates it with `jsonschema` (a new dependency);
4. on failure retries once, with the validation error added to the prompt;
5. then raises `AgentFailed` carrying the last reply.

The CLI route keeps native `--json-schema`. M1's Q6 decides whether `"auto"` sends schema
calls to the host: the host route serves them if no more than 1 in 100 needs the retry
and none fails it; otherwise `"auto"` sends schema calls to the CLI. The executor records
the verdict and sets the rule.

### Results, audit, registry

- Host results are the existing `AgentResult`, with two fields added at the end, both
  defaulted so existing constructors keep working: `via: str = "cli"` and
  `agent_id: str | None = None`. `session_id` on a host result is the host's session id.
- Host jobs append to the audit trail as today (`"llm"` / `"agent"` records), with
  `via="host"`.
- Host agent runs appear in `agent.list()` with provider `"claude-host"`, the subagent id,
  and their transcript path: `~/.claude/projects/<cwd slug>/<host session id>/subagents/agent-<agentId>.jsonl`.
  Reading that transcript is how acceptance 2 checks a run's prompt floor.

### Non-goals

- `send()`, `resume()` and `fork()` on host agents. These are conversations; the CLI route
  serves them.
- Loading the secrets guard (the devcloud plugin) into the host. Children today load no
  plugins either. This is a follow-up once the host is in use.
- A raw HTTP model loop through the gateway. This was the alternative before
  `$.agent.spawn` was known. It would reimplement tools, permissions and compaction
  outside Claude Code, run without hooks, and depend on an unverified question: whether a
  hand-built request with the subscription OAuth credential is accepted.
- The Message Batches API, by the owner's ruling: requests go through the gateway.
- The API-key route. Its `sdk-cli` header requirement is for a later investigation, and
  the owner is to be asked again before it is built.
- The Workflow tool's concurrency setting (`CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS`),
  which the owner decides separately. The host does not use the Workflow tool.

## Acceptance

Run from a session with ptc at the shipped version. Each item is observable from a ptc
cell or a shell.

1. **A 64-wide `llm()` fan-out runs on one host.**
   ```python
   import time
   t = time.time()
   rs = await asyncio.gather(*[llm(f"Reply with exactly the number {i}.") for i in range(64)])
   print(sum(r.strip() == str(i) for i, r in enumerate(rs)), round(time.time() - t, 1))
   ```
   This prints `64 <seconds>`. While it runs,
   `pgrep -fl "claude_agent_sdk/_bundled/claude" | grep -c -- "--plugin-dir.*host-plugin"`
   is `1`, and no other bundled CLI belongs to this kernel. The same fan-out with
   `via="cli"` takes longer at its cap of 8: report both times.
2. **32 reader agents run on the host.** `agent.run(f"How many lines does {p} have? Reply
   with the number only.", profile="reader")` over 32 files whose line counts the test
   knows. All 32 are right, and the first request in each run's transcript has a prompt
   of at most 3,000 tokens.
3. **CLI-route floors.** `llm("Reply with exactly: OK", via="cli")` costs at most 100
   prompt tokens with credentials in the environment, and at most 400 with
   `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` removed from the child's environment.
   `agent.run(..., profile="reader", via="cli")` costs at most 3,100.
4. **A host crash fails its jobs and recovers.** `kill -9` the host during a 32-wide
   fan-out. Every in-flight call raises `AgentFailed` whose message names the host's
   exit. A following `llm()` succeeds on a new host pid.
5. **No host outlives its kernel.** After `ptc restart`, the old host pid is gone
   (`ps -p <pid>` prints nothing).
6. **Fallback.** With `PTC_HOST=off`, and separately with the host plugin unloadable
   (pointed at an empty directory in the test), `llm()` still answers on the CLI route.
   The unloadable case prints one warning in the cell output.
7. **The suites pass**: `uv run pytest test/unit test/integration -q` and the live suites
   named in Concrete Steps.

## Execution

### Constraints that bind every milestone

- `main` must carry `claude-agent-sdk==0.2.169` (bundled CLI 2.1.305) before M1 starts.
  It ships as ptc v0.5.5 in a separate change; merge `main` into this branch first. The
  mod API used here is that engine's. Read signatures from the laid types. For laying them
  headless and type-checking a mod, use the approach of doperpowers' `tests/mods/typecheck.sh`
  (`--lay`). Commit no laid types: `host-plugin/.claude-plugin/types/` is gitignored.
- The host loads nothing of the user's (`setting_sources=[]`, no MCP servers) and is one
  process. No host job may start a process of its own, except the tools a subagent runs
  (a `Bash` call is its own process, as in any session).
- Python 3.12 under the repo's `uv` / `pytest` toolchain. The mod is TypeScript under the
  `plugin-authoring` skill's guidance.
- Live tests and spikes use haiku. A programmatic fan-out of more than about 50 model calls
  outside the existing suites is announced to the dispatching session before it runs: the
  owner reads what fan-outs a session launches.
- The trust model (README, "Trust model — read this") holds unchanged for host agents. They
  run with the permission mode their options give, `bypassPermissions` by default, as
  children do today.

### Plan of Work

### M1 — spike: host feasibility

Prototyping. The deliverable is knowledge, not shipped code. Build a minimal host plugin
and a Python driver under `test/spikes/s9_agent_host/`. The driver starts a host as
"The host's lifecycle" describes and serves a stub of the channel. The mod polls it and
runs the jobs it is given. Answer these questions and record each answer with its
evidence in Surprises & Discoveries:

- **Q1.** Does the mod load and `session.start` fire in an SDK-started headless CLI with
  `setting_sources=[]`, the `plugins` option and the function-hooks variable? Does it need
  a first query? If so, what is the smallest one?
- **Q2.** Does `$.agent.register` plus `$.agent.spawn` run a lean subagent to its
  `turn.complete` with no main-loop turn? Does a type's tool list hold however the host's
  main-loop tools are set, and which setting is needed? What is the prompt floor of a
  `reader`-shaped type, read from its transcript?
- **Q3.** Does `$.model.complete` run N calls concurrently?
- **Q4.** A ramp of 8, 32, 128 and 256 concurrent jobs, half `complete` and half
  reader-type agents that each do one `Read`: wall time, failures by reason, host peak RSS
  and CPU, per-job latency p50/p95. Does `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` bind as
  set?
- **Q5.** Can the mod stop a running subagent (any `$` call, or an engine path it can
  reach)? Does aborting `$.model.complete`'s signal resolve `aborted`?
- **Q6.** Over 100 host `complete` calls with a small JSON schema, using the prompt +
  validate method under "Structured output": how many need the retry, and how many fail it?
- **Q7.** In this engine version, what are `$.http.fetch`'s body cap and the behavior of a
  held request near 30 s?
- **Q8.** What does the host's death look like to the driver (the SDK client's error, the
  socket), and how long does a fresh start take?

**Promote** when Q1–Q3 work and the 128-job step finishes with at least 99 % success and
the host's RSS at most 2 GB. M3 then builds the route, with `PTC_HOST_MAX_CONCURRENCY`'s
default set to the widest step that met those numbers.

**Partial**: if `$.model.complete` works but spawned agents do not, the host route
serves `llm()` only. Record it, and M3 drops the `agent` job kind.

**Discard**: if mods do not run in a headless host at all, record it and stop with
`BLOCKED`. The design's alternative (non-goals, raw HTTP loop) goes back to the owner.

This milestone touches `test/spikes/s9_agent_host/` only. The prototype is deleted or kept
there as a spike, as earlier spikes were. M3 writes the real code from scratch, guided by
the verdict. The spike's model-call budget is about 600 haiku calls (Q4: 424; Q6: 100;
the rest small), announced before it runs per the constraints.

### M2 — prompt profiles on the CLI route

At the end of M2, every CLI-route child is built from a profile. `llm()` runs at the
`bare` floor (52 tokens with credentials in the environment), and `agent.*` callers who
pass nothing see today's behavior.

It touches:
- `src/ptc/runtime/profiles.py` (new): `Profile`, `PRESETS`, `resolve`, `memory_text`
  (Interfaces).
- `src/ptc/runtime/agents.py`: `AgentOpts` gains `profile`. `_Agent._opts` resolves
  `profile=` together with the existing `system=` and `tools=` keywords, with the
  keywords overriding.
- `src/ptc/runtime/claude_backend.py`: `_sdk_options` maps a resolved profile: system or
  preset, tools, memory appended, setting sources, `--bare` by the rule.
- `src/ptc/runtime/_llm.py`: `llm()` takes `profile="bare"`.

Decisions that belong here:
- `bare_llm=True` keeps its structural guarantee: zero tools and no ptc MCP server. A
  profile cannot give an `llm()` call tools. `llm(profile=...)` with tools raises
  `ValueError`.
- The `--bare` decision reads the child's environment as the SDK will pass it: the
  kernel's environment merged with `child_ptc_env`. That merge is the one
  `claude_backend._child_env` documents, so check the same mapping. Never log or audit it:
  the trust note in `_child_env` binds.

- `--bare` sets `CLAUDE_CODE_SIMPLE=1`, which may change more than the prompt. Before
  applying the rule to `agent.*` children, compare a `full` child's tool list on its
  init message with and without `--bare`. If they differ, apply `--bare` only to
  profiles with an explicit tool list (`bare`, `reader`, custom), and record which.

It does not touch the host, the channel, or `via=`; those are M3's. Codex children are
untouched; profiles are Claude-only, and `provider="codex"` with a non-default profile
raises `ValueError`.

It proves acceptance 3. Its tests pin:
- preset field values;
- keyword overrides;
- memory text framing and order;
- the `--bare` rule both ways (credentials in the environment or not);
- `settings` mapping to setting sources.

### M3 — the host route

At the end of M3, `via="host"` and `"auto"` work as "Design" describes. A fan-out of host
jobs runs in one host process per kernel. Crashes, teardown and fallback behave as
acceptance 1, 2, 4, 5 and 6 say.

It touches:
- `src/ptc/runtime/host.py` (new): `AgentHost` (Interfaces), covering the channel server,
  the job table, the host semaphore, start / hello / health / crash loop, and teardown.
- `host-plugin/` (new): `.claude-plugin/plugin.json` (name `ptc-host`), `hooks/hooks.json`
  naming its module, `hooks/mods/host.tsx`, and a `.gitignore` for `.claude-plugin/types/`.
- `src/ptc/runtime/_llm.py` and `agents.py`: `via=` resolution per "Routes". The host path
  submits jobs. `agent.list()` gains host entries.
- `src/ptc/runtime/bootstrap.py`: the host joins the backend release path.
- `src/ptc/runtime/claude_backend.py`: unchanged except where `AgentResult` gains its two
  fields.
- `pyproject.toml` and `uv.lock`: add `jsonschema`.

Decisions that belong here:
- The kernel's channel server runs on the kernel's own asyncio loop (`STATE.loop`).
  Futures are resolved on it. A result posted for an unknown or settled id gets 404 and
  changes nothing.
- A job's `timeoutMs` is the call's remaining `timeout` after its semaphore wait, as
  `guarded` computes it today. On expiry the kernel fails the call, queues a `cancel` for
  the job, and returns its semaphore slot.
- Job ids are `j-<uuid4 hex>`. Type names follow "Lean agent types". Use canonical JSON
  for the hash: sorted keys, no whitespace.
- The mod's own failures (an exception in its dispatch) post `ok: false, reason: "error"`
  with the message, so a mod bug surfaces as a failed call, never as a hang.
- `web.py` composes `web_fetch`'s summarization with `llm()`'s permit and documents
  why that cannot deadlock at the cap. Re-check that argument once `llm()` may hold the
  host semaphore instead, and keep it true on both routes.
- Host-route `AgentResult.cost_usd` is computed from nothing: leave it `None`.
  `num_turns` is `1` for a `complete` and `None` for an agent.

It does not touch the CLI route's process handling, the codex backend, the MCP adapter's
tool surface, or the SKILL text; the SKILL text is M4's.

It proves acceptance 1, 2, 4, 5 and 6. Its tests pin:
- the channel protocol against a fake mod written in Python (hello and protocol mismatch,
  held polls answered at once on a queued job, results, 404 on a settled id, the
  large-payload file path both ways);
- every in-flight job failing on host exit;
- the crash-loop switch to the CLI;
- cancel on timeout;
- `"auto"` resolution for each case in "Routes";
- schema validation and retry.

Live tests drive a real host for acceptance 1, 2, 4 and 5.

### M4 — doctrine, acceptance, ship

At the end of M4, ptc's documentation teaches the routes and profiles, the version is
bumped and shipped, and acceptance has been run as written.

It touches:
- `skills/ptc/SKILL.md`: the "Fan-out rules" and "Sub-LM map-reduce" sections, rewritten
  around routes and profiles, with the measured floors table (this spec's, re-measured
  where M2 and M3 changed a number). The rule "every child is a whole `claude` process"
  becomes a statement about the CLI route only.
- `README.md` "Configuration": `PTC_HOST`, `PTC_HOST_MAX_CONCURRENCY`, `PTC_HOST_START_S`.
- The kernel spec's "Configuration reference" table and a pointer from its "Agents" and
  "Sub-LM calls" sections to this spec.
- `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`: the next minor
  version (0.6.0), with a ship entry in this spec's Decision Log.

The SKILL text is behavior-shaping. Keep its existing voice and length discipline. Each
new line must change what a reading agent does; cut what it would do anyway.

It proves acceptance 1–7, run as written, with the outputs recorded in Surprises &
Discoveries.

### Concrete Steps

All from the worktree root, `/Users/new/Developer/GitHub/ptc-tool/.claude/worktrees/agent-host`
(or wherever the branch is checked out).

```bash
git merge main                                   # before M1: brings claude-agent-sdk 0.2.169
uv sync                                          # the repo's own venv
uv run pytest test/unit -q                       # fast; expect all pass
uv run pytest test/integration -q                # starts real kernels; expect all pass
uv run pytest test/live/test_llm_live.py test/live/test_agents_live.py -q   # real model calls (haiku)
uv run pytest test/live -q -k host               # M3's live host tests, once they exist
```

Spike (M1): `uv run python test/spikes/s9_agent_host/drive.py --ramp 8,32,128,256` prints
one line per step:
`width=<n> ok=<n> fail=<n> wall=<s> rss_peak=<MB> p50=<s> p95=<s>`.

### Interfaces and Dependencies

```python
# src/ptc/runtime/profiles.py
SystemSpec = str | Literal["claude_code"] | tuple[Literal["claude_code"], str]

@dataclass(frozen=True)
class Profile:
    system: SystemSpec
    tools: list[str] | Literal["default"]
    memory: Literal["global", "project", "both"] | None = None
    settings: Literal["none", "user", "all"] = "none"

PRESETS: dict[str, Profile]            # "bare", "reader", "worker", "full"
def resolve(profile: str | Profile | None, *, default: str, **overrides) -> Profile: ...
def memory_text(memory: str | None, cwd: str) -> str: ...      # "" when nothing applies

# src/ptc/runtime/host.py
class HostUnavailable(RuntimeError): ...   # disabled, start failed, or crash-looped

class AgentHost:
    def enabled(self) -> bool: ...          # PTC_HOST != "off" and not crash-looped
    async def ensure_started(self) -> None: ...   # raises HostUnavailable
    async def submit(self, job: dict, timeout: float | None) -> dict: ...  # the Result; raises AgentFailed on host exit or timeout
    async def close(self) -> None: ...

def get_host() -> AgentHost: ...            # the kernel's one instance

# public, in the kernel namespace
async def llm(prompt, *, model="haiku", system=None, json_schema=None, timeout=300.0,
              profile="bare", via="auto", max_tokens=4096, effort=None): ...
# agent.run / agent.spawn gain: profile=None (→ "full"), via="auto"
# AgentResult gains: via: str = "cli", agent_id: str | None = None
```

- The channel protocol and the job and result shapes are under "The channel".
- New dependency: `jsonschema` (structured output on the host route).
- The host plugin is named `ptc-host`. Its module path is `hooks/mods/host.tsx`.
- Environment it reads: `PTC_HOST_SOCKET`.
- Configuration: `PTC_HOST` (`auto` | `off`, default `auto`), `PTC_HOST_MAX_CONCURRENCY`
  (default from M1, initially 64), `PTC_HOST_START_S` (30).

## Surprises & Discoveries

- Observation: Claude Code 2.1.305 was the current CLI, and the SDK pin's bundled CLI was
  2.1.237.
  Evidence: `claude --version` → `2.1.305`; `claude_agent_sdk/_cli_version.py` in
  0.2.142 → `2.1.237`, in 0.2.169 → `2.1.305`.

## Decision Log

- Decision: Verification for this spec. An independent spec review by the
  `doperpowers:adversarial-reviewer` agent (the spec is technical-heavy), and in the same
  round its buildability review of the execution section (four milestones). The finished
  branch gets a review-code whole-branch review at **high** (`doperpowers:reviewer-high`).
  Process lifecycle, a new IPC channel and concurrency caps are where a merely adequate
  review costs most.
  Rationale: The stakes are a runtime every ptc fan-out will take by default. The default
  medium rung is a weaker read of lifecycle and concurrency than this warrants.
  Date/Author: 2026-10-20, doperpowers-15.
- Decision: Execution routing. Several milestones go to one `doperpowers:plan-executor`
  at its pins (opus/high; its task-executor opus/high, task-reviewer sol/xhigh).
  Rationale: execspec step 4.
  Date/Author: 2026-10-20, doperpowers-15.

## Outcomes & Retrospective

Pending — written at finish.
