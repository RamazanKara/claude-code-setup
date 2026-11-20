---
name: issue-batch
description: Fix a list of issues with Orca workers on the flat-rate models - one GLM 5.3 code worker per issue, one DeepSeek builder that integrates them and runs the full gate once, one GLM review and one landing. Use when the user hands over several issues or bugs at once.
---

# Issue batch

You orchestrate: write briefs, pass reports on, land. Load Orca's guide first (`orchestration` skill) and follow its loop: start the workers, then end your turn; Orca wakes you with each `worker_done`. Never poll.

1. **Brief each issue** in a temp file: the issue in the reporter's words, the end state, the narrowest test that proves it, known paths with line ranges, and: "Write the fix and its regression test; run only that test and the formatter. Commit; never push to main. Deliver your report with worker_done: SHA, files, the test and its result, what stays unverified."
2. **Code workers**, up to 4 at once. OpenCode takes `--model` only in an existing worktree, so per issue: `orca worktree create --name <topic> --parent-worktree current --base-branch origin/main --json`, then `orca orchestration worker-start --worktree name:<topic> --agent opencode --model opencode-go/glm-5.3 --spec-file <brief>`. When GLM's budget is out, start it again there with `--model opencode-go/deepseek-v4.1-flash`; when OpenCode Go is out, with `--agent claude --model sonnet`. A bug the worker could not diagnose goes to a Claude worker.
3. **Builder**, once the first SHAs are in: `worker-start --worktree new-child --name batch-<date> --base-branch origin/main --agent opencode --spec-file <brief>` (OpenCode's default model is DeepSeek V4.1 Flash). Brief: "Cherry-pick these commits in order: <SHAs>. Run <the project's full gate>. Change no code. Deliver with worker_done the integrated SHA if green; else per failure the commit it belongs to, the error with `path:line` and the log's path."
4. **Review while it builds**, in the background: `oc.py review --dir <builder worktree> "<each issue in the reporter's words; range origin/main..HEAD>"`. Over ~500 lines, one review per issue's commits.
5. **Failures and findings** go to the issue's worker with the exact lines: `orca orchestration send --to dispatch:<id> --subject <topic> --body "<lines>"`; its fix commit goes to the builder the same way, with "cherry-pick <SHA> and rerun the gate". Review again only after a High finding.
6. **Land once**: `land.py --repo <builder worktree> --check "<the gate>"`. Remove the batch's worktrees and report per issue: landed (SHA), not landed (why), not verified.

To build on the laptop, start the builder with `--on laptop` after loading the guide's placement reference; workers then push `wip/<topic>` and the builder fetches it. A finished worker there refuses `send`: start a new one. An issue still unclear stays out; land the rest.
