# Working rules

Optimize for momentum and for my Claude usage budget.

## Autonomy
- Work fully autonomously with standing approval for everything: commit, push, tag, release, force-push, rewrite history, delete stale files/branches/worktrees/artifacts, install or remove dev tools, kill dev processes, change dev config. Never ask; pick the best option and note it in the summary.
- Commit straight to the current branch in small focused commits and push once verified. No feature branches or PRs unless I ask.
- Never commit secrets.

## Delegation
Do quick things inline: up to ~10 tool calls, or anything that needs this conversation's context. Delegate the rest to the cheapest agent whose description fits:
- Searches needing more than ~3 reads go to `scout`, never Explore.
- Builds, test and device runs, and integration chores (push, CI wait, tag) go to `runner` with exact commands, branch and SHA.
- Design goes to `designer` or `architect`, never `implementer`. Technical design (pacing, protocols, data layout), root causes the first fix didn't find, and anything an implementer failed at twice go to `architect`, unless the decision needs this conversation. Browser work goes to `designer` for design, `general-purpose` otherwise.
- `reviewer` checks implementer and designer diffs over ~50 lines, or touching protocol, concurrency, security or release code, before pushing; review smaller diffs inline.
- One concern per agent: ~100 turns, a diff under ~500 lines; split big redesigns into sequenced briefs.
- Fixes, review findings included, go back to the same agent via SendMessage. Reuse an agent for a new task only if it touches the same files.
- Parallel agents or sessions only on disjoint files.

## Context
- Filter command output to errors/tail; send long logs to a file and grep it.
- Wait on long builds, tests and CI (`gh run watch <id> --exit-status`) in the background.
- Hand off yourself, without asking: a new topic, or fresh work after a compaction, goes to `start_session` (`user_asked`, `fresh`, self-contained brief) unless it's quick; say so in one line with a link and stop. Within one task, compaction is the hand-off.

## CI (GitHub Actions minutes are tight)
One workflow on push to main + workflow_dispatch, one job: lint, unit tests, build. ubuntu-latest by default; use a Windows runner when the project needs it (2× minutes, so keep that job lean). Tag-triggered releases and Pages deploys are fine. No scans, schedules, PR triggers, e2e, or macOS runners.

## Done means verified
Run the tests/build/lint or the app. Fix failures instead of reporting them. For user-facing changes, look at the result (screenshot). State plainly what you couldn't verify.

## Output
Concise, result first. End with: what changed, what was verified, anything I need to know.

@~/.claude/personal.md
