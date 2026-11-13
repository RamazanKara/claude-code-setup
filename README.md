# claude-code-setup

Shared working rules for Claude Code, Codex, OpenCode and Antigravity, optional Haiku and DeepSeek helper definitions, plus model and compaction settings. Agents work autonomously without prescribed handoffs, model tiers or review routing.

## Install

Requires Python 3.8+ and Claude Code. Run `python install.py --dry-run`, then `python install.py`.

The installer copies `claude/CLAUDE.md` into `~/.claude` (or `CLAUDE_CONFIG_DIR`), merges `claude/settings.json`, and creates `personal.md` from its example only if absent. It writes the same rules, with `personal.md` inlined, for each other agent whose config directory exists: Codex (`~/.codex/AGENTS.md` or `CODEX_HOME`), OpenCode (`~/.config/opencode/AGENTS.md`) and Antigravity (`~/.gemini/GEMINI.md`). It also merges `opencode/opencode.json` into OpenCode's global config, accepting JSONC comments and trailing commas; changed configs are written as JSON. Replaced files are backed up under `~/.claude/backups/`. Orca's Claude profiles import `~/.claude`, so the installer writes there even when run from an Orca Claude session.

The installer also copies each harness's `scout`, `runner` and `mechanic` helper definitions into its agents directory: `~/.claude/agents`, OpenCode's `agents`, `~/.codex/agents` and `~/.gemini/config/agents`. Each helper runs on that harness's own cheap model. A shared rule sends broad searches, builds, test runs, the push chore and fully specified edits to these helpers where they exist; everything else stays with the main agent.

A second rule moves volume work off the costlier models, after Spotify's [bulk-reader and code-writer routing](https://engineering.atspotify.com/2026/9/portal-by-spotify-cut-my-claude-code-token-usage-by-90): bulk reading and scaffolding go to a DeepSeek V4.1 Flash worker started through Orca's orchestration (`orca orchestration worker-start --agent opencode --model opencode-go/deepseek-v4.1-flash`), and only its report comes back. As there, editing, debugging and design stay with the main agent, which reads the exact range itself before changing it. Orca cannot pass OpenCode a reasoning level and starts its workers at `high`, so `opencode/opencode.json` sets `reasoning_effort: max` in that model's request body. That holds on every launch path and also overrides a variant picked in OpenCode; use `settings.reasoningEffort` instead for a default a variant can still lower. A third rule says what a brief must carry, after Spotify's notes on [context engineering for background coding agents](https://engineering.atspotify.com/2025/11/context-engineering-background-coding-agents-part-2): the end state and its check, the context up front, an example, when not to act, and one change per brief.

The installer also copies `claude/hooks/read_streak.py` to `~/.claude/hooks` and registers it as a `PostToolBatch` hook in `settings.json`, run by the Python that ran the installer (rerun it if that Python moves). The hook counts turns in a row that made one read-only call and, at the third and every sixth after, adds one line telling the model to batch its reads or hand the survey to a helper. It never blocks a call, skips the cheap helper agents, and `CLAUDE_READ_STREAK` sets the first nudge (0 turns it off). The reason is in our own transcripts (`tools/context_fill.py`, four days to 2026-11-13): 80 to 90% of turns made a single tool call, and streaks of three or more turns with one read-only call each were 19% of main-chat and 37% of subagent spend, while whole-file reads of over 350 lines were 2% of what fills the main context. So the hook watches turns, not file size.

Edit templates here before reinstalling. Keep machine-specific paths in `~/.claude/personal.md`, which Claude imports and the other agents get inlined; rerun the installer after editing it. Existing hooks, credentials and allow permissions are preserved. The installer removes deny permissions, the old desktop skill blocks, OpenCode helper routing and the disabled general agent override. It also removes the retired Opus helper definitions when their contents still match this setup; user replacements are preserved.

## Contents

- `claude/CLAUDE.md`: autonomy, focused changes, verification, Git landing, lean context, delegation (helpers, DeepSeek workers through Orca, briefs) and temporary-output rules.
- `claude/agents/`: Haiku scout, runner and mechanic definitions.
- `claude/hooks/read_streak.py`: the nudge after read-only turns of one call each.
- `opencode/agents/`: DeepSeek V4.1 Flash scout, runner and mechanic definitions, without deny permissions.
- `codex/agents/`: GPT-6 Luna scout, runner and mechanic definitions at medium reasoning effort.
- `antigravity/agents/`: Gemini Flash scout, runner and mechanic definitions.
- `claude/settings.json`: output, effort and compaction settings.
- `opencode/opencode.json`: DeepSeek V4.1 Flash as OpenCode's default model at maximum reasoning, and context limits for Anthropic models, Gemini 4 Argon and Kimi K3.1 Pro.
- `test_install.py`: installer migration, backup and repeat-install checks (`python -m unittest -v`).
- `tools/cost_report.py`: optional historical transcript analysis; it does not control the workflow.
- `tools/context_fill.py`: what fills Claude Code contexts (tool results by tool, shell output by command kind) and where the turns go (tool calls per turn, spend in read-only streaks).
- `tools/opencode_cost.py`: where OpenCode's Anthropic spend goes: cost split, context sizes, what fills the context, and the 5-minute vs 1-hour cache trade-off.

## License

[MIT](LICENSE)
