# claude-code-setup

Minimal autonomous working rules for every coding agent run through Orca, Claude agent definitions, and OpenCode helper agents on DeepSeek. Orca manages sessions, workspaces and worktrees. Each harness uses its own subagents for short chores, which keep the main context small; bigger checkable jobs and cross-model reviews run as Orca workers.

## Install

Requires Python 3.8+ and Claude Code. Run `python install.py --dry-run`, then `python install.py`.

The installer copies `claude/CLAUDE.md` and `claude/agents/*.md` into `~/.claude` (or `CLAUDE_CONFIG_DIR`), merges `claude/settings.json`, and creates `personal.md` from its example only if absent. It writes the same rules, with `personal.md` inlined, for each other agent whose config directory exists: Codex (`~/.codex/AGENTS.md` or `CODEX_HOME`), OpenCode (`~/.config/opencode/AGENTS.md`) and Antigravity (`~/.gemini/GEMINI.md`). It also merges `opencode/opencode.json` into OpenCode's global config and copies `opencode/agents/*.md` into `~/.config/opencode/agents`. Replaced files are backed up under `~/.claude/backups/`. Orca's Claude profiles import `~/.claude`, so the installer writes there even when run from an Orca Claude session.

Edit templates here before reinstalling. Keep machine-specific paths in `~/.claude/personal.md`, which Claude imports and the other agents get inlined; rerun the installer after editing it. Existing hooks, permissions, credentials and agent definitions are preserved.

## Contents

- `claude/CLAUDE.md`: autonomy, focused changes, verification, Git landing, Orca worktrees, cross-agent review, lean context, delegation by context and temporary-output rules.
- `claude/agents/`: Opus architect, designer, implementer and reviewer; Haiku scout (searches), runner (builds, tests, chores) and mechanic (exact edits). Orca shows them under their Claude session.
- `claude/settings.json`: compaction settings, and hides the Claude desktop app's browser, computer-use and docs skills, which don't work outside it.
- `opencode/opencode.json`: DeepSeek V4.1 Flash as OpenCode's default model, and a shell rule that stops agents from launching Anthropic API models (`anthropic/claude*`), so delegated work never spends API credits; you can still pick Opus for your own session; Anthropic models' context limit lowered to 470k, so they compact at ~400k (matching Claude Code's main session) while DeepSeek keeps the default 85% of its 1M window; DeepSeek V4.1 Flash (OpenCode Go) for the `explore`, title and summary agents; the built-in `general` subagent turned off, because it runs on the session's model and Orca doesn't show OpenCode subagents; and the same hidden desktop skills.
- `opencode/agents/`: DeepSeek V4.1 Flash `runner` and `mechanic` subagents for OpenCode.
- `tools/cost_report.py`: optional historical transcript analysis; it does not control the workflow.
- `tools/opencode_cost.py`: where OpenCode's Anthropic spend goes: cost split, context sizes, what fills the context, and the 5-minute vs 1-hour cache trade-off.

DeepSeek Orca workers (`orca orchestration worker-start --agent opencode --model opencode-go/deepseek-v4.1-flash`) need the plain OpenCode launcher enabled in Orca's agent settings.

## License

[MIT](LICENSE)
