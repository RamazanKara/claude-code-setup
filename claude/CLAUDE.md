# Working rules
Optimize for momentum and my usage budget.

## Autonomy
- Standing approval for routine dev actions (commit, push, tag, clean up stale branches/worktrees/artifacts, dev tools, processes and config): don't ask, note it in the summary.
- Small focused commits straight to the current branch; push once verified. No branches or PRs unless asked. Never commit secrets.

## Delegation
Split work by context, not by role: one loop carries a task from plan to verified result. Every handoff re-explores and loses detail (implementers spend ~22% of their cost before their first edit), and a main-chat turn costs only ~1.3× an agent turn.
- Work here when you already hold the context (you just investigated it, we're iterating on it) or it takes under ~30 tool calls. Don't hand off what you just worked out.
- Delegate when the agent's context is disposable or the work runs in parallel; it returns a compact result. Briefs carry what you already found (paths, lines, causes) so nobody re-explores.
  - `implementer`: changes in code you haven't loaded, or parallel tasks. Design-then-build is one implementer (effort xhigh when the design is hard), not `architect` then `implementer`.
  - `runner`: long builds/tests, device runs, push/CI/tag chores, with exact commands, branch and SHA.
  - `scout`: searches over ~3 reads, never Explore. `mechanic`: fully specified mechanical edits.
  - `designer`: UI where look and feel matter.
  - `architect`: decisions with no build in the same task, root causes a first fix missed, anything an implementer failed twice.
- `reviewer` is the fresh-eyes check before push for diffs over ~50 lines or touching concurrency, security or release code, yours included; review smaller ones yourself.
- One concern per agent (~100 turns, diff under ~500 lines). Fixes go back to the same agent via SendMessage. Parallel agents only on disjoint files.
- Follow-up work in the same files or area goes to the implementer that just worked there (SendMessage), not a new one; a new run re-explores (~$1.65 each). Start fresh only for a different area, or when that agent is near compaction or past ~150 turns.
- Change these rules only on measurement: run claude-code-setup's `tools/cost_report.py` before and after.

## Context
- Filter output to errors/tail; send long logs to a file and grep it.
- Wait on long builds, tests and CI (`gh run watch <id> --exit-status`) in the background.
- New unrelated topic: a fresh session (`start_session` if available) unless quick.

## CI
One workflow (push to main + manual), one job (lint, tests, build), cheapest runner. No schedules, PR triggers or scans unless asked.

## Done means verified
Run the tests/build/lint or the app; look at user-facing results. Fix failures instead of reporting them. Say what you couldn't verify.

## Output
Result first, concise. End with what changed, what was verified, what I need to know.

@~/.claude/personal.md
