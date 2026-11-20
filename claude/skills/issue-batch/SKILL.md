---
name: issue-batch
description: Fix a list of issues with Orca workers on DeepSeek V4.1 Flash - one code worker per issue, one builder that integrates them and runs the full gate once, one review (Opus from Claude Code) and one landing. Use when the user hands over several issues or bugs at once.
---

# Issue batch

You orchestrate (Claude Code, or GLM 5.3 or DeepSeek V4.1 Flash in OpenCode when Claude's usage is spent): write briefs, pass reports on, land. Load Orca's guide first (`orchestration` skill) and follow its loop: start the workers, then end your turn; Orca wakes you with each `worker_done`. Never poll.

1. **Brief each issue** in a temp file: the issue in the reporter's words, the end state, the narrowest test that proves it, known paths with line ranges, and: "Write the fix and its regression test; run only that test and the formatter. Commit; never push to main. Deliver your report with worker_done: SHA, files, the test and its result, what stays unverified."
2. **Code workers**, up to 8 at once: `orca orchestration worker-start --worktree new-child --name <topic> --base-branch origin/main --agent opencode2 --spec-file <brief>`. Orca takes no `--model` or `--effort` for OpenCode, so the worker runs `opencode.json`'s default: DeepSeek V4.1 Flash at max effort. From Claude Code: when OpenCode Go's limit is reached, start it again with `--agent claude --model sonnet`, and a bug the worker could not diagnose goes to a Claude worker.
3. **Builder**, once the first SHAs are in: the same command with `--name batch-<date>`. Brief: "Cherry-pick these commits in order: <SHAs>. Run <the project's full gate> through the `runner` subagent (low effort). Change no code. Deliver with worker_done the integrated SHA if green; else per failure the commit it belongs to, the error with `path:line` and the log's path."
4. **Review while it builds**: the `reviewer` agent (Opus) reviews the whole batch at once, in the background: the builder worktree's path, the range origin/main..HEAD and each issue in the reporter's words. An OpenCode orchestrator uses its `reviewer` subagent instead and starts no Claude workers.
5. **Failures and findings** go to the issue's worker with the exact lines: `orca orchestration send --to dispatch:<id> --subject <topic> --body "<lines>"`; its fix commit goes to the builder the same way, with "cherry-pick <SHA> and rerun the gate". Review again only after a High finding.
6. **Land once**: `land.py --repo <builder worktree> --check "<the gate>"`. Remove the batch's worktrees and report per issue: landed (SHA), not landed (why), not verified.

To build on the laptop, start the builder with `--on laptop` after loading the guide's placement reference; workers then push `wip/<topic>` and the builder fetches it. A finished worker there refuses `send`: start a new one. An issue still unclear stays out; land the rest.
