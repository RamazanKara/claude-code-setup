---
name: issue-batch
description: Fix a list of issues with Orca workers - one code worker per issue, one cheap builder that integrates them and runs the full gate once, one review and one landing. Use when the user hands over several issues or bugs at once.
---

# Issue batch

You orchestrate: you write briefs, pass reports on and land. You do not read code or build. Load Orca's guide first (`orchestration` skill) and resolve its CLI.

1. **Brief each issue** in a file in the temp folder: the issue in the reporter's words, the end state, the narrowest test that proves it (the project's code map names it), known paths with line ranges, and this: "Work in your own worktree off origin/main. Write the fix and its regression test; run only that test and the formatter, never the full gate. Commit; do not push to main (push the branch as `wip/<topic>` only if told to). Report: branch, SHA, files, the test you ran and its result, what stays unverified."
2. **Start the code workers**, at most 4 at once: `worker-start --worktree new-child --name <topic> --spec-file <brief>`.
   - Claude (Opus) for concurrency, lifetime, protocol, decoder, release or hardware code, and for a bug that still needs diagnosis.
   - `--agent opencode --model opencode-go/glm-5.3` for a fix the brief fully specifies, tests, docs, translations and text.
3. **Build once.** When the first branches are in, start one builder: `--agent opencode --model opencode-go/glm-5.3 --worktree new-child --name batch-<date>`; on the machine `personal.md` names for heavy work when the user is at this PC (then the workers push their `wip/` branches). Its brief: "Cherry-pick these commits onto origin/main in this order: <SHA list>. Run <the project's full gate> under the project's build lock. Change no code. Report the integrated SHA if green; otherwise, per failure, the branch it belongs to (by file), the failing test or error with `path:line`, and the log's path. Keep the worktree; more commits will follow." Device or hardware measurements the batch needs run here too, once.
4. **Review while it builds**: start the `reviewer` on the builder's worktree (`Reviewed:` must name it, since the push hook counts only a review of the pushing checkout), range `origin/main..HEAD`, with each issue in its reporter's words. Over ~500 lines, one reviewer per branch's commits, all naming that worktree.
5. **Failures and findings** go to the branch's worker as a short message with the builder's or reviewer's lines; it fixes and commits; the builder cherry-picks the fix and runs the gate again. Ask for a second review only after a High finding.
6. **Land once**: `land.py --repo <builder worktree> --check "<the gate>"`. Then stop the workers, remove their worktrees and `wip/` branches, and report per issue: landed (SHA), not landed (why), not verified.

A branch whose fix is still unclear stays out of the batch; land the rest.
