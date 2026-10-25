# claude-code-setup

A global `CLAUDE.md`, seven subagents and a few settings that make Claude Code spend less of your usage budget without slowing you down. The main chat stays on your strongest model for judgment; the bulk work goes to agents sized for it, and Haiku does the searching, running and waiting.

Built from one heavy user's transcripts over a few weeks of daily multi-agent coding, then tuned against what the numbers showed (see [Why these rules](#why-these-rules)).

## Install

Needs Python 3.8+ and Claude Code.

```bash
git clone https://github.com/RamazanKara/claude-code-setup.git
cd claude-code-setup
python install.py --dry-run   # see what changes
python install.py --link      # or without --link to copy
```

`--link` symlinks the files into `~/.claude`, so `git pull` updates every machine (on Windows it needs Developer Mode or an elevated shell). Without it, rerun `python install.py` after pulling.

The installer:
- puts `CLAUDE.md` and `agents/*.md` into `~/.claude` (or `$CLAUDE_CONFIG_DIR`),
- merges `settings.json` into yours: your keys stay, lists such as `permissions.deny` are unioned,
- creates `~/.claude/personal.md` once from [personal.example.md](claude/personal.example.md) and never touches it again,
- backs up everything it replaces to `~/.claude/backups/claude-code-setup-<time>/`.

Then edit `~/.claude/personal.md`: your OS, where your projects live, anything only you need. `CLAUDE.md` imports it.

## What's in it

| File | What it does |
| --- | --- |
| [CLAUDE.md](claude/CLAUDE.md) | Working rules: when to do things inline vs delegate, which agent gets what, keeping context small, cheap CI, "done means verified". |
| [agents/scout.md](claude/agents/scout.md) | Haiku, read-only. Finds files, symbols and call sites; replaces the built-in Explore agent, which runs on your main model. |
| [agents/runner.md](claude/agents/runner.md) | Haiku. Runs builds, tests, benchmarks, device checks; pushes, waits on CI, tags. Never edits code. |
| [agents/mechanic.md](claude/agents/mechanic.md) | Haiku. Fully specified mechanical edits: renames, version bumps, applying an exact diff. |
| [agents/implementer.md](claude/agents/implementer.md) | Opus. Builds from a clear brief, self-reviews against the reviewer's most common findings, hands searches and runs to scout and runner. |
| [agents/designer.md](claude/agents/designer.md) | Opus. UI work where look and feel matter; designs, builds and screenshots in one pass. |
| [agents/architect.md](claude/agents/architect.md) | Opus at xhigh effort. Technical design, root causes the first fix missed, anything an implementer failed at twice. |
| [agents/reviewer.md](claude/agents/reviewer.md) | Opus at xhigh effort. Reviews diffs before push; reports findings only. |
| [settings.json](claude/settings.json) | Subagent default model, compaction windows, output cap, a few noisy tools denied. |
| [tools/cost_report.py](tools/cost_report.py) | Estimates your spend from local transcripts, split by main chat vs agent type and by token kind. |

Each agent sets `model`, `effort`, `maxTurns`, `autoCompactWindow` and a cache TTL in its frontmatter. Haiku agents cap context at 100k because Haiku prompts over 100k cost far more.

## Before you use it

- **Autonomy.** The Autonomy section gives Claude standing approval to commit, push, force-push, rewrite history and delete stale branches without asking, because every confirmation round trip costs a turn. If that's too much for you, soften it (or move your own version into `personal.md` and delete it from `CLAUDE.md`).
- **Models.** Agent frontmatter says `opus`, `sonnet` and `haiku`, so it follows whatever those aliases point at. Prices in `tools/cost_report.py` are list prices at the time of writing; edit `PRICES` if yours differ.
- **Desktop-app bits.** The hand-off rule uses `start_session` and the designer uses the built-in browser (`mcp__Claude_Browser__*`) from the Claude desktop app. In the plain CLI those lines are simply ignored.
- **Experimental frontmatter.** `experimental.cacheTtl` and `omitClaudeMd` worked when this was written but may change.

## Why these rules

From about three weeks of transcripts (one user, mostly Rust and Android work), priced at list rates:

- **Context size, not the model, drove agent cost.** Opus agents with no compaction limit ran to 300k-960k tokens of context and averaged ~$13 a run. Capping `autoCompactWindow` and keeping one concern per agent (~100 turns, diff under ~500 lines) fixed most of that.
- **Opus vs Sonnet for implementing was close to a wash.** Opus needed 6.2 turns per edit, Sonnet 8.9. With cached reads priced the same on both, Opus came out at about 0.93× Sonnet's cost per edit. Hence Opus implementers, with `CLAUDE_CODE_SUBAGENT_MODEL=sonnet` as the default for anything unnamed.
- **Reviews pay for themselves.** 86% of 123 reviews found something. The top findings: silent fallbacks or swallowed errors, unhandled edge cases, tests that pass without the fix, broken callers, stale docs. The implementer now checks for exactly these before reporting.
- **Haiku can do a lot of the work.** Exploring before the first edit, device and live checks, and waiting on CI cost ~$300 in four days on Sonnet inside implementer runs. On Haiku the same device runs cost ~$3 instead of $78.
- **The main chat is the most expensive seat.** A main-chat turn cost ~1.7× an agent turn (bigger context). ~$80 in four days went on turns that only polled CI or ran one git command, hence background `gh run watch` and runner chores. Resuming after more than an hour idle re-caches the whole context; 37 such resumes cost $49, so start a fresh session after a long break.
- **Cache TTL per agent.** Agents that wait on things (implementer, runner, designer, main chat) keep a 1h cache; short-lived ones (reviewer, scout, mechanic) use 5m so they don't pay the 1h write premium.

Run `python tools/cost_report.py --days 7` before and after to see what it does for you.

## License

[MIT](LICENSE)
