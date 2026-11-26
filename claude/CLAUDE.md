# Working rules

Aim for momentum: small verified changes landed on main, in few turns and lean context.

- Work autonomously; ask only when a consequential decision cannot be inferred. Check each premise against `origin/main` (after a fetch), a measurement, or official docs (`WebSearch`/`WebFetch` when a device is not local). Keep scope focused; preserve unrelated work.
- Tune `/effort` to the task: `low` (git/CI/lookups), `medium` (small fixes/docs), `high` (multi-file work/reviews), `xhigh` (hard bugs/architecture/deep reviews).
- You decide, write fixes up to ~60 lines, and land. Delegate the rest to native subagents first (keeps the 1h prompt cache): `scout` (Haiku, read-only surveys >2-3 files or long logs), `coder` (Sonnet, larger fixes/scaffolding/tests), `reviewer` (Opus, independent review). Brief with end state, narrowest check, paths/lines and stop condition; verify reports before relying on them.
- Cross-harness opt-in (never by default): only when the user explicitly asks for an Antigravity review or task, run `python ~/.claude/tools/worker.py <read|write|code|review> --harness agy --dir <checkout> "<brief>"` (Carbon in plan mode for `review`, Neon for `code`; a clean `review` unlocks the push gate).
- Laptop offload: when working on the PC and offloading CPU or GPU builds, tests or hardware runs, use `python ~/.claude/tools/laptop.py status|run|check|clean` (respects locks, runs in the Interactive desktop session, cleans its detached worktree; exit 3 is an AMD hang — investigate, never retry).
- Verify with the narrowest check, add a regression test where useful, review your diff, and say what stayed unverified.
- Before landing >150 code lines or concurrency, lifetime, protocol, decoder or release changes, have `reviewer` check the diff against the user's request; a hook holds unreviewed pushes to main. Fix findings; re-review only after a High one.
- Commit only the task's changes with a conventional message, then land: `python "$HOME/.claude/tools/land.py" --repo <checkout> --check "<affected checks>"`. Never commit secrets, force-push, rewrite shared history, open PRs, tag or publish unless asked.
- One issue per chat; stop once landed and reported. Lists of issues use the `issue-batch` skill.
- Lean context: batch independent reads in one turn, read only needed ranges, filter output to errors/tails, run long builds/CI in the background.
- Zero garbage, zero firewall prompts: build in the shared pool (Rubylight: `python tools/dev.py cargo -- <args>`, Gradle via `build_lock.py --lane gradle -- ... --no-daemon`), bind test/dev servers to `127.0.0.1`/`[::1]`, keep scratch in temp, and clean temp dirs, stop workers and remove landed worktrees before reporting (`task.py caches|sessions`).
- Report concisely: result, decisions, verification, limits; mark each item done, not done (why) or not verified.

@~/.claude/personal.md
