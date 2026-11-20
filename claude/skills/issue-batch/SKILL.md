---
name: issue-batch
description: Fix a list of issues with Orca workers on the flat-rate models - one GLM 5.3 code worker per issue, one DeepSeek builder that integrates them and runs the full gate once, one GLM review and one landing. Use when the user hands over several issues or bugs at once.
---

# Issue batch

You orchestrate: write briefs, pass reports on, land. Load Orca's guide first (`orchestration` skill).

1. **Brief each issue** in a temp file: the issue in the reporter's words, the end state, the narrowest test that proves it, known paths with line ranges, and: "Work in your own worktree off origin/main. Write the fix and its regression test; run only that test and the formatter. Commit; never push to main. Report: SHA, files, the test and its result, what stays unverified."
2. **Code workers**, up to 4 at once: `worker-start --agent opencode --model opencode-go/glm-5.3 --worktree new-child --name <topic> --spec-file <brief>`. When GLM's budget is out, restart with `--model opencode-go/deepseek-v4.1-flash`; when OpenCode Go is out, `--agent claude --model sonnet`. A bug the worker could not diagnose goes to a Claude worker.
3. **Builder**, once the first SHAs are in: `--agent opencode --model opencode-go/deepseek-v4.1-flash --worktree new-child --name batch-<date>`. Brief: "Cherry-pick these commits onto origin/main in order: <SHAs>. Run <the project's full gate>. Change no code. Report the integrated SHA if green; else per failure the branch it belongs to, the error with `path:line` and the log's path. Keep the worktree; more commits follow."
4. **Review while it builds**: `oc.py review --dir <builder worktree> "<each issue in the reporter's words; range origin/main..HEAD>"`. Over ~500 lines, one review per branch's commits.
5. **Failures and findings** go to the branch's worker with the exact lines; it commits a fix; the builder picks it and reruns the gate. Review again only after a High finding.
6. **Land once**: `land.py --repo <builder worktree> --check "<the gate>"`. Stop the workers, remove their worktrees, report per issue: landed (SHA), not landed (why), not verified.

An issue still unclear stays out; land the rest.
