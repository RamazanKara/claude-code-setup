# claude-code-setup

A global `CLAUDE.md`, seven subagents and a few settings that make Claude Code spend less of your usage budget without slowing you down. The main chat keeps your strongest model for judgment; bulk work goes to agents sized for it, and Haiku does the searching, running and waiting.

The rules come from measuring one heavy user's transcripts over a few weeks of daily multi-agent coding (see [Why these rules](#why-these-rules)).

## Install

Needs Python 3.8+ and Claude Code.

```bash
git clone https://github.com/RamazanKara/claude-code-setup.git
cd claude-code-setup
python install.py --dry-run   # see what changes
python install.py
```

To update, `git pull` and run `python install.py` again.

The installer:
- copies `CLAUDE.md` and `agents/*.md` into `~/.claude` (or `$CLAUDE_CONFIG_DIR`),
- merges `settings.json` into yours: your keys stay, lists such as `permissions.deny` are unioned,
- creates `~/.claude/personal.md` once from [personal.example.md](claude/personal.example.md) and never touches it again,
- backs up everything it replaces to `~/.claude/backups/claude-code-setup-<time>/`.

Put machine- and person-specific rules (OS, where projects live, extra permissions) in `~/.claude/personal.md`; `CLAUDE.md` imports it.

## What's in it

| File | What it does |
| --- | --- |
| [CLAUDE.md](claude/CLAUDE.md) | When to work inline vs delegate, which agent gets what, keeping context small, minimal CI, "done means verified". |
| [agents/scout.md](claude/agents/scout.md) | Haiku, read-only. Finds files, symbols and call sites; replaces the built-in Explore agent, which runs on your main model. |
| [agents/runner.md](claude/agents/runner.md) | Haiku. Runs builds, tests and scripts; pushes, waits on CI, tags. Never edits code. |
| [agents/mechanic.md](claude/agents/mechanic.md) | Haiku. Fully specified mechanical edits: renames, version bumps, applying an exact diff. |
| [agents/implementer.md](claude/agents/implementer.md) | Opus. Builds from a clear brief, self-reviews against reviewers' most common findings. |
| [agents/designer.md](claude/agents/designer.md) | Opus. UI work where look and feel matter; designs, builds and screenshots in one pass. |
| [agents/architect.md](claude/agents/architect.md) | Opus at xhigh effort. Design, root causes the first fix missed, anything an implementer failed at twice. |
| [agents/reviewer.md](claude/agents/reviewer.md) | Opus at xhigh effort. Reviews diffs before push; reports findings only. |
| [settings.json](claude/settings.json) | Subagent default model, compaction windows, output cap, a few rarely used tools denied. |
| [tools/cost_report.py](tools/cost_report.py) | Estimates your spend from local transcripts, by main chat vs agent type and by token kind. |

## Before you use it

- **Autonomy.** `CLAUDE.md` lets Claude commit, push and clean up without asking, and commit straight to the current branch. Every confirmation costs a turn. If you want feature branches or PRs, or more freedom (force-push, history rewrites), say so in `personal.md`.
- **Desktop app.** `start_session` and the designer's built-in browser exist only in the Claude desktop app; the CLI ignores them.
- **Experimental frontmatter.** `experimental.cacheTtl` and `omitClaudeMd` worked when this was written but may change.

## Why these rules

From transcripts priced at list rates:

- **Context size, not the model, drove agent cost.** Agents with no compaction limit ran to 300k-960k tokens of context and averaged ~$13 a run. Capping `autoCompactWindow` and keeping one concern per agent (~100 turns, diff under ~500 lines) fixed most of that. Haiku agents cap at 100k because Haiku prompts over 100k cost far more.
- **Opus vs Sonnet for implementing was close to a wash.** Opus needed 6.2 turns per edit, Sonnet 8.9; with cached reads priced the same, Opus came out at about 0.93× Sonnet's cost per edit.
- **Reviews pay for themselves.** 86% of 123 reviews found something. The top findings (swallowed errors, edge cases, tests that pass without the fix, broken callers, stale docs) are now the implementer's self-review checklist.
- **Haiku can do a lot of the work.** Searching, running checks and waiting on CI inside implementer runs cost ~$300 in four days on Sonnet; the same runs on Haiku cost a few percent of that.
- **The main chat is the most expensive seat.** A main-chat turn cost ~1.7× an agent turn. ~$80 in four days went on turns that only polled CI or ran one git command, hence background `gh run watch` and the runner. Resuming after more than an hour idle re-caches the whole context, so start a fresh session after a long break.
- **Cache TTL per agent.** Agents that wait (implementer, runner, designer) keep a 1h cache; short-lived ones (reviewer, scout, mechanic, architect) use 5m to skip the 1h write premium.

Run `python tools/cost_report.py --days 7` before and after to see what it does for you.

## License

[MIT](LICENSE)
