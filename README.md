# claude-code-setup

Minimal autonomous working rules for every coding agent run through Orca, and four optional Claude agent definitions. Delegated work runs as Orca workers, cheap ones on DeepSeek, so it stays visible in Orca. Orca manages sessions, workspaces and worktrees; the global instructions do not impose a delegation workflow.

## Install

Requires Python 3.8+ and Claude Code. Run `python install.py --dry-run`, then `python install.py`.

The installer copies `claude/CLAUDE.md` and `claude/agents/*.md` into `~/.claude` (or `CLAUDE_CONFIG_DIR`), merges `claude/settings.json`, and creates `personal.md` from its example only if absent. It writes the same rules, with `personal.md` inlined, for each other agent whose config directory exists: Codex (`~/.codex/AGENTS.md` or `CODEX_HOME`), OpenCode (`~/.config/opencode/AGENTS.md`) and Antigravity (`~/.gemini/GEMINI.md`). It also merges `opencode/opencode.json` into OpenCode's global config and removes retired helper agents (Claude's `mechanic`, `runner` and `scout`, OpenCode's `mechanic` and `runner`). Replaced and removed files are backed up under `~/.claude/backups/`. Orca's Claude profiles import `~/.claude`, so the installer writes there even when run from an Orca Claude session.

Edit templates here before reinstalling. Keep machine-specific paths in `~/.claude/personal.md`, which Claude imports and the other agents get inlined; rerun the installer after editing it. Existing hooks, permissions, credentials and agent definitions are preserved.

## Contents

- `claude/CLAUDE.md`: autonomy, focused changes, verification, Git landing, Orca worktrees, cross-agent review and temporary-output rules.
- `claude/agents/`: architect, designer, implementer and reviewer definitions (Opus), available when explicitly useful.
- `claude/settings.json`: compaction settings, and hides the Claude desktop app's browser, computer-use and docs skills, which don't work outside it.
- `opencode/opencode.json`: OpenCode's default model, DeepSeek V4.1 Flash (OpenCode Go) for its read-only `explore`, title and summary agents, the same hidden desktop skills, and the built-in `general` subagent turned off: Orca doesn't show OpenCode subagents, and `general` runs on the session's model. Delegated work goes to Orca workers instead (`orca orchestration worker-start --agent opencode --model opencode-go/deepseek-v4.1-flash` for DeepSeek), which needs the OpenCode launcher enabled in Orca's agent settings.
- `tools/cost_report.py`: optional historical transcript analysis; it does not control the workflow.

## License

[MIT](LICENSE)
