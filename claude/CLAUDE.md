# Working rules

- Work autonomously from the request through implementation and verification. Choose routine implementation details yourself; ask only when essential information or a consequential decision cannot be inferred.
- Prioritize correctness, reliability and maintainability. Inspect relevant code and existing changes, keep the scope focused, and preserve unrelated work and user data.
- Verify the requested behavior and affected existing behavior with appropriate checks. Add regression coverage where useful, review the final diff, and report anything unverified honestly.
- For repository changes, commit only the task's changes with a specific conventional message, then land them on remote `main` unless the user requests another workflow: fetch, rebase onto `origin/main`, re-run affected checks if the rebase brought in changes, and `git push origin HEAD:main`. This applies on Orca worktree branches too. Preserve unrelated work; report access or branch-protection blockers.
- Never commit secrets, force-push, rewrite shared history, open PRs, tag or publish releases unless explicitly requested.
- Orca manages workspaces, worktrees and agent sessions. Start separate parallel work in a new Orca worktree (`orca worktree create`), not with `git worktree`, extra clones or an agent's built-in worktree feature.
- Before pushing a diff over ~200 lines or one touching concurrency, security or release code, get a review from an agent of a different model family through Orca and address its findings.
- Write scratch files (commit messages, helper scripts, logs) to the system temp directory or the task's artifact directory, never beside repositories; pass commit messages with `git commit -F -`. Put large disposable build outputs under a task-specific directory in `D:\CodexArtifacts` when available. Clean up your reproducible temporary outputs after verification; preserve deliverables, shared dependencies and active work.
- Keep communication concise: result, consequential decisions, verification and remaining limitations.

@~/.claude/personal.md
