# Working rules

Aim for momentum: small verified changes landed on main, in few turns and little context.

- Work autonomously; ask only when a consequential decision cannot be inferred. Check each premise against the code (`origin/main` after a fetch), a measurement or its source. Keep the scope focused; preserve unrelated work.
- You decide, write fixes up to ~60 lines and land. The rest goes to DeepSeek V4.1 Flash (never Pro) on OpenCode Go, which is cheap: `read` for a survey that would read more than two or three files and for long logs (a quick search stays yours), `write` for scaffolding, translation and fully specified edits, `code` for larger fixes, `review` for review. In Claude Code, Codex and Gemini run `python ~/.claude/tools/oc.py <mode> --dir <checkout> "<brief>"` in the background (up to ~25 min); it prints only the report, and Claude models take over when Go's limit is reached. In OpenCode use the subagents `scout` (read), `mechanic` (write), `coder` (code), `reviewer` (review) and `runner` (builds and tests). When the user asks for Claude only, or `OC_MODEL=claude` is set, pass `--model claude` and start Claude workers. A worker does its own work.
- Brief a worker with the end state, the check that proves it, paths with line ranges and when to stop. Check what it reports before relying on it.
- Verify with the narrowest fitting check, add a regression test where useful, review your diff, and say what stayed unverified.
- Before landing a change over ~150 code lines or one in concurrency, lifetime, protocol, decoder or release code, have it reviewed with the request in the user's words; in Claude Code a hook holds an unreviewed push to main. Fix the findings; review again only after a High one.
- Commit only the task's changes with a specific conventional message, then land: `python "$HOME/.claude/tools/land.py" --repo <checkout> --check "<affected checks>"`. Never commit secrets, force-push, rewrite shared history, open PRs, tag or publish unless asked.
- One issue per chat; stop once it has landed and been reported. A list of issues goes through the `issue-batch` skill.
- Lean context: batch independent reads in one turn, read only the ranges needed, filter output to errors and tails, wait on builds and CI in the background.
- Scratch files go in the temp folder, large build outputs below the artifacts root `personal.md` names; delete what a task made.
- Report concisely: result, decisions, verification, limits; name each part of the request as done, not done (why) or not verified.

@~/.claude/personal.md
