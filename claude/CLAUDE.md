# Working rules

Aim for momentum: small verified changes landed on main, in few turns and little context.

- Work autonomously; ask only when essential information or a consequential decision cannot be inferred.
- Check each premise against the code (`origin/main` after a fetch when the checkout may be behind), a measurement or its source. Keep the scope focused; preserve unrelated work and user data.
- Verify the requested and affected behaviour with fitting checks, add a regression test where useful, review your diff, and say what stayed unverified.
- Commit only the task's changes with a specific conventional message and land them on remote `main`: `python "$HOME/.claude/tools/land.py" --repo <checkout> --check "<affected checks>"` rebases, checks when the rebase brought changes, and pushes (without it: fetch, rebase onto `origin/main`, re-check, `git push origin HEAD:main`). Commit and push in separate commands.
- Never commit secrets, force-push, rewrite shared history, open PRs, tag or publish releases unless asked.
- One issue per chat: once it has landed and been reported, stop. For a list of issues, orchestrate with the `issue-batch` skill: one Orca code worker per issue commits its fix, one cheap builder runs the gate over all of them, and the batch lands once.
- Few turns, lean context: every turn re-reads all of it. Batch independent reads and searches in one turn, read only the ranges needed, filter output to errors and tails, send long logs to a file, and wait on builds and CI in the background.
- Write a fix of up to about 60 lines yourself, and keep your context for the change: a search over more than two or three files goes to `scout`, a fully specified edit to `mechanic`, a long build or test run to `runner`, a survey, translation or scaffolding to `python ~/.claude/tools/deepseek.py read|write --model glm` (GLM 5.3 and DeepSeek run on a flat subscription). Start a coding agent only for work beside yours on other files, never on a model costlier than Opus. Brief any agent with the end state, the check that proves it, paths with line ranges and when to stop; ask for a short report with `path:line` evidence.
- Have the `reviewer` agent check a change of over about 150 code lines, or a smaller one in concurrency, lifetime, protocol, decoder or release code, before landing: give it the repository, the range since `origin/main`, the user's request in their words and any vendor SDK's path; fix its findings and ask again only after a High one. A hook holds an unreviewed push to `main`; if the user asked for speed, pass `land.py --skip "<reason>"`.
- Scratch files go in the system temp folder, large build outputs below the artifacts root `personal.md` names; delete what a task made when it ends.
- Report concisely: result, decisions, verification, limits; name each part of the request as done, not done (why) or not verified.

@~/.claude/personal.md
