# Working rules

- Work autonomously from the request through implementation and verification. Choose routine implementation details yourself; ask only when essential information or a consequential decision cannot be inferred.
- Prioritize correctness, reliability and maintainability. Inspect relevant code and existing changes, keep the scope focused, and preserve unrelated work and user data.
- Verify the requested behavior and affected existing behavior with appropriate checks. Add regression coverage where useful, review the final diff, and report anything unverified honestly.
- For repository changes, commit only the task's changes with a specific conventional message and push to remote `main` after verification, unless the user requests another workflow. Preserve unrelated work; report access or branch-protection blockers.
- Never commit secrets, force-push, rewrite shared history, open PRs, tag or publish releases unless explicitly requested.
- Put large disposable build outputs under a task-specific directory in `D:\CodexArtifacts` when available. Clean up your reproducible temporary outputs after verification; preserve deliverables, shared dependencies and active work.
- Keep communication concise: result, consequential decisions, verification and remaining limitations.

@~/.claude/personal.md
