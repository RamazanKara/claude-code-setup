# claude-code-setup

Minimal autonomous working rules for every coding agent run through Orca, and seven optional Claude agent definitions. Orca manages sessions, workspaces and worktrees; the global instructions do not impose a delegation workflow.

## Install

Requires Python 3.8+ and Claude Code. Run `python install.py --dry-run`, then `python install.py`.

The installer copies `claude/CLAUDE.md` and `claude/agents/*.md` into `~/.claude` (or `CLAUDE_CONFIG_DIR`), merges `claude/settings.json`, and creates `personal.md` from its example only if absent. It writes the same rules, with `personal.md` inlined, for each other agent whose config directory exists: Codex (`~/.codex/AGENTS.md` or `CODEX_HOME`), OpenCode (`~/.config/opencode/AGENTS.md`) and Antigravity (`~/.gemini/GEMINI.md`). Replaced files are backed up under `~/.claude/backups/`.

Edit templates here before reinstalling. Keep machine-specific paths in `~/.claude/personal.md`, which Claude imports and the other agents get inlined; rerun the installer after editing it. Existing hooks, permissions, credentials and agent definitions are preserved.

## Contents

- `claude/CLAUDE.md`: autonomy, focused changes, verification, Git landing, Orca worktrees, cross-agent review and temporary-output rules.
- `claude/agents/`: architect, designer, implementer, mechanic, reviewer, runner and scout definitions, available when explicitly useful.
- `claude/settings.json`: optional output and compaction settings.
- `tools/cost_report.py`: optional historical transcript analysis; it does not control the workflow.

## License

[MIT](LICENSE)
