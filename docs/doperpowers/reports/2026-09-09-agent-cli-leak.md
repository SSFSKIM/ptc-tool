# Leaked agent CLIs: 80 processes, 10.7 GB, 9 hours after the work finished

**Date:** 2026-09-09
**PTC version:** 0.5.0 (build `7359787981e2`)
**Kernel:** `28a8164d-3315-4091-9c70-0c31de05e495` (pid 39222), cwd `~/Developer/GitHub/doperpowers`
**Host:** macOS, 32 GB RAM
**Observed on:** packaged plugin build `7359787981e2` (0.5.0)
**Line refs verified against:** repo HEAD `07cc7b4` on `main`

## Summary

A brainstorming fan-out in one PTC kernel spawned 157 agents across 9 cells. Every cell
returned `status: "ok"`. Nine hours later, **80 `claude` CLI subprocesses were still
alive**, holding **10.67 GB of RSS** and burning a **measured 1.21 CPU cores**
continuously. System swap was at 11.1 GB of 12 GB and the machine was thrashing.

The count is exact and not a coincidence:

| agent status in `agents.json` | count | live CLIs |
|---|---:|---:|
| `done`  | 80 | **80** |
| `error` | 17 | 0 |

Every agent that **succeeded** leaked its CLI. Every agent that **failed** was cleaned up.

## This is documented behavior, not a crash

`skills/ptc/SKILL.md:100-101` states it plainly:

> `h.close()` ends a finished one (its CLI stays alive for follow-up `send()`s until you do).

The design is deliberate and coherent: a handle stays live so you can `send()` a follow-up
turn without paying a fresh spawn. `src/ptc/runtime/claude_backend.py:243-265` is careful
about exactly one direction of this — it disconnects on the exception path, with a comment
naming the hazard:

```python
await client.connect()
# connect() has spawned a `claude` CLI. From here on the caller gets a Session or an
# exception, and on the exception path it gets no handle at all — so anything that
# fails after connect() must disconnect here or the CLI it spawned is a leak nobody
# holds a reference to (F6). Covers cancellation too: query() is awaited.
```

The success path returns a live `Session` by design. `agents.py:653` and `agents.py:739`
call `await h.close()` only inside `except Exception`. So the failure path is airtight and
the success path is the caller's responsibility.

That responsibility is never discharged by a fan-out that gathers results and moves on —
which is the shape SKILL.md itself teaches for `agent`.

## What makes it bite

### 1. The concurrency cap does not bound live processes

`PTC_MAX_CONCURRENCY` defaults to 8, and this kernel ran with `max_concurrency: 8`. But the
semaphore (`agents.py:486-490`, `guarded()` at `agents.py:67`) gates **in-flight turns**. A
finished-but-open handle has released the semaphore and still owns a CLI process.

So the cap held perfectly and the process count still reached 80 — **10× the number a
reader of "concurrency is capped at 8" would predict.** Nothing in the system counts live
CLIs, and no limit applies to them.

### 2. An idle Claude CLI is not free

The natural assumption is that a finished CLI parks cheaply until `send()` or teardown. It
does not. Measured over a 30-second wall-clock window across all 80:

```
80 procs sampled over 30s wall
total CPU consumed: 36.4s  -> 121% of one core (1.21 cores)
mean per proc: 1.5%   max: 2.1%
```

Not one was quiescent. Each also held an ESTABLISHED TCP connection to the API
(`160.79.104.10:443`) and ~133 MB RSS, nine hours after its last turn.

Cumulative CPU tells the same story: each process had accumulated ~19m30s of CPU over
9h31m of life, and the values clustered tightly (`19:1x`–`20:0x` across all 80). Processes
doing different work do not converge on the same CPU time; a shared idle loop does.

### 3. The only backstop is 24 hours away

`src/ptc/paths.py:175` sets `idle_hours: float = 24.0`. The kernel TTL is the sole
mechanism that would have reclaimed this. At 10.7 GB and 1.2 cores, a 24-hour backstop is
not a backstop — the machine degrades long before it fires. There is no per-agent idle
reaper.

## Timeline

| time | event |
|---|---|
| 01:03 | kernel `28a8164d` starts |
| 03:24:07 | first of the surviving 80 CLIs spawns (cell 19, 50 agents) |
| 03:38:36 | last of the surviving 80 spawns (cell 23, 30 agents) |
| 03:40:54 | last agent turn completes; `agents.json` last write |
| 03:40:55 | cell 23 returns `status: "ok"`, `duration_ms: 377870`, log reads `30 ok 0 errors` |
| 03:40 → 12:55 | kernel idle (36s of CPU in 12h). 80 CLIs stay up, ~1.2 cores, 10.7 GB |
| ~12:55 | found during an unrelated "why is this machine slow" investigation |

Agent spawns per cell: 3→15, 6→5, 9→10, 10→5, 14→15, 15→10, 19→50, 21→17, 23→30.
157 total spawns, 97 in `agents.json`, 80 alive at discovery.

Note the kernel's own CPU time was 36 seconds across 12 hours. Nothing was hung on the
Python side. The cells completed, the results were collected, the namespace went quiet —
and the process tree underneath it did not.

## Why it went unnoticed for 9 hours

Every signal a caller would check said the work was fine:

- cell `status: "ok"`, `0 errors`
- results returned and were used
- the kernel was idle
- `max_concurrency` was respected

Nothing surfaces live CLI count. The failure is invisible from inside PTC and only visible
from `ps`.

## Suggested directions

Roughly in order of value per unit of change:

1. **Auto-close on terminal status by default.** Make the success path symmetric with the
   error path: close the CLI when a turn reaches `done`, and require an explicit opt-in
   (`keep_alive=True`, or the first `send()` re-opening via `resume`) for the multi-turn
   case. The handle can outlive the process — `agents.py:225-228` already keeps
   `session_id` and messages across `close()` precisely so a closed handle stays useful,
   and `agent.resume(sid)` already exists as the re-entry path. This inverts the default so
   that the cheap, common shape (one-shot fan-out) is the safe one.

2. **Bound live CLIs, not just in-flight turns.** A second cap — or make the existing
   semaphore hold until close rather than until turn end — so that "concurrency 8" means
   what a reader expects.

3. **A per-agent idle reaper** well below the 24h kernel TTL. A `done` handle untouched for
   N minutes gets its CLI closed, keeping `session_id` for `resume`.

4. **Surface the count.** Live CLI count in the cell footer or `agent.list()`, so a fan-out
   that leaves 80 processes behind says so at the moment it happens.

5. **Document the cost.** SKILL.md:100-101 says the CLI stays alive but not what that costs.
   "~133 MB and ~1.5% of a core each, until you close it" would change how callers write
   fan-outs.

(1) alone would have prevented this entirely.

## Reproduction

Fan out N one-shot agents, gather results, do not call `close()`:

```python
hs = [await agent.spawn(f"...{i}...") for i in range(50)]
rs = [await h.result() for h in hs]
# no h.close() — N CLI processes remain live until the kernel's 24h TTL
```

Verify:

```sh
KPID=$(pgrep -f ipykernel_launcher | head -1)
ps -axo ppid=,pid=,rss= | awk -v k=$KPID '$1==k{n++; s+=$3} END{printf "%d live CLIs, %.2f GB\n", n, s/1048576}'
```

## Immediate mitigation

```sh
pkill -P <kernel-pid>          # drop leaked child CLIs, keep the kernel
```

Reclaimed here: 10.67 GB RSS and 1.21 CPU cores.
