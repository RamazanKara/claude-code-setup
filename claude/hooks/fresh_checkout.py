#!/usr/bin/env python3
"""SessionStart hook: bring the session's checkout up to its remote main before the session works in it.

A checkout that stays on main while the work lands from worktrees falls behind. A session started in it
reads old rules (AGENTS.md, CLAUDE.md) and searches old code: one told the user that a feature released
the day before was not ours, and sessions kept loading rules the user had withdrawn a day earlier.

Claude Code loads those files before SessionStart hooks run (probed 2026-11-15), so a session that this
hook brings up to date is told which of them changed; the next session loads them current.

When the checkout is on main or master, tracks a remote branch, holds no commits of its own, no edits to
tracked files (edits inside a submodule do not count) and no merge, rebase or cherry-pick under way, this
fetches that branch and fast-forwards to it. Untracked files stay. Otherwise, when it is behind, the
session is told in one line to read the remote branch instead. It never merges, stashes or resets.

  CLAUDE_FRESH_CHECKOUT=0   turns it off

Never an error: a failure (no network, no git) stays silent.
"""
import json, os, subprocess, sys

BRANCHES = ('main', 'master')
UNDER_WAY = ('MERGE_HEAD', 'rebase-merge', 'rebase-apply', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'BISECT_LOG')
RULES = ('AGENTS.md', 'CLAUDE.md')
ENV = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never', 'GIT_ASKPASS': '', 'SSH_ASKPASS': ''}


def git(top, *args, timeout=None):
    try:
        done = subprocess.run(['git', '-C', top, *args], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, env=ENV, encoding='utf-8', errors='replace')
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def say(text):
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': text}}))


def main():
    if os.environ.get('CLAUDE_FRESH_CHECKOUT', '1').strip() == '0':
        return
    try:
        event = json.load(sys.stdin)
    except ValueError:
        event = {}
    if not isinstance(event, dict) or event.get('source') == 'compact':
        return
    top = git(event.get('cwd') or os.getcwd(), 'rev-parse', '--show-toplevel', timeout=5)
    branch = top and git(top, 'symbolic-ref', '--quiet', '--short', 'HEAD', timeout=5)
    if branch not in BRANCHES:
        return
    upstream = git(top, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}', timeout=5)
    remote = upstream and git(top, 'config', f'branch.{branch}.remote', timeout=5)
    if not upstream or not remote or not upstream.startswith(remote + '/'):
        return
    if git(top, 'fetch', '--quiet', '--no-tags', remote, upstream[len(remote) + 1:], timeout=10) is None:
        return
    counts = git(top, 'rev-list', '--left-right', '--count', f'HEAD...{upstream}', timeout=5)
    try:
        ahead, behind = map(int, counts.split())
    except (AttributeError, ValueError):
        return
    if not behind:
        return
    gitdir = git(top, 'rev-parse', '--absolute-git-dir', timeout=5) or ''
    dirty = git(top, 'status', '--porcelain', '--untracked-files=no', '--ignore-submodules=dirty', timeout=20)
    why = ('it holds commits of its own' if ahead else
           'a merge, rebase or cherry-pick is under way' if any(os.path.exists(os.path.join(gitdir, n)) for n in UNDER_WAY) else
           'it has edits to tracked files' if dirty else
           'its state could not be read' if dirty is None else None)
    stale = (f'Its files, AGENTS.md and CLAUDE.md included, are older than {upstream}: read and search that '
             f'(`git grep <text> {upstream}`, `git show {upstream}:<path>`) before saying what main holds.')
    if why:
        return say(f'This checkout ({top}, {branch}) is {behind} commits behind {upstream} and was left as it is '
                   f'because {why}. {stale}')
    old = git(top, 'rev-parse', 'HEAD', timeout=5)
    if git(top, 'merge', '--ff-only', '--quiet', upstream) is None:  # no timeout: never stop a checkout half-way
        return say(f'This checkout ({top}, {branch}) is {behind} commits behind {upstream} and the fast-forward failed. {stale}')
    changed = [p for p in (git(top, 'diff', '--name-only', old, 'HEAD', timeout=5) or '').splitlines()
               if p.rsplit('/', 1)[-1] in RULES]
    say(f'This checkout ({top}) was {behind} commits behind {upstream} and was fast-forwarded to it at session start.'
        + (f' Changed with it: {", ".join(changed)}. The copy in your context was loaded before this update: read the '
           'file again before relying on its rules.' if changed else ''))


if __name__ == '__main__':
    try:
        main()
    except Exception:  # a hook that breaks must not stop a session from starting
        pass
