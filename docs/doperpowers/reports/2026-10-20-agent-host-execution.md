# Agent host — execution report

Plan-executor for `docs/doperpowers/specs/2026-10-20-agent-host-design.md`, branch `agent-host`,
integrated into `main` by a `--no-ff` merge (no PR, per the spec's author: ptc-tool lands on main
directly). Ships ptc v0.6.0.

## Milestones

| milestone | commits | review |
|---|---|---|
| merge of main (v0.5.5, claude-agent-sdk 0.2.169, CLI 2.1.305) | 1f50755 | — |
| M1 — spike: host feasibility | 2edebad..50d3e17 | task review Approved; minors folded into the spec (a51eb9d) |
| M2 — prompt profiles on the CLI route | 3777674, fix 3e554d5 | Needs fixes → Approved |
| M3 — the host route | 22e4c1b, 17ddb4e, fix be5003b | Needs fixes → Approved |
| M4 — doctrine, acceptance, ship v0.6.0 | 7e0bf3d..b830461, fix 6f16eb5, 908e7b6 | Needs fixes → Approved |
| Whole-branch review (reviewer-high, 5a6bca7..908e7b6) | fix 33df0fc | incorrect (3 P2) → correct |

The spec carries every decision and measurement (Decision Log, Surprises & Discoveries) and its
Outcomes & Retrospective (160083b).

## Validation

- M1 verdict Promote: 8/32/128/256-wide ramps all 100 %, 581 MB peak at 256, p95 7.45 s; 0/100
  schema calls needed the retry.
- Acceptance, run as written in M4: 1 (64/64 on one host in ~4 s vs ~21 s on the CLI at cap 8),
  2 (32/32 readers on the rerun with the fixed fixture, first request 3,459 ≤ 4,200, all on
  haiku), 3 (61 / 421 / 3,095 under `tmp_path`, limits 100 / 450 / 3,400), 4 (every in-flight
  call fails naming SIGKILL, next call on a new pid), 5 (old host pid gone after restart), 6
  (both fallbacks answer on the CLI, the unloadable plugin with one warning). 7: unit and
  integration pass except load-sensitive flakes that pass on rerun (below).
- After the branch-review fix wave: `uv run pytest test/unit test/integration -q` → 797 passed,
  1 warning (pre-existing fork DeprecationWarning); focused host/routes/backend/profile 145/145;
  mod typecheck clean; live channel tests 2/2 with zero model calls.
- On the merge commit: the unit suite (recorded in the hand-back).

## Model requests (counted at the gateway log by session id)

M1 823 (host only, under its 850 budget). M2 ~52–62 (reported ~44). M3 ~408 (reported 335,
against a 400 cap). M4 397 of its 400 cap. The CLI-route undercount came from every CLI child's
hidden session-title request, found in M4 and removed (7c415dc).

## Residuals

Logged on ptc-tool issue #1 ("From v0.6.0"): host tool processes outliving an externally
SIGKILLed host (killpg fix sketched); the 64k–128k uncapped-reply divergence; the
preflight-cancel fix covered by typecheck and the reviewer's mocked-`$` probe only (no
kernel-side timing hook; a TS harness for the mod would close it); an unreadable prompt file
surfacing as the spawn's "non-empty prompt" refusal; test-output noise; one figure measured on
an earlier CLI build.

## Friction

- The MacBook's CLI cannot sign in with credentials removed ("OAuth session expired"), so
  acceptance 3's no-credential case is measured as credential present with `--bare` withheld
  (friction tracker #4).
- Load-sensitive integration flakes (`test_peek_on_a_dead_kernel_raises_unavailable`,
  `test_mutation_footer`) under concurrent live runs (friction tracker #4).
- The harness forced five interim hand-backs while subagents were mid-run; each resumed from
  the ledger without loss.
