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
- M4 (doctrine, acceptance, ship): complete, reviewed clean. c7dc7a0..b830461 (ship v0.6.0),
  fix 6f16eb5, 908e7b6. Acceptance 1–6 pass (acceptance 2 32/32 on rerun); acceptance 7's last
  full run had one integration flake (test_mutation_footer, start timeout, 3/3 on rerun).
  Every CLI child's hidden session-title request is now off (CLAUDE_CODE_DISABLE_TERMINAL_TITLE,
  7c415dc). Requests, from the gateway log: M4 397/400, M3 ~408 (reported 335), M2 ~52–62
  (reported ~44), M1 823 (host only).
- Whole-branch review (reviewer-high, 5a6bca7..908e7b6): incorrect, 3 P2 — a launch/close race
  leaves a launched host unowned (host.py ~306); the mod spawns an agent after a cancel that
  arrived during preflight (host.tsx ~325); a failed agent's partial text is lost (host.tsx
  ~307). Fix wave IN PROGRESS on afec58e598f5dea84, with the deferred schema-retry
  double-audit record added.
- Not started: re-review, the v0.6.0 tech-debt comment on ptc-tool issue #1, Outcomes &
  Retrospective, PR.

## Resume path

Ledger: .doperpowers/sde/2026-10-20-agent-host-design/progress.md (bases, heads, handles).
Wait for the fix wave's "Branch-review fix wave" section in task-3-report.md, then resume
reviewer-high (af8ac97308df732c2) on 908e7b6..<fix head>. Then the tech-debt comment
(hard-kill killpg, 64k–128k limit, fork warning, flakes), Outcomes & Retrospective, PR.
