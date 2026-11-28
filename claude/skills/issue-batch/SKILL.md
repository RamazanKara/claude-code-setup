---
name: issue-batch
description: Fix a list of issues with Orca workers - one code worker per issue, one builder that integrates them and runs the full gate once, one independent review and one landing. Use when the user hands over several issues or bugs at once.
---

# Issue batch

You orchestrate (Claude Code Opus or Antigravity Orchestrator): write briefs, pass reports on, land. Load Orca's guide first (`orchestration` skill) and follow its loop: start the workers, then end your turn; Orca wakes you with each `worker_done`. Never poll. Stay on your active harness by default; cross harnesses only when the user explicitly asks.

1. **Brief each issue** in a temp file: the issue in the reporter's words, the end state, the narrowest test that proves it, known paths with line ranges, and: "Write the fix and its regression test; run only that test and the formatter through the project's shared build entry (Rubylight: `python tools/dev.py cargo -- test ...`; Gradle via `build_lock.py --lane gradle -- ... --no-daemon`), never a private target dir and never wildcard `0.0.0.0` binds. Commit; never push to main. Deliver your report with worker_done: SHA, files, the test and its result, what stays unverified."
2. **Code workers**, each tightly scoped (1–3 related items, incremental commits) in its own worktree; cap concurrent local Rust/Gradle builders to ≤2–3 on one PC (up to 8 lightweight/non-building workers, or offload builds to the laptop):
   - From Claude Code: `orca orchestration worker-start --worktree new-child --name <topic> --base-branch origin/main --agent claude --model sonnet --spec-file <brief>`.
   - From Antigravity: `orca orchestration worker-start --worktree new-child --name <topic> --base-branch origin/main --agent antigravity --model gemini-4-neon-high --spec-file <brief>` (always pin Neon so workers never inherit `Orchestrator (Neon)`'s Flash executor).
3. **Builder**, once the first SHAs are in: start a worktree with `--name batch-<date>` (or offload to the laptop via `--on laptop` or `python ~/.claude/tools/laptop.py check|run` when working on the PC). Brief: "Cherry-pick these commits in order: <SHAs>. Run <the project's full gate>. Change no code. Deliver with worker_done the integrated SHA if green; else per failure the commit it belongs to, the error with `path:line` and the log's path."
4. **Review while it builds**: run one independent review of `origin/main..HEAD` in the builder worktree with each issue in the reporter's words:
   - From Claude Code: native `reviewer` agent (Opus), or opt-in cross-harness `python ~/.claude/tools/worker.py review --harness agy --dir <builder worktree> "<brief>"` only when the user asks for an Antigravity second opinion.
   - From Antigravity: `python ~/.claude/tools/worker.py review --dir <builder worktree> "<brief>"` (Antigravity Neon `gemini-4-neon-high` in plan mode; pass `--effort xhigh` for deep concurrency/protocol/release batches), or opt-in `--harness claude` only when the user asks for an Opus review.
5. **Failures and findings** go to the issue's worker with the exact lines: `orca orchestration send --to dispatch:<id> --subject <topic> --body "<lines>"`; its fix commit goes to the builder the same way, with "cherry-pick <SHA> and rerun the gate". Re-review only after a `High` finding (the push gate holds automatically until fixed and re-reviewed cleanly).
6. **Land once**: `python ~/.claude/tools/land.py --repo <builder worktree> --check "<the gate>"`. Stop the batch's workers (`orca orchestration worker-stop --dispatch <id>`), remove their worktrees (`orca worktree rm --worktree path:<path> --delete-branch`), run `python ~/.claude/tools/task.py caches` and report per issue: landed (SHA), not landed (why), not verified.

To offload builds or GPU/e2e runs to the laptop, either pass `--on laptop` to the builder (workers push `wip/<topic>` and the builder fetches it) or run `python ~/.claude/tools/laptop.py run --branch wip/<topic> [--gpu] -- <cmd>`. A finished remote worker refuses `send`: start a new one. An issue still unclear stays out; land the rest.
