# claude-code-setup

Minimal autonomous working rules and seven optional Claude agent definitions. Orca manages sessions and workspaces; the global instructions do not impose a delegation workflow.

## Install

Requires Python 3.8+ and Claude Code. Run `python install.py --dry-run`, then `python install.py`.

The installer copies `claude/CLAUDE.md` and `claude/agents/*.md` into `~/.claude` (or `CLAUDE_CONFIG_DIR`), merges `claude/settings.json`, and creates `personal.md` from its example only if absent. Replaced files are backed up under `~/.claude/backups/`.

Edit templates here before reinstalling. Keep machine-specific paths in `~/.claude/personal.md`, which the global instructions import. Existing hooks, permissions, credentials and agent definitions are preserved.

## Contents

- `claude/CLAUDE.md`: autonomy, focused changes, verification, Git and temporary-output rules.
- `claude/agents/`: architect, designer, implementer, mechanic, reviewer, runner and scout definitions, available when explicitly useful.
- `claude/settings.json`: optional output and compaction settings.
- `tools/cost_report.py`: optional historical transcript analysis; it does not control the workflow.

## License

[MIT](LICENSE)
