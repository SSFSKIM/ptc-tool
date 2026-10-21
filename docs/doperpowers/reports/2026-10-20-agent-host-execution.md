# Agent host — execution report (interim)

Plan-executor for docs/doperpowers/specs/2026-10-20-agent-host-design.md, branch `agent-host`.
This is an interim report: the harness required a hand-back while M3's executor was still
running. Nothing is pushed and no PR is open.

## State

- Base merge: origin/main 5a6bca7 (v0.5.5, claude-agent-sdk 0.2.169) merged at 1f50755.
- M1 (spike): complete, reviewed clean. Commits 2edebad..50d3e17. Verdict Promote (Decision
  Log), 823/850 haiku calls. Review minors folded into the spec at a51eb9d.
- M2 (profiles, CLI route): complete, reviewed clean. 3777674, fix 3e554d5. About 44 haiku
  calls. Unit 650, integration 69, live profiles 3/3 executed.
- Spec revisions from the author's rulings, all committed: d88c747 (acceptance 2 at 4,200,
  host cap 256), e7d196b (M2 decisions), 35ec513 (ptc MCP only when named, git context off),
  aba75bb (M2 measurements), 4b8a600 (acceptance 3 at 450 / 3,400), d4eaefa (M2 tick).
- M3 (host route): complete, reviewed clean. 22e4c1b, 17ddb4e, fix be5003b; 335/400 haiku
  calls. Deferred Minor (tech debt): a schema retry that falls back between attempts writes
  two audit records.
- M4 (doctrine, acceptance, ship): executor ab1f018a781043a9b returned NEEDS_CONTEXT at
  84ba26e (commits 7e0bf3d, d0214ec, 84ba26e). Acceptance 1, 3, 4, 5, 6 pass; acceptance 2
  31/32, the miss caused by the fixture's line-0 labels (fixed in d0214ec, not run live).
  247/300 calls. The rerun (~64 calls, ~311 total) was put to the author; no answer yet. The
  ship commit (0.6.0, revision note, ship entry, Progress tick) waits on it.
- Not started: M4 task review, whole-branch review (high), the v0.6.0 tech-debt comment on
  issue #1, Outcomes & Retrospective, PR.

## Resume path

Ledger: .doperpowers/sde/2026-10-20-agent-host-design/progress.md (bases, heads, handles).
On the author's answer, resume the M4 executor (rerun or record 31/32, then the ship
commit). Then the M4 task review (c7dc7a0..ship head), then reviewer-high over
`git merge-base main HEAD`..HEAD. then M4, then reviewer-high over `git merge-base main HEAD`..HEAD.

## Friction

- The CLI cannot sign in on the MacBook with credentials removed ("OAuth session expired"),
  so acceptance 3's no-credential case is measured as credential present with --bare
  withheld. M2's executor logged this in the ptc-tool friction tracker (issue #4).
