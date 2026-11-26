# claude-code-setup

Shared dual-harness working rules, hooks, agents, skills, and tools for **Claude Code** (`~/.claude/`) and **Antigravity** (`~/.gemini/config/`). Agents work autonomously toward small verified changes on `main`, keep context lean, build in shared target pools without triggering Windows Defender Firewall prompts, offload CPU or GPU work to the laptop when working on the PC, and review larger diffs before landing.

## Install

Requires Python 3.8+ and Claude Code (and optionally Antigravity `agy`). Run:

```bash
python install.py --dry-run
python install.py
```

The installer:
- Copies `claude/CLAUDE.md`, native Claude Code subagents (`scout`, `coder`, `reviewer`), dual-harness hooks, shared tools, and skills into `~/.claude/` (ignoring Orca's `CLAUDE_CONFIG_DIR` profile override).
- Merges `claude/settings.json` into `~/.claude/settings.json` (preserving Orca hooks while marking Orca telemetry hooks `async: true`, removing legacy overrides, keeping `subagentPromptCacheTtl: "1h"`, `bashOutputMaxChars: 12000`, `autoCompactWindow: 300000` for Opus/Sonnet/Fable, and `autoMemoryEnabled: false`).
- When `~/.gemini/config/` exists, writes `GEMINI.md` (from `antigravity/GEMINI.md` with `~/.claude/personal.md` inlined), merges a `"workflow-guards"` hook group into `~/.gemini/config/hooks.json` (preserving `"orca-status"` and any user groups), and copies the shared skills into `~/.gemini/config/skills/` (preserving Orca's skills).
- Retires legacy Codex files and superseded tools (`deepseek.py`, `oc.py`), backing up everything replaced under `~/.claude/backups/`.

## Key Workflows

### 1. Native Subagents First, Opt-In Cross-Harness Delegation (`worker.py`)
Both harnesses stay **native by default** (never silently crossing harnesses):
- **Claude Code**: uses native subagents (`scout` Haiku/low read-only + web, `coder` Sonnet/high, `reviewer` Opus/high read-only + web) to preserve the 1h prompt cache.
- **Antigravity**: uses Orchestrator (Neon), native `research`/`self` subagents, and `/swarm` when explicitly invoked.
- **Opt-in cross-harness** (only when explicitly requested, e.g. for an independent second opinion):
  - From Claude Code -> Antigravity Carbon review: `python ~/.claude/tools/worker.py review --harness agy --dir <checkout> "<brief>"`
  - From Antigravity -> Claude Opus 5.5 review: `python ~/.claude/tools/worker.py review --harness claude --effort xhigh --dir <checkout> "<brief>"`
  - `read` and `review` enforce read-only (`--disallowedTools` on Claude, `--mode plan` on Antigravity). A clean `review` ending with `Reviewed: <repo>` unlocks `review_gate.py` and `land.py` for either harness.

### 2. General PC -> Laptop CPU & GPU Offload (`laptop.py`)
Usable identically from Claude Code and Antigravity when working on the PC:
- `python ~/.claude/tools/laptop.py status [--json]` — checks PC & laptop locks (`BUILD-LOCK`, `LAPTOP-LOCK`, live `PRIORITY`), held lanes, console user `ramaz`, and free disk space.
- `python ~/.claude/tools/laptop.py run --branch wip/<topic> [--gpu] [--push] [--wait] [--timeout 120] -- <command>` — checks out `origin/<branch>` into a detached worktree on the laptop, runs `<command>` inside an **Interactive Scheduled Task** in `ramaz`'s desktop session (bypassing SSH Session 0 so both CPU builds/tests and GPU/WGC/e2e/soak runs work), claims `LAPTOP-LOCK` when `--gpu` is set, removes its detached worktree in `finally`, and preserves exit code `3` (AMD encoder hang sentinel — investigate, never retry).
- `python ~/.claude/tools/laptop.py check <branch> [--wait]` — wraps Rubylight's `tools\remote-check.ps1`.
- `python ~/.claude/tools/laptop.py clean [--apply]` — prunes finished detached worktrees on the laptop and prunes idle pool slots if `C:` drops below 45 GB.

### 3. Dual-Harness Guards & Landing
One set of hook scripts serves **both** Claude Code (`settings.json`) and Antigravity (`hooks.json`):
- `claude/hooks/pre_bash.py`: single-process `PreToolUse` dispatcher running `cargo_pool.py` (enforces `python tools/dev.py cargo -- <args>`, Gradle `--lane gradle --no-daemon`, and loopback-only `http.server`/`vite` binds to prevent Windows Defender Firewall prompts) + `review_gate.py` (holds unreviewed pushes to `main` >= 150 code lines).
- `claude/hooks/fresh_checkout.py`: `SessionStart` fast-forward of clean `main`/`master` checkouts (or stale-branch warning).
- `claude/hooks/read_streak.py`: Claude Code `PostToolBatch` nudge after consecutive single-call read-only turns.
- `claude/tools/land.py`: fetch, rebase, re-check if `main` moved, review gate (active in both Claude Code and Antigravity), push, and optional `--watch` CI.

### 4. Zero-Garbage Hygiene (`task.py`)
- `task.py open|add|close|sweep|daily`: per-task artifact tracking and safe teardown.
- `task.py caches [--days 3] [--apply]`: prunes idle non-pooled Cargo target directories.
- `task.py sessions [--days 14] [--gemini-days N] [--apply]`: prunes dead Orca project dirs, stale Claude transcripts (preserving `memory/` and live sessions), and optionally stale Antigravity `antigravity-cli/{brain,conversations}` entries (preserving the active conversation and locked `.db` files).

## Contents

- `claude/CLAUDE.md`: concise Claude Code working rules (kept under 3,000 bytes).
- `antigravity/GEMINI.md`: concise Antigravity working rules (inlined with `personal.md`).
- `claude/agents/{scout,coder,reviewer}.md`: native Claude Code subagents.
- `claude/hooks/{pre_bash,cargo_pool,review_gate,fresh_checkout,read_streak}.py`: dual-harness hooks.
- `claude/tools/{worker,laptop,land,task,memory_sync,update_harnesses}.py`: shared CLI tools.
- `claude/skills/{issue-batch,merge-train,public-copy}/SKILL.md`: shared skills installed for both harnesses.
- `tools/{cost_report,context_fill,user_messages}.py`: optional transcript analytics.
- `test_install.py`, `test_task.py`: full test suite (`python -m unittest -v`).

## License

[MIT](LICENSE)
