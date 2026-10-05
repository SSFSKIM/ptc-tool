# ptc — Programmatic Tool Calling for Claude Code

A persistent per-session IPython kernel exposed to Claude Code as an MCP server, with
Claude-Code-equivalent tools pre-bound as Python functions: `read`, `write`, `edit`,
`bash`, `agent` (Claude + Codex children), `llm`, `web_fetch`, `web_search`, `history`,
`workflow`.

The point is that intermediates stay in the kernel instead of in the model's context. One
cell can read four hundred files, filter them deterministically, fan out to child agents,
and print a summary — and only that summary reaches the conversation. Variables, imports
and agent handles survive across tool calls, turns, compaction, and `claude --resume`.

## Install

    /plugin marketplace add SSFSKIM/ptc-tool     # inside Claude Code
    /plugin install ptc@ptc-tool

Or from a checkout (dev install — the package IS the plugin root):

    cd ptc-tool/ && uv run ptc setup     # provision ~/.ptc/venv (one-time)
    claude --plugin-dir .

On a warm `uv` cache the launcher can provision the venv inside Claude Code's 30 s MCP
startup window on its own; running `ptc setup` once first is what makes a cold cache safe.

Recommended settings (`~/.claude/settings.json`) for prompt-free use:

    {"permissions": {"allow": ["mcp__plugin_ptc_ptc__exec",
                               "mcp__plugin_ptc_ptc__wait",
                               "mcp__plugin_ptc_ptc__interrupt",
                               "mcp__plugin_ptc_ptc__restart",
                               "mcp__plugin_ptc_ptc__kernels"]}}

Those long names are not a typo. A plugin-provided MCP server's tools are named
`mcp__plugin_<plugin>_<server>__<tool>`; only a **directly** registered server
(`claude mcp add ptc -- <ptc>/bin/ptc-launch`) gets the short `mcp__ptc__*` form. Note
also that plugin MCP tools arrive **deferred** — the model loads them through ToolSearch
before the first call, which is why the skill prints the long name verbatim.

## Trust model — read this

Allowing `mcp__plugin_ptc_ptc__exec` IS the security decision: from then on, model-written
Python runs with your OS permissions, outside Claude Code's per-tool permission prompts
and sandbox, and Claude children spawned from the kernel default to `bypassPermissions`.
The mutation footer and `~/.ptc/kernels/<session>/audit.jsonl` give visibility, not
enforcement — plus an opt-in deny tripwire (`~/.ptc/policy.json`, env `PTC_POLICY`), which
is a tripwire, never a sandbox. Its rules are yours (PTC ships none) and they trip `bash`,
`write`, `edit`, and `web_fetch` — the only names a rule may list, and per redirect hop —
with an audited `PermissionError`; raw Python in the kernel stays ungoverned by design, and
a malformed policy file (an unknown tool name included) fails governed calls loudly rather
than pretending to protect. A kernel from a current build reads the policy live, so one you
write or edit mid-session governs its very next call — no restart needed. What is refused
is attaching to a kernel booted from a build older than policy enforcement itself: its
wrappers cannot consult a policy at all, so the refusal names both exits — `restart()` to
govern it (the namespace is lost) or remove the policy file to keep the ungoverned
namespace. Use a worktree or container for untrusted work.

The kernel inherits the MCP adapter's environment verbatim, credential-bearing `CLAUDE_*`
variables included. Nothing in PTC enumerates that environment into a log, an audit record
or a registry row, and Codex children get an environment built from an allowlist rather
than an inherited copy, so Claude-side credentials never cross into another vendor's
binary.

Before you enable `provider="codex"`, know what it costs. PTC starts every codex thread
`sandbox: "read-only"`, and read-only in Codex means no WRITES — it grants full-disk
**reads**, and the child gets your real `HOME` (that is where its subscription auth lives).
So a codex task, or a prompt injection that reaches one, can read any file you can —
`~/.claude` among them — and whatever it reads leaves in a request to OpenAI. Nothing in
PTC prevents that; it is the same bargain as allowing `exec` in the first place. Keep codex
children out of directories whose contents you would not send to another vendor.

Billing: all kernel-originated Claude model calls go through the `claude` CLI (your
subscription when OAuth-logged-in). Do not put `ANTHROPIC_API_KEY` in the environment — it
silently shadows OAuth and flips billing to the metered API. `codex` children authenticate
the same way from `~/.codex/auth.json`; `OPENAI_API_KEY` is likewise not forwarded.

## Lifecycle

One detached kernel per Claude Code session, discovered via a SessionStart hook. State
survives `--resume` until the idle TTL (default 24 h; `PTC_IDLE_HOURS`), a `restart`, or
the machine rebooting. A restart loses the Python namespace but not child agent sessions —
those live in `agents.json` and stay resumable through `agent.list()` / `agent.resume()`.

