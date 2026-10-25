# Working rules
Optimize for momentum and my usage budget.

## Autonomy
- Standing approval for routine dev actions (commit, push, tag, clean up stale branches/worktrees/artifacts, dev tools, processes and config): don't ask, note it in the summary.
- Small focused commits straight to the current branch; push once verified. No branches or PRs unless asked. Never commit secrets.

## Delegation
Inline: up to ~10 tool calls, or anything needing this conversation's context. Otherwise the cheapest fitting agent:
- `scout` for searches over ~3 reads, never Explore.
- `runner` for long builds/tests and push/CI/tag chores, with exact commands, branch and SHA.
- `designer` or `architect` for design, never `implementer`; `architect` also for root causes the first fix missed and anything an implementer failed twice.
- `reviewer` before push for agent diffs over ~50 lines or touching concurrency, security or release code; review smaller ones inline.
- One concern per agent (~100 turns, diff under ~500 lines). Fixes go back to the same agent via SendMessage. Parallel agents only on disjoint files.

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
