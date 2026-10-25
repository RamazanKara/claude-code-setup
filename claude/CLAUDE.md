# Working rules

Optimize for momentum and for my Claude usage budget.

## Autonomy
- Work autonomously with standing approval for routine dev actions: commit, push, tag, delete stale branches/worktrees/artifacts, install or remove dev tools, kill dev processes, change dev config. Don't ask; pick the best option and note it in the summary.
- Commit straight to the current branch in small focused commits and push once verified. No feature branches or PRs unless I ask.
- Never commit secrets.

## Delegation
Do quick things inline: up to ~10 tool calls, or anything that needs this conversation's context. Delegate the rest to the cheapest agent whose description fits:
- Searches needing more than ~3 reads go to `scout`, never Explore.
- Long builds, test runs and integration chores (push, CI wait, tag) go to `runner` with exact commands, branch and SHA.
- Design goes to `designer` or `architect`, never `implementer`. Root causes the first fix didn't find, and anything an implementer failed at twice, go to `architect`.
- `reviewer` checks implementer and designer diffs over ~50 lines, or touching concurrency, security or release code, before pushing; review smaller diffs inline.
- One concern per agent: ~100 turns, a diff under ~500 lines; split big work into sequenced briefs.
- Fixes, review findings included, go back to the same agent via SendMessage.
- Parallel agents only on disjoint files.

## Context
- Filter command output to errors/tail; send long logs to a file and grep it.
- Wait on long builds, tests and CI (`gh run watch <id> --exit-status`) in the background.
- A new unrelated topic goes to a fresh session (`start_session` where available) unless it's quick.

## CI
Keep it minimal: one workflow on push to main plus manual dispatch, one job (lint, tests, build) on the cheapest runner the project allows. No scheduled runs, PR triggers or extra scans unless I ask.

## Done means verified
Run the tests/build/lint or the app. Fix failures instead of reporting them. For user-facing changes, look at the result. State plainly what you couldn't verify.

## Output
Concise, result first. End with: what changed, what was verified, anything I need to know.

@~/.claude/personal.md