A subagent's auto-keyed kernel (`<key>--sub-<agent_id>`) idles out after 1 h instead
(`PTC_SUB_IDLE_HOURS`, never longer than `PTC_IDLE_HOURS`): it serves one subagent run and
has no `--resume` to wait for. Each key's directory under `~/.ptc/kernels/` outlives its
kernel so the next attach can report the expiry; the MCP server sweeps them at startup once
the kernel is confirmed gone and nothing in the directory has changed for 7 days (1 day for
a subagent key). A main key whose `agents.json` still lists child sessions is kept.

Two things end an idle kernel before its TTL. When a subagent stops, a `SubagentStop` hook
marks its kernel and the kernel goes 10 min later (`PTC_STOP_GRACE_MIN`) — long enough for
a SendMessage continuation, and any cell run after the stop restores the normal TTL. And a
kernel holding a lot of memory goes early: ≥ 1 GB after 30 min idle (`PTC_HEAVY_MB`,
`PTC_HEAVY_IDLE_MIN`), or ≥ 512 MB after 5 min idle while macOS reports memory pressure
(`PTC_PRESSURE_MB`, `PTC_PRESSURE_IDLE_MIN`). Memory is the kernel's physical footprint
(compressed and swapped pages included; RSS on Linux, which has no pressure rule). A cell
in flight is never expired, and the next call's notice says which rule fired. Results from
a kernel holding ≥ 512 MB show it in the header (`mem 5.2G`), and `kernels` lists each live
kernel's footprint.

`list | doctor` inspect this machine's kernels from any shell; `exec | wait | interrupt |
kill | restart` act on one kernel. Those five take `--session`; with none given the CLI
picks the newest live kernel and prints which one it picked. `kill --all` ends every
kernel whose ownership still checks out, and `doctor` only reports — it prints what
`setup` would provision rather than provisioning anything itself.

Nothing installs a `ptc` onto your `PATH`, so run it by one of the two paths that exist.
From the session's own Bash tool, or any shell, the provisioned venv has it:

    ~/.ptc/venv/bin/ptc exec 'print(x)'
    ~/.ptc/venv/bin/ptc list

From a checkout of this package (the form `setup` above uses), `uv run` puts its own
project environment on the path for one command:

    cd ptc/ && uv run ptc list

Both drive the same kernels; the venv one is what the skill and the acceptance scenarios
use, because it needs no checkout and no working directory. Adding a `ptc` to your own
`PATH` — a symlink, a shell alias — is yours to do and nothing here does it for you.

Exit codes: **0** success · **1** the cell raised, the wait found no such cell, or `kill`
found nothing to kill · **3** the kernel was busy and the code was **not** run · **130**
the cell was interrupted before it finished. Nothing is ever queued, so 3 means "resubmit
after it finishes, or `interrupt` first" — a cell that was submitted and is merely still
running is a success (exit 0) and prints its `wait` cell id. 130 is the shell's
128+SIGINT: the cell was stopped, which is a different instruction to a script than a cell
that ran and failed.

## Codex (documented, untested)

    codex mcp add ptc -- /abs/path/to/ptc/bin/ptc-launch

This works because the adapter is plain stdio MCP, but no acceptance test exercises it.
Session keying degrades to explicit `session=` / `PTC_SESSION`; when PTC cannot key off a
host session id, every result header says so (`[keying: adapter-local]`).

## Configuration

| env | default | meaning |
|---|---|---|
| PTC_HOME | ~/.ptc | root (expanded and resolved to an absolute path, so a relative setting means the same directory in the kernel and its children as in the shell that set it) |
| PTC_SESSION | — | session key override; set on the kernel and its children |
| PTC_YIELD_S | 300 | exec/wait yield timeout |
| PTC_MAX_OUTPUT_CHARS | 12000 | result cap (server clamp 50000) |
| PTC_IDLE_HOURS | 24 | kernel TTL |
| PTC_SUB_IDLE_HOURS | 1 | TTL for a subagent's auto-keyed kernel (capped by PTC_IDLE_HOURS) |
| PTC_STOP_GRACE_MIN | 10 | how long a subagent's kernel outlives the subagent's stop |
| PTC_HEAVY_MB / PTC_HEAVY_IDLE_MIN | 1024 / 30 | idle kernels at or over this footprint expire after this long (`0` MB disables) |
| PTC_PRESSURE_MB / PTC_PRESSURE_IDLE_MIN | 512 / 5 | the same rule while macOS reports memory pressure (`0` MB disables) |
| PTC_MEM_NOTE_MB | 512 | footprint at which result headers show the kernel's memory (`0` disables) |
| PTC_MAX_CONCURRENCY | 8 | SDK-call semaphore |
| PTC_MAX_DEPTH | 1 | agent recursion brake |
| PTC_CODEX_INHERIT | unset | `1` lets Codex children see your `~/.codex` hooks, plugins and plugin-provided skills (the default `--disable hooks --disable plugins` removes all three); credential stripping from the codex child's environment is unconditional and this knob does not affect it |

POSIX only (macOS/Linux) — detached spawn, flock-based ownership and the launcher all
assume it; Windows is future work. Python >=3.12.
