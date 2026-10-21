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
- [x] (2026-10-20) SDK pin to `claude-agent-sdk==0.2.169` (bundled CLI 2.1.305) shipped on `main` as v0.5.5 (5a6bca7), merged into this branch at 1f50755.
- [x] (2026-10-20) Spec review, round 1: adversarial design review and buildability review, both folded in (Decision Log).
- [x] (2026-10-20) Spec review, round 2 on the round-1 fixes: folded in (Decision Log).
- [x] (2026-10-20) M1 — spike: host feasibility. Verdict: Promote (Decision Log), 823 of 850 haiku calls.
- [x] (2026-10-20) M2 — prompt profiles on the CLI route (3777674, fix 3e554d5; review clean).
- [x] (2026-10-20) M3 — the host route (22e4c1b, 17ddb4e, fix be5003b; review clean). 335 of 400 haiku calls.
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
| in-process subagent, lean type, Read/Grep/Glob | **3,404** (2.1.305, M1; 2,583 on the build first measured: the three tools' schemas grew between builds) | none |
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
  `send()` into an error the caller did not choose. `agent.spawn(via="host")` is allowed
  and single-shot: its handle settles once, and `send()` raises.
- The CLI route is required for: `provider="codex"`; a profile whose system is
  `"claude_code"`; `settings` other than `"none"`; and `json_schema=` when M1's verdict says
  host schema calls are not reliable enough (see "Structured output").

So `agent.run()` with its default profile (`full`, whose tools are `"default"`) stays on
the CLI route, exactly as today; a `reader` or custom-system run takes the host. A type
whose tools name `Agent` or any `mcp__*` tool is CLI-only too: the host carries no MCP
servers, and a host subagent that could spawn subagents would bypass the `PTC_MAX_DEPTH`
brake.

`via="host"` with a call the host cannot serve raises `ValueError` naming the reason.
`via="cli"` always works.

**Option compatibility.** Every option a call already accepts either travels to the host
or sends the call to the CLI route. None is dropped silently, because a dropped `cwd`
reads the wrong checkout and a dropped `output_schema` loses `AgentResult.structured`,
which the kernel spec promises.

| option | host route |
|---|---|
| `model`, `effort`, `max_turns`, `permission_mode` | carried: the lean type's `model`, `effort`, `maxTurns`, `permissionMode` |
| `tools` / profile tools | carried as the type's `tools`; `"default"`, `Agent` or `mcp__*` → CLI |
| `system` / profile system | carried as the type's `prompt` when a string; `"claude_code"` → CLI |
| `cwd` | carried as the spawn's `cwd` (`AgentSpawnInput.cwd`, "the directory the subagent runs in"), if M1's Q2 confirms a spawn honors it; otherwise a `cwd` other than the kernel's → CLI |
| `allowed_tools` | meaningless under `bypassPermissions` (carried as nothing); with any other permission mode → CLI |
| `output_schema` on `agent.*` | → CLI (subagents have no structured output) |
| `json_schema` on `llm()` | "Structured output" below |
| `timeout` | the call's deadline ("Deadlines" below) |
| `provider="codex"` | → CLI |

Under `"auto"`, "→ CLI" sends the call there. Under `via="host"`, it raises `ValueError`
naming the option.

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
- **A cold host.** Under `"auto"` the first host-eligible call starts the host and waits
  for it within its own deadline. A start that fails counts toward the crash loop below,
  and that call goes to the CLI with what remains of its deadline ("Deadlines").
- **Generations and retirement.** Each started host is one *generation*. A generation is
  *retired* when any of these happens:
  - its process exits (crash, kill, OOM);
  - its lease lapses (below);
  - its channel loses a delivery (below);
  - its abandoned jobs fill the host cap ("Concurrency").

  Retiring kills the process if it still runs, and fails every in-flight job of that
  generation at once with `AgentFailed("agent host retired: <cause>")`. The cause is the
  exit code or signal, `lease`, `delivery` or `abandoned`. Nothing is replayed, because an
  agent may already have run tools. The next host-route call starts a fresh generation.
- **Lease.** The mod's `/jobs` poll is its heartbeat. A generation that sends no poll for
  60 s after `/hello` (the hold is 25 s) has lapsed and is retired, even though its CLI
  is alive. This catches a mod whose poll loop died, which process exit alone would miss.
  It matters because `agent.run()`'s `timeout` defaults to `None`, and without the lease
  such a call could wait forever.
- **Crash loop.** If three host starts fail or three generations are retired within
  5 minutes, counting every cause except `abandoned`,
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
| `GET /jobs?wait=<ms>&ack=<seq>` | — | `200 {"seq": <n>, "jobs": [Job…], "cancel": ["<id>"…]}`. The kernel holds the request up to `wait` (the mod sends 25,000, under fetch's 30 s bound) and answers at once when a job or cancel is queued. `seq` numbers each non-empty response; `ack` is the `seq` of the last response the mod received. |
| `POST /results/<id>` | `Result` | `200 {"ok": true}`, or `200 {"ok": true, "late": true}` when the job's caller was already settled (timed out or cancelled): the result is discarded and the job's slot is released all the same; `404` only for an id the kernel never issued or already released |

Every job ends in exactly one result POST. A cancelled `complete` posts `reason:
"aborted"`. A stopped subagent posts its `turn.complete` reason. One that cannot be
stopped posts whatever it ends with. So there is no separate end notice: a job's slot is
released when its result arrives, whatever its caller's state, or when its generation is
retired.

```jsonc
// Job, kind "complete"
{"id": "j-<uuid>", "kind": "complete", "model": "haiku", "system": "…", "prompt": "…",
 "promptFile": null, "systemFile": null, "maxTokens": 4096, "effort": null, "timeoutMs": 300000}
// Job, kind "agent"
{"id": "j-<uuid>", "kind": "agent",
 "type": {"prompt": "…", "tools": ["Read", "Grep", "Glob"], "model": "haiku", "effort": null,
          "permissionMode": "bypassPermissions", "maxTurns": 50},
 "prompt": "…", "promptFile": null, "cwd": "/abs/dir", "description": "ptc agent",
 "timeoutMs": 600000}   // timeoutMs null when the call has no deadline
// Result
{"ok": true, "text": "…", "textFile": null, "usage": {…}, "agentId": "…"}
{"ok": false, "reason": "api-error|refusal|aborted|error|denied|timeout|too-large", "detail": "…", "status": 429}
```

- **Delivery.** A response, with every job and cancel in it, counts as delivered when
  the next poll acks its `seq`. If a poll arrives whose `ack` is older than the last
  response sent, that response was lost. The kernel
  then retires the generation, cause `delivery`. It does not redeliver: a lost response
  is rare on a local socket, and redelivery would need deduplication in the mod to rule
  out running an agent twice. A result POST that fails (an exception, or any status other
  than 200/404) is retried by the mod three times within 10 s. If all three fail, the mod
  stops polling, and the lease retires the generation. Every transport failure thus ends
  in a bounded retirement, never in a stranded call.
- **Sizes.** Size the serialized envelope, never the raw text. JSON escaping can more than
  double a text: 2.2 M newlines encode to 4.4 M characters.
  - **Poll responses.** The kernel builds each one so that its JSON stays at or under
    4,000,000 characters, under the 4,194,304-character fetch cap. Jobs that do not fit
    wait for the next poll.
  - **Large job fields.** A job whose own JSON would pass 3,500,000 characters has its
    `prompt` and `system` moved to `<kernel_dir>/host/in/<id>.prompt.txt` and
    `.system.txt`, named by `promptFile` / `systemFile`. The mod reads them with
    `$.fs.read`.
  - **Over the read cap.** Each of those fields is at most 4 MiB in UTF-8 bytes (the
    read cap). A larger one is refused in the kernel with `ValueError` before it is
    queued.
  - **Results.** The mod measures its result envelope the same way. A result over
    3,500,000 characters writes its `text` to `<kernel_dir>/host/out/<id>.txt`, named by
    `textFile`; the kernel reads the file and deletes it. A text over 4 MiB in UTF-8
    bytes cannot be written by `$.fs.write`. The mod then posts
    `ok: false, reason: "too-large"` with the size, and the kernel raises `AgentFailed`.
- **Dispatch in the mod.** One poll loop. Each job is started without waiting for it to
  finish, and its result is posted when it settles.
  - `complete` runs `$.model.complete` with an `AbortController` per job.
  - `agent` makes sure the job's lean type is registered (below), calls `$.agent.spawn`,
    and keeps `agentId → job id`. A `turn.complete` hook carrying that `agentId` posts the
    result: `reason: "answer"` → `ok: true` with `e.answer`; any other reason → `ok: false`
    with that reason. A spawn that comes back `{ deny }` posts `reason: "denied"` with the
    text.
  - A `cancel` aborts a `complete`'s controller. For an agent it does whatever M1 found
    stops a subagent (Q5). If nothing does, the subagent runs out, bounded by its
    `maxTurns` ("Concurrency"). Either way the job's one result POST, `late` by then,
    releases its slot.
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
tool list. The measured floor on 2.1.305 (M1) is 853 tokens with `READER_SYSTEM` and no
tools, and 3,404 with Read/Grep/Glob outside git (3,604 in a git cwd with the host's
recipe).

### Concurrency

Host jobs run under their own kernel-wide semaphore, `PTC_HOST_MAX_CONCURRENCY`. Its
default is **256**, the widest width M1's ramp measured, at which it held its promote
criteria: steps of 8, 32, 128 and 256 jobs all succeeded 100 %, and at 256 one host
peaked at 581 MB with p95 latency 7.45 s (Surprises & Discoveries, Q4). CLI children keep `PTC_MAX_CONCURRENCY`. The two are separate
because a host slot is one coroutine and one API stream inside an existing process, while
a CLI slot is a 300–450 MB process. One shared cap would either starve the host or let
the CLI route exhaust RAM. The host's own `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` is set
above the semaphore, so the kernel's semaphore is the only cap that binds.

**Settlement is separate from capacity.** When a call's deadline passes or its caller
cancels it, the caller is settled at once (`TimeoutError` or `CancelledError`, as on the
CLI route), and the kernel queues a `cancel`. The job's slot stays held until the job
has actually ended in the host: until its result arrives (`late`), or until its
generation is retired. Releasing is idempotent: the first of those two releases the
slot, and the other changes nothing. Releasing the
slot at the caller's timeout would admit replacements while the abandoned agents still
run, and the engine's own subagent cap would then bind and stall later work.

- An abandoned agent's work is always bounded: a host agent job's `maxTurns` defaults to
  50 when the caller gives none.
- If abandoned jobs come to hold every slot, the generation is retired with cause
  `abandoned`, which kills them. Live jobs of that generation fail with that cause too.
  That is the price of a host whose agents cannot be stopped one by one, and it only
  happens once the cap is entirely abandoned.
- If M1's Q5 finds no way to stop a subagent, this rule plus the `maxTurns` bound is how
  host agents stay bounded. It is recorded with the verdict.

A rate limit
comes back as `api-error` with its status, and the kernel raises `AgentFailed` just as the
CLI route raises it for a failed turn today (the `llm()` contract in the kernel spec).

### Deadlines

A call has one deadline, fixed when it is entered: `deadline = monotonic() + timeout`
(`None` means none). Everything the call waits on spends that same budget, and no later
step gets a fresh one. That covers the host's start, the host semaphore, the job in the
host, and a fallback to the CLI. It is the existing wall-clock contract of `timeout`
("a deadline covering the queue wait too", `_llm.py`).

- On the host route, `AgentHost.submit(job, deadline)` owns admission. It waits for the
  host's start and then for a host-semaphore permit, each bounded by the deadline. It
  sets the job's `timeoutMs` to the time left at submission. When the deadline passes at
  any stage, it raises `TimeoutError`, which is what `guarded`'s `asyncio.wait_for` raises
  on the CLI route, so callers catch one type on both routes. The shared CLI semaphore is
  not taken.
- `timeout` is validated once, at entry, by `guarded`'s rule: `None` means no deadline,
  `0` is a deadline already passed, and a negative value is a `ValueError`. Nothing
  derived later is passed back through that check.
- When `"auto"` falls back to the CLI because the host is unavailable (start failed or
  crash-looped), the CLI attempt runs under `guarded(shared_semaphore(), make, remaining)`
  with `remaining = max(0.0, deadline - monotonic())`, or `None` when the call has no
  deadline. A budget that is already spent therefore fails with `TimeoutError`, never
  with `guarded`'s negative-argument `ValueError`.
- `guarded` itself is unchanged. It stays the CLI route's admission, and the call site
  computes its argument from the deadline.

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
    tools given, and, only when the task names no reply format of its own, cite absolute
    paths and end with the findings. (M3: a citation instruction stated unconditionally
    outweighed a task's "number only" in 7 of 32 haiku readers.) The executor writes it and
    keeps it under 120 words.
  - `worker`: system `"claude_code"`, tools `["Read", "Grep", "Glob", "Bash", "Edit", "Write"]`,
    memory `"global"`, settings `"none"`. Its system means it runs on the CLI route.
  - `full`: system `""` (the empty system prompt the SDK sends today, `--system-prompt ""`),
    tools `"default"`, no memory, settings `"none"`. This is exactly today's `agent.run` child, and `agent.*`'s default profile, so existing
    callers see no change.
- **Memory text.** Each file appears in the same framing Claude Code uses for its own:
  `Contents of <absolute path> (<user's private global instructions for all projects |
  project instructions>):\n\n<text>`.
  - `"global"` is `~/.claude/CLAUDE.md`.
  - `"project"` is every `CLAUDE.md` from the child's working directory up to its git
    root, in Claude Code's order: root first.
  - A missing file contributes nothing.
- **The `--bare` rule.** The CLI route adds `--bare` (through `extra_args`) exactly when
  the profile's settings are `"none"`, its system is a plain string, its tools are `[]`,
  and the child's environment carries a non-empty `ANTHROPIC_API_KEY` or
  `ANTHROPIC_AUTH_TOKEN`. (M2: on 2.1.305 `--bare` cuts any tool list to Bash, Edit and
  Read and replaces Claude Code's system prompt with a stub, so a profile with tools
  never takes it.) Measured: it takes `llm()` from 379 to 52 tokens on this
  machine. Without credentials in the environment it would fail authentication, so the
  route keeps the 379-token form. `--bare` also skips hooks, LSP and plugin sync, which a
  settings-`"none"` child does not load anyway.
- **The ptc MCP server.** An `agent.*` CLI child gets the ptc MCP server only when its
  tools are `"default"` or its explicit list names it, as the whole server (`mcp__ptc`)
  or one of its tools (`mcp__ptc__exec`). `--tools` does not filter MCP tools, so
  attaching it to an explicit list would add its tools to every reader. A child that
  needs its own kernel names `mcp__ptc`. `llm()` never gets it.
- **Git context.** Every CLI child whose profile is not `full` runs with
  `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1`, as the host process does, so both routes give
  a profile the same prompt. `full` stays exactly today's child.
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
   with the number only.", profile="reader", model="haiku")` over 32 files whose line counts the test
   knows, in a git cwd. All 32 are right, and the first request in each run's transcript
   has a prompt of at most 4,200 tokens.
3. **CLI-route floors**, in a non-git cwd, with `PTC_LAUNCHER` set as in a real kernel. `llm("Reply with exactly: OK",
   via="cli")` costs at most 100 prompt tokens with credentials in the environment, and
   at most 450 without `--bare` (measured with the credential present and `--bare`
   withheld, the same prompt, since this machine cannot sign in with the credentials
   removed). `agent.run(..., profile="reader", via="cli")` costs at most 3,400.
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

- **Q9.** Does a spawn's `cwd` set the subagent's working directory? Check with a
  reader agent asked for its working directory's listing.

**Verdict.** Exactly one of the following applies.

- **Promote.** Q1, Q2 and Q3 work, and some step of width 32 or more finishes with at
  least 99 % success and host RSS at most 2 GB. M3 proceeds. Its inputs, recorded with the
  verdict, are:
  - `PTC_HOST_MAX_CONCURRENCY`'s default: the widest step that met those numbers;
  - the launch recipe (Q1, Q2);
  - the cancel mechanism or its absence (Q5);
  - `"auto"`'s rule for schema calls (Q6);
  - the body cap (Q7);
  - whether `cwd` is carried (Q9).
- **Not promoted.** Any other result stops M1 with `BLOCKED`. Examples: completions work
  but agents do not; both work but no step of width ≥ 32 meets the numbers; mods load but
  completions fail or run one at a time; mods do not run headless at all. The `BLOCKED`
  carries:
  - the evidence;
  - the condition that failed;
  - a proposed revision: a narrower scope such as a host route for `llm()` alone, with
    acceptance 2 moved to the CLI route; or the non-goals' raw HTTP loop.

  A revision that narrows the scope or changes acceptance is the owner's to approve, so
  M3 does not start on it until the dispatching session has the answer.

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
- Deadlines follow "Deadlines". On expiry the kernel settles the caller with
  `TimeoutError` and queues a `cancel`. It does **not** release the job's host-semaphore
  slot: the slot goes back only when the job's result arrives or its generation retires
  ("Settlement is separate from capacity").
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
- the option-compatibility table: a non-default `cwd` (carried or routed per M1's Q9) and
  an `agent.run(output_schema=…)` under `"auto"` going to the CLI with `structured`
  populated, `via="host"` raising for it;
- deadlines: a call that spends most of its `timeout` waiting for a host permit gets only
  the remainder as `timeoutMs`, and a fallback to the CLI gets only the remainder too;
- the channel protocol against a fake mod written in Python (hello and protocol mismatch,
  held polls answered at once on a queued job, results, 404 on a settled id, the
  large-payload file path both ways);
- every in-flight job failing on host exit;
- the crash-loop switch to the CLI;
- retirement on lease lapse (a fake mod that stops polling) and on a lost delivery (a
  poll that does not ack), with every in-flight job failing and nothing replayed;
- capacity: repeated caller timeouts, with the abandoned agents still running in the
  fake mod, admit no more host jobs than the cap. A late result releases its slot
  exactly once, also when it races the caller's timeout. A cap filled by abandoned jobs
  retires the generation;
- delivery: a lost response carrying only cancels (the next poll's stale `ack`) retires
  the generation. A result POST the mod cannot deliver after its retries ends in the
  lease retirement;
- deadlines: a fallback reached with its budget spent raises `TimeoutError`;
  `timeout=0` raises `TimeoutError` on both routes; a negative `timeout` raises
  `ValueError` at entry;
- envelope sizing: a newline-heavy text that is small raw but large encoded spills to a
  file; an over-4-MiB field is refused;
- two simultaneous reader runs in two directories holding identically named files, each
  answering from its own directory;
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
PTC_LIVE=1 uv run pytest test/live/test_llm_live.py test/live/test_agents_live.py -q -rs   # real model calls (haiku)
PTC_LIVE=1 uv run pytest test/live -q -rs -k host   # M3's live host tests, once they exist
```

The live suites skip unless `PTC_LIVE=1` (`test/live/test_llm_live.py`). They need a
`claude` CLI that can authenticate, as this machine's can through the local gateway. A
live run counts only with its executed-test count recorded: `-rs` lists skips, and an
all-skipped run proves nothing. M3's new host live tests use the same `PTC_LIVE` gate.

Spike (M1): `uv run python test/spikes/s9_agent_host/drive.py --ramp 8,32,128,256 --main-tools default`
(the recorded run's host cwd was a temporary directory outside git; the driver's
`--main-tools` default, `none`, cannot spawn agents, per Q2) prints
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
    async def submit(self, job: dict, deadline: float | None) -> dict: ...  # deadline in time.monotonic(); the Result; raises AgentFailed on host exit or deadline
    async def close(self) -> None: ...

def get_host() -> AgentHost: ...            # the kernel's one instance

# public, in the kernel namespace
async def llm(prompt, *, model="haiku", system=None, json_schema=None, timeout=300.0,
              profile="bare", via="auto", max_tokens=None, effort=None): ...
# max_tokens=None: the CLI's own per-model output cap, so both routes return the same reply
# agent.run / agent.spawn gain: profile=None (→ "full"), via="auto"
# AgentResult gains: via: str = "cli", agent_id: str | None = None
```

- The channel protocol and the job and result shapes are under "The channel".
- New dependency: `jsonschema` (structured output on the host route).
- The host plugin is named `ptc-host`. Its module path is `hooks/mods/host.tsx`.
- Environment it reads: `PTC_HOST_SOCKET`.
- Configuration: `PTC_HOST` (`auto` | `off`, default `auto`), `PTC_HOST_MAX_CONCURRENCY`
  (default 256, from M1), `PTC_HOST_START_S` (30).

## Surprises & Discoveries

- Observation: Claude Code 2.1.305 was the current CLI, and the SDK pin's bundled CLI was
  2.1.237.
  Evidence: `claude --version` → `2.1.305`; `claude_agent_sdk/_cli_version.py` in
  0.2.142 → `2.1.237`, in 0.2.169 → `2.1.305`.

M1's answers follow (2026-10-20, SDK 0.2.169, bundled CLI 2.1.305, haiku, the
MacBook). The spike is `test/spikes/s9_agent_host/`. Each run wrote
`out/<mode>-<stamp>/report.json` and the host's debug log there (gitignored). The calls
are in `ledger.jsonl` beside the driver.

- Observation (Q1): the mod loads and `session.start` fires in an SDK-started headless
  CLI with no first query. `connect()` alone is enough.
  Evidence: `drive.py --q 1` gave `first_query=None hello=True start_s=0.82`. The
  `/hello` body carried `engine: "2.1.305"`, and the debug log reads `hooks module
  ptc-host@inline loaded … events: session.start,turn.complete,agent.offer,turn.step,tool.call`.
  No model call was made.
- Observation (Q2): `$.agent.spawn` runs a lean subagent to its `turn.complete` with no
  main-loop turn, but only if the host's main loop has the `Agent` tool. The spawn goes
  through that tool (`$.tool.call (ptc-host): Agent (prompt, description,
  run_in_background, model, subagent_type, cwd)` in the debug log). With main-loop
  `tools=[]`, or `["Read","Grep","Glob"]`, every spawn failed with `$.tool.call: no tool
  named "Agent" in this session`.
- Observation (Q2): a type's tool list depends on the main loop's tools, by a rule the
  spike did not pin down (it is not a plain intersection). With main-loop tools `Agent`
  alone, a type naming Read/Grep/Glob was offered only `['Grep', 'Glob']`: Read was
  silently dropped, while Grep and Glob, which the main loop did not list, stayed. With the main loop's default tools (`tools=None`, 24 listed) or
  `Agent,Read,Grep,Glob`, the type was offered exactly `['Read', 'Grep', 'Glob']`.
  Evidence: the `api-request-shape` row of each subagent transcript records the tools the
  request offered (`q2-20261020-193012`, `q2-20261020-193143`).
- Observation (Q2, floor): the reader-shaped type's first request costs **3,404** prompt
  tokens outside git and **3,996** in a git cwd. With `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1`
  in the host's environment it costs **3,604** in a git cwd. Measured by `drive.py --q 10`
  (one type part changed per job, prompt "Reply with exactly: OK", one request each).
  The breakdown outside git:
  - **812**: the engine's floor for any lean type. That is the identity line (62 chars);
    the subagent notes the engine appends to every type's prompt (about 1,240 chars:
    "Messages from the agent that launched you…" and five "Notes" bullets); the
    `# Environment` block (about 1,000 chars: cwd, platform, shell, OS, the
    untrusted-downloads line, the model line, the date); and the job prompt.
  - **+41**: `READER_SYSTEM` itself (853 with no tools).
  - **+2,551**: the three tool definitions. Alone, Read adds 998, Grep 1,562 and Glob
    563 (Grep's input schema is 2,764 chars).
  - In a git cwd, **+592** more. The gitStatus block (about 400 tokens: branch, status,
    recent commits) is what `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1` removes. The rest is
    the git lines of the `# Environment` block (here a worktree's two paragraphs, about
    200 tokens), which that variable leaves in place.

  Neither the notes nor the environment block has a switch an agent definition or an
  environment variable reaches that this spike found: `omitClaudeMd` is set and strips
  nothing more. Against the measurement the design rests on (2,583), the gap is the tool
  definitions (2,551 here against about 1,840 then) and about 70 tokens of base.
  Acceptance 2's "at most 3,000" does not hold for this build. The final host
  configuration below, in a git cwd, gives 3,604 (3,996 without the variable).
- Observation (Q2): the lean types ran on haiku (`claude-haiku-5-5` on every agent
  request of every run) only because the driver strips `CLAUDE_CODE_SUBAGENT_MODEL`,
  which this machine's sessions export as `opus`. The engine forces that variable on
  every subagent, a plugin's spawn included (`CLAUDE_CODE_SUBAGENT_MODEL_FORCE`,
  `agent.spawn`'s doc). The kernel inherits it from the session, so a host left with it
  would run every host agent on opus. `hostproc.STRIP` lists what the spike removes.
- Observation (Q3): `$.model.complete` runs N calls concurrently. 16 calls in 1.19 s of
  wall time, against 17.69 s of summed latency. All 16 were in flight at once (the
  mod-side start/settle intervals overlap 16 deep), and all 16 were right.
- Observation (Q4, the cap): `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` binds as set, and a
  spawn past it is **refused, not queued**. With the cap at 2 and 4 agents, the debug log
  reads `$.agent.spawn (ptc-host): 2 running at once; refused` twice. The call rejects,
  so the mod posts `reason: "error"`. The engine's default cap is 20 (2.1.304 bundle,
  `Izr = 20`). `CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION` caps a session's lifetime spawns
  (300 in this machine's environment), which a long-lived host would reach; the spike
  sets it to 100000.
- Observation (Q4): the ramp held at every width with no failures and at most 581 MB.
  Each step started a fresh host, ran half `complete` jobs and half reader agents that
  each made one `Read`, with `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` = width + 8 and the
  final host configuration below. `drive.py --ramp 8,32,128,256` printed:

  ```
  width=8 ok=8 fail=0 wall=6.39 rss_peak=365 p50=2.65 p95=4.34 cpu_s=1.22 cpu_peak=106% calls=12
  width=32 ok=32 fail=0 wall=5.43 rss_peak=472 p50=2.38 p95=3.02 cpu_s=1.63 cpu_peak=143% calls=48
  width=128 ok=128 fail=0 wall=10.51 rss_peak=480 p50=5.97 p95=7.62 cpu_s=4.11 cpu_peak=123% calls=192
  width=256 ok=256 fail=0 wall=11.05 rss_peak=581 p50=5.13 p95=7.45 cpu_s=6.31 cpu_peak=164% calls=384
  ```

  RSS is the host's process tree in MB, sampled every 0.25 s; `cpu_s` is the host's CPU
  time over the step. Every answer was right: each complete returned its number and
  each agent its file's token. At 256 the mod had all 128 completes and all 128 agents in
  flight at once (the start/settle intervals overlap 128 deep for each kind). No
  failure reason occurred at any width. One host held 128 concurrent subagents in 581 MB.
  The 8 CLI children of the 2026-10-20 fan-out held 2.5 GB.
- Observation (Q6): over 100 host `complete` calls with a two-field JSON schema, using
  the prompt + validate method of "Structured output", **0 needed the retry** and 0
  failed it. Every reply was bare JSON with no code fence (`{"n": 0, "parity":
  "even"}`), and all 100 carried the right `n`. The 100 ran concurrently on one host.
- Observation (Q5): the mod stops a running subagent with `$.tool.call({ tool:
  "TaskStop", task_id: agentId })`. That needs `TaskStop` among the main loop's tools,
  which the default set has. The agent's `turn.complete` arrived with `reason: "aborted"`
  0.01 s after the cancel was queued, after 2 of its up to 10 requests.
  Aborting `$.model.complete`'s signal resolves `aborted`, 0.01 s after the cancel.
  Two other strategies did not stop anything: answering the cancelled agent's
  `turn.step` from a hook, and denying its `tool.call`s. Both agents ran to their natural
  end (9 requests each), because a subagent a plugin spawned steps past that plugin's own
  hooks, as `turn.step`'s doc says.
- Observation (Q7): `$.http.fetch` refuses a request body over **4,194,304 characters**
  (`a request body of 4194305 characters is over the 4194304-character limit`). It
  refuses a response body over **4,194,304 bytes** (`body of 4194305 bytes is over the
  4194304-byte limit`). The response cap is in bytes, so the poll-response budget must be
  measured in bytes: JSON with `ensure_ascii` makes the two counts equal. A held request
  answered at 29.9 s arrives. One at 30.1 s or later is aborted at 30,000 ms (`aborted:
  no complete answer within 30000ms`). The 25 s hold has 5 s of margin.
- Observation (Q8): a host killed with `kill -9` shows up in the SDK at once. The
  message reader raised `ProcessError('Command failed with exit code -9 …')` 12 ms after
  the kill, and a later `query()` raised `CLIConnectionError('Cannot write to terminated
  process (exit code: -9)')`. The channel saw nothing: the held poll's connection just
  ended, and no further poll came. The two in-flight jobs stayed pending until the driver
  failed them, which is what M3's retirement must do. A fresh start took 2.91 s to
  `/hello`, against 0.93 s for the first start of the run.
- Observation (Q9): a spawn's `cwd` does **not** set the subagent's working directory.
  `cwd` reached the Agent tool's input (debug log), but both agents, given `dir-a` and
  `dir-b`, ran in the host's cwd: their transcript rows' `cwd`, their Globs (both saw
  both directories), and their failed relative Read of `same.txt` all show it.
- Observation: the engine writes a subagent's last transcript rows after the plugin's
  `turn.complete` hook has run. Read straight away, the final request is missing (1
  request seen, 2 written). The driver counts calls after a 2 s settle. M3's
  `agent.list()` transcript path is right, but anything that reads that transcript right
  after the result must allow for this.
- Observation: M1 spent **823 haiku calls** of its 850. Q1 0, Q2 16, Q3 16, the cap probe
  8, Q5 22, Q7 0, the floor breakdown 18, Q8 3, Q9 4, Q4 636 and Q6 100.
  Evidence: `test/spikes/s9_agent_host/ledger.jsonl`. Agent calls are counted from each
  transcript (one per assistant message id) and complete calls one per job. Every agent
  request recorded `claude-haiku-5-5`.

- Observation (M2): acceptance 3 measured live in a short non-git cwd with `PTC_LAUNCHER`
  set: `llm()` with `--bare` 61 prompt tokens, without `--bare` 400, the CLI `reader`
  3,068. In a git worktree with `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1` set: `llm()`
  without `--bare` 634, the CLI reader 3,302. For these CLI children the variable saves
  nothing (634 before it too); the ~240 extra tokens in a worktree are the worktree lines
  of the environment block, which it leaves in place, as M1 saw on the host. The 400
  limit is met with no margin: under pytest's `tmp_path` the same call is 417.
  Evidence: `test/live/test_profiles_live.py`; M2's task report.
- Observation (M2): `--tools` ignores MCP names without an error: `--tools
  Read,mcp__ptc__exec` still listed all six ptc tools. The backend therefore drops ptc
  names from `--tools`, and naming any one ptc tool attaches the whole server.

- Observation (M3): live, in real kernels. Acceptance 1: host 64/64 in 4.2 s on one host
  pid; `via="cli"` 64/64 in 21.5 s at cap 8. Acceptance 2: 32/32 readers in a git cwd,
  every first request 3,448 prompt tokens, all on `claude-haiku-5-5` with the session's
  `CLAUDE_CODE_SUBAGENT_MODEL=opus` exported. Acceptance 4: all 32 in-flight calls raised
  `AgentFailed: agent host retired: killed by signal 9 (SIGKILL)` and the next `llm()`
  answered on a new pid. Acceptance 5: the old host pid gone within 10 s of a restart.
  Acceptance 6: both fallbacks answer on the CLI, the unloadable plugin with exactly one
  warning. 324 haiku calls.
- Observation (M3): on 2.1.305 the Read tool shows an empty numbered line after a trailing
  newline, and haiku readers counted it as a line 3 times in 32. The live test's files end
  without a newline. The host reader floor is 3,448 in a plain git repository and 3,369
  outside git; M1's 3,604 was a worktree, whose environment block is longer.
- Observation (M3): on a host crash the SDK's logger writes `Fatal error in message
  reader: Command failed with exit code -9` to the cell's stderr before the calls fail.
  `jsonschema` was already in `uv.lock`, through `mcp`.

## Decision Log

- Decision: M3's fix wave, accepted by the author.
  - `llm(max_tokens=None)` sends the host `maxTokens: null`, which the mod asks as 64,000
    (`$.model.complete`'s cap), retrying once at the figure the engine names for a smaller
    model. An explicit cap reaches a CLI child as its `CLAUDE_CODE_MAX_OUTPUT_TOKENS`
    (`AgentOpts.max_output_tokens`, `llm()`'s alone; `agent.*` refuses it). A cap over
    64,000 sends `"auto"` to the CLI, and `via="host"` refuses it.
  - A cut is read from usage, `output_tokens >=` the caller's cap: the engine reports no
    stop reason and adds 2,048 tokens of thinking headroom on the wire, while the CLI's
    limit counts thinking too. Both routes then raise `AgentFailed` naming the cap, with
    the partial text. On the CLI that needed `run_once` to fold the SDK's `ResultError`
    into `AgentFailed`, which changes every is_error one-shot (max turns, a rate limit)
    for today's callers too; the 0.6.0 changelog says so.
  - Launch and hello share one start deadline; a launch that never returns fails the start
    and counts toward the crash loop.
  - A failed host result keeps its `agentId`, session and partial text, and the registry
    row gets `agent_id` and `transcript`. The `claude-host` row is written when the job is
    queued, so a call that fell back leaves none. Each logical call writes one audit record,
    once its route is final; a `HostUnavailable` before queuing writes none of its own.
  - Known limit, accepted: with no cap given, a reply between 64,000 and 128,000 tokens
    (the CLI's own default on this machine is 128,000) is whole on the CLI and fails loudly
    on the host as `AgentFailed` with the partial text. M4's doctrine says so in one line,
    with `max_tokens` above 64,000 as the way to the CLI.
  - Residual, logged as tech debt, not fixed: Bash tool processes run in their own process
    groups (`detached`), so a host SIGKILLed from outside the kernel, or one that ignores
    SIGTERM for 3 s, leaves its tools' groups running, as on the CLI route and codex. The
    optional fix: killpg the host's direct children's groups before the SIGKILL escalation
    in `_SdkHost.close`. Every exit that runs ptc's code reaps them (live probe).
  Rationale: the route choice must not change the answer, within what `$.model.complete`
  can produce.
  Date/Author: 2026-10-20, M3 task-executor; rulings by doperpowers-15; folded by the plan-executor.
- Decision: The author's rulings on M3's concerns. (1) `llm()`'s `max_tokens` defaults to
  the CLI's own per-model output cap, not 4096 (Interfaces), and a reply cut at the cap
  ends the same way on both routes, as the CLI route already surfaces it; the route
  choice must not change the answer. (2) Acceptance 2 runs on haiku (`model="haiku"`), as
  the live test does. (3) `READER_SYSTEM` cites files and ends with findings only when the
  task names no reply format of its own; acceptance 2 is re-checked with it in M4's
  acceptance run. (4) The mod's bounded result retry gets a stub-channel test if cheap,
  else a tech-debt entry; no live calls on it.
  Rationale: under `"auto"` a 4096 cap would cut replies the CLI returns whole. The
  citation instruction outweighed "number only" (M3, Discoveries).
  Date/Author: 2026-10-20, doperpowers-15 (the spec's author), recorded by the plan-executor.
- Decision: M3's executor decisions, accepted by the author as in intent and folded.
  - The host runs `bypassPermissions` only: another `permission_mode` goes to the CLI
    (`via="host"` raises), because a bypass parent's mode takes precedence over a
    subagent's own and another mode would be dropped silently. `allowed_tools` is
    therefore always "carried as nothing".
  - Tools that start agents (`Agent`, `Task`, `Workflow`) send a call to the CLI, for the
    `PTC_MAX_DEPTH` reason the spec gives for `Agent`.
  - The session's variables (`host.EMPTIED`: the subagent-model, workflow-model, plugin-dir
    and session-identity variables) are set to empty in `options.env`, since the SDK
    merges `env` over `os.environ` and cannot delete a key; the engine reads each empty
    one as unset (checked in the bundle, and live: haiku on every host request with
    `opus` exported).
  - One socket per generation, `<kernel_dir>/host/g<n>.sock`; a retired generation's
    server answers 410, which ends that mod's poll loop. A poll superseded by a newer one
    is answered empty; the kernel caps any hold at 25 s.
  - Protocol 1 additions: `/hello` carries `session` and `cwd` (the transcript path and
    `session_id`), and its response carries `outDir`. A `textFile` is accepted only at
    `out/<id>.txt`. A job's `cwd` field is sent as `null` (Q9).
  - A job that waited for a slot while its generation retired starts the next
    generation; `submit` raises `HostUnavailable` only before anything is queued. A
    caller abandoning a never-delivered job releases its slot at once.
  - `enabled()` is false when the host is closed or `PTC_LAUNCHER` is unset (no plugin to
    find); then `"auto"` uses the CLI and nothing counts toward the crash loop. A missing
    `host-plugin/.claude-plugin/plugin.json` fails the start at once.
  - Warnings: one per failed start, shared by its waiters; one at the crash-loop switch.
  - A result with reason `"timeout"` raises `TimeoutError`, like the kernel's own deadline.
  - `PTC_HOST*` are read once from the kernel's environment in `get_host()`, not added to
    `Config`.
  - The route logic lives in `routes.py`, the channel and lifecycle in `host.py`.
  Rationale: each follows the spec's stated reasons where its letter was silent.
  Date/Author: 2026-10-20, M3 task-executor, accepted by doperpowers-15, folded by the plan-executor.
- Decision: Acceptance 3's limits get margin: `llm()` without `--bare` at most 450, the
  CLI reader at most 3,400, `llm()` with `--bare` still at most 100, in any non-git cwd
  (the length condition dropped; M4 runs it under pytest's `tmp_path`).
  Rationale: the limits are the measured values (400, 3,068) plus about 10 %, because the
  environment block grows with the cwd's path (417 under `tmp_path`); the criterion checks
  how the prompt is made up, not how long the path is.
  Date/Author: 2026-10-20, doperpowers-15 (the spec's author), recorded by the plan-executor.
- Decision: The spec's author's rulings on M2's concerns. (1) The ptc MCP server attaches
  to an `agent.*` CLI child only when its tools are `"default"` or name `mcp__ptc` /
  `mcp__ptc__<tool>` ("Profiles"). A fix in intent, not a new contract: v0.5.4's doctrine
  already says "Give each child exactly the tools its task needs — `tools=["Read",
  "Grep"]` for a reader" (skills/ptc/SKILL.md), and its 2,776 was measured outside a
  kernel, so the leak contradicted the documented behavior. M4 says so in the changelog
  and in the skill's `tools=` paragraph: a child that needs its own kernel names
  `mcp__ptc`. A test runs with `PTC_LAUNCHER` set, so acceptance 3 is measured as a real
  kernel runs. (2) Acceptance 3 is stated for a short non-git cwd, and every non-`full`
  CLI profile runs with `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1`, as the host does; the
  git-worktree figure with the variable set is recorded beside it. (3) Measuring the
  no-credential case as credential present with `--bare` withheld is accepted.
  Rationale: the alternative that lost, the server on every non-llm child, made the CLI
  reader cost 5,792 tokens rather than about 3,066 and let a reader run code. M2 measured
  the cwd's cost: the no-`--bare` `llm()` was 392 in `/tmp/<short>`, 417 under pytest's
  `tmp_path` and 634 in a git worktree, where the CLI reader was 3,327.
  Date/Author: 2026-10-20, doperpowers-15 (the spec's author), recorded by the plan-executor.
- Decision: M2's executor decisions, folded. (1) `full`'s system is `""`, not
  `"claude_code"`: today's `agent.run` child runs on the SDK's empty `--system-prompt ""`
  (619 tokens), and the preset would cost 3,733; "exactly today's child" is the intent.
  `full` still stays on the CLI route, through `tools="default"`. (2) `--bare` only for a
  tool-less, string-system, settings-`"none"` profile with a credential present, since on
  2.1.305 it cuts a tool list to Bash/Edit/Read (`reader` lost Grep and Glob). (3) `llm()`
  refuses a profile whose settings are not `"none"`: user settings load plugins and MCP
  servers, ptc's among them, and break `bare_llm`'s no-tools guarantee. (4) Memory uses
  Claude Code's exact label, `project instructions, checked into the codebase`; the
  project walk stops at the nearest `.git` (a worktree's `.git` file counts), and outside
  git only the cwd's own `CLAUDE.md` is read. (5) `READER_SYSTEM` is 25 words.
  Rationale: each keeps the spec's stated intent where its letter was wrong about 2.1.305.
  Date/Author: 2026-10-20, M2 task-executor, folded by the plan-executor.
- Decision: M1's task review approved the spike with Minor findings only. Two bear on
  M3 and are recorded here. Q2's tool rule is restated as observed, not as an
  intersection (above). And the spike's result-POST retry is not bounded at 10 s: three
  awaited fetches, each allowed 30 s, can take about 96 s. "The channel"'s 10 s bound is
  M3's to implement with an overall deadline on the retries; the spike is no evidence for it.
  Rationale: M3 builds against what the spike shows, not what its prototype claimed.
  Date/Author: 2026-10-20, plan-executor.
- Decision: Acceptance 2's floor becomes "a reader agent's first request on the host
  route, in a git cwd, is at most 4,200 prompt tokens": M1's 3,604 floor for the shipped
  host recipe plus about 15 %. The floor's parts (M1, Q2): the engine's floor for any lean
  type, 812 (identity line, the subagent notes the engine appends to every type, the
  `# Environment` block, the job prompt); `READER_SYSTEM`, 41; the Read/Grep/Glob
  definitions, 2,551; in a git cwd, the gitStatus block, stripped by
  `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1` in the host's environment, with about 200
  tokens of git lines left in the environment block. The stale 2,583 row of the floors
  table is corrected to 3,404; the rest of that table waits for M4's re-measure.
  Rationale: the alternative that lost was keeping 3,000, which would need the reader
  tools' schemas cut, and those are the engine's, not ptc's. The intent, lean agents
  about an order of magnitude under `general-purpose` (26 k), holds at 4,200.
  Date/Author: 2026-10-20, doperpowers-15 (the spec's author), recorded by the plan-executor.
- Decision: **M1 verdict: Promote.** Q1, Q2 and Q3 work. Every ramp step met the criteria
  (≥ 99 % success, host RSS ≤ 2 GB); the widest was 256, at 100 % and 581 MB. M3
  proceeds with these inputs:
  - **`PTC_HOST_MAX_CONCURRENCY` default: 256**, the widest step that met the numbers.
    The ramp's top was 256; nothing wider was measured.
  - **Launch recipe (Q1, Q2).** Through `ClaudeSDKClient`, `connect()` with no first
    query is enough. `/hello` came 0.8–0.9 s after `connect()` on a first start and 2.9 s
    after a restart. The main loop's tools must be the default set (`tools` left unset):
    a spawn goes through the `Agent` tool, a cancel through `TaskStop`, and only with
    the default set was a type's tool list observed to hold exactly (Q2). M3 relies on
    that configuration, not on a general rule. The main loop never runs a turn, so its
    tool list costs nothing. `setting_sources=[]`, no MCP servers, the plugin through
    `plugins`, `cwd` the kernel's. The environment:
    - `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1`;
    - `PTC_HOST_SOCKET` = the socket's **short real path**, not the symlink, since
      `socketPath` is a `sun_path` too;
    - `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` ≥ `PTC_HOST_MAX_CONCURRENCY` (the spike
      used width + 8), so the kernel's semaphore is the only cap that binds: a spawn
      past the engine's cap is refused, not queued;
    - `CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION` set far above any host's lifetime (the
      spike used 100000). The kernel's environment may carry 300;
    - `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1`, which takes about 400 tokens of gitStatus
      off every host agent in a git cwd;
    - removed: `CLAUDE_CODE_SUBAGENT_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` and
      `CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL`, which would force a model on every host
      agent; `CLAUDE_CODE_PLUGIN_DIRS`, which would load the session's mods into the
      host; and the parent session's identity variables (`hostproc.STRIP`).
  - **Cancel (Q5).** An agent is stopped by `$.tool.call({ tool: "TaskStop", task_id:
    agentId })`, and its `turn.complete` then reads `reason: "aborted"` within about
    10 ms. A `complete` is stopped by aborting its signal, and resolves `aborted`. The
    one-result rule holds for both. A plugin's own `turn.step` and `tool.call` hooks
    cannot stop its spawned agents.
  - **`"auto"` and schema calls (Q6).** `"auto"` sends `llm(json_schema=…)` to the host:
    0 of 100 needed the retry, within the rule's "no more than 1 in 100, none failing".
  - **Body caps (Q7).** A request body may be up to 4,194,304 characters, and a response
    body up to 4,194,304 **bytes**. A held request is aborted at 30,000 ms. The kernel's
    4,000,000 poll-response budget should be counted in bytes of the encoded JSON
    (`ensure_ascii` JSON makes that the character count). The 25 s hold stands.
  - **`cwd` (Q9): not carried.** A spawn's `cwd` does not move the subagent, so under
    `"auto"` a `cwd` other than the kernel's goes to the CLI route, and `via="host"`
    raises for it, as the option table's fallback says.

  Two findings bear on acceptance, and the spec's author owns them. Acceptance 2's
  3,000-token reader floor does not hold. The host reader costs 3,404 outside git and
  3,604 in a git cwd with the recipe above; the cause breakdown is in Surprises &
  Discoveries. And host death is visible only through the SDK, not through the channel
  (Q8).
  Rationale: the verdict rule in M1's entry, applied to the evidence in Surprises &
  Discoveries.
  Date/Author: 2026-10-20, M1 task-executor.

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

- Decision: Second review round folded in.
  - One result POST ends every job, a `late` one included. It releases the slot
    idempotently, so the separate `/ended` notice is removed.
  - Acks cover whole poll responses by `seq`, so a lost cancel is detected too.
  - M3 no longer releases a slot at the caller's timeout.
  - Host-route timeouts raise `TimeoutError`, as `guarded` does.
  - A spent fallback budget is clamped to 0 rather than passed negative.
  Rationale: The first fold left M3 contradicting "Concurrency". `/ended` and cancel-only
  responses sat outside the delivery guarantee. A late result answered 404 without
  releasing its slot. Passing a negative remainder to `guarded` raised `ValueError` for
  an exhausted, valid timeout (verified by running `guarded` in isolation).
  Date/Author: 2026-10-20, doperpowers-15.
- Decision: Design review folded in.
  - A slot is released only when its job has ended in the host, not when its caller
    settles.
  - Host agent jobs get a `maxTurns` default of 50.
  - A cap filled by abandoned jobs retires the generation.
  - Host generations retire on exit, on a 60 s lease lapse, or on a lost delivery
    (detected by acks), and never replay.
  - Cold-start failure falls back within the call's deadline.
  - Sizes are measured on the serialized envelope, with spill and `too-large` rules.
  Rationale: Releasing a slot at the caller's timeout let abandoned agents outlive the
  cap. Process exit alone missed a dead poll loop, and with `timeout=None` that meant a
  call could wait forever. A raw-text size threshold could still overflow the fetch cap
  after JSON escaping. Redelivery with deduplication was rejected in favor of retirement:
  simpler, and it never runs an agent twice.
  Date/Author: 2026-10-20, doperpowers-15.
- Decision: Buildability review folded in. Added the option-compatibility table, one
  deadline per call (`AgentHost.submit` takes a monotonic deadline), an exhaustive M1
  verdict whose every non-promote outcome is `BLOCKED` with a proposed revision for the
  owner, Q9 (spawn `cwd`), and `PTC_LIVE=1` on the live commands.
  Rationale: Without the table, a `cwd` or `output_schema` would have been dropped on the
  host route. Without the deadline rule, `guarded` would have been assumed to expose a
  remainder it does not expose. The spike had outcomes with no verdict. The live suites
  skip silently without `PTC_LIVE`.
  Date/Author: 2026-10-20, doperpowers-15.

## Outcomes & Retrospective

Pending — written at finish.
