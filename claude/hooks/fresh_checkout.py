#!/usr/bin/env python3
"""SessionStart hook: bring the session's checkout up to its remote main before the session works in it.

A checkout that stays on main while the work lands from worktrees falls behind. A session started in it
reads old rules (AGENTS.md, CLAUDE.md) and searches old code: one told the user that a feature released
the day before was not ours, and sessions kept loading rules the user had withdrawn a day earlier.
Claude Code loads those files before SessionStart hooks run (probed 2026-11-15), so the session this hook
updates is told which of them changed; the next session loads them current.

It fetches the branch the checkout tracks and fast-forwards only when all of this holds: the checkout is
on main or master, holds no commits of its own, no edits to tracked files and no merge, rebase or
cherry-pick under way, and no other live Claude Code session works in it (one may be building or
releasing from it; the sessions folder of the config directory says which run where). Untracked files
stay, ignored ones too (`--no-overwrite-ignore`), and submodules are not touched: moved ones are named.
Otherwise, when the checkout is behind, the session is told so in one line, with the rule files that
changed upstream and how to read them. It never merges, stashes or resets.

  CLAUDE_FRESH_CHECKOUT=0   turns it off

Never an error: a failure (no network, no git) stays silent. The fetch gets 15 s and is killed with
everything it started after that, so a session never waits on a stalled remote.
"""
import glob, json, os, subprocess, sys, tempfile

BRANCHES = ('main', 'master')
UNDER_WAY = ('MERGE_HEAD', 'rebase-merge', 'rebase-apply', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'BISECT_LOG')
RULES = ('AGENTS.md', 'CLAUDE.md')
QUIET = ('-c', 'gc.auto=0', '-c', 'maintenance.auto=false')  # no background gc started by this hook
ENV = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never', 'GIT_ASKPASS': '', 'SSH_ASKPASS': ''}
ENV.setdefault('GIT_SSH_COMMAND', 'ssh -o BatchMode=yes')


def git(top, *args, timeout=10):
    """Output of a git command, or None. Output goes through a file, not a pipe: a process git starts and
    leaves running (a credential helper, a daemon) would hold a pipe open and the wait with it."""
    with tempfile.TemporaryFile() as out:
        try:
            done = subprocess.run(['git', '-C', top, *QUIET, *args], stdin=subprocess.DEVNULL, stdout=out,
                                  stderr=subprocess.DEVNULL, env=ENV, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        out.seek(0)
        return out.read().decode('utf-8', 'replace').strip() if done.returncode == 0 else None


def fetch(top, remote, ref, timeout=15):
    """Fetch one branch; past the deadline the fetch and everything it started are killed."""
    windows = os.name == 'nt'
    try:
        proc = subprocess.Popen(['git', '-C', top, *QUIET, 'fetch', '--quiet', '--no-tags', '--no-recurse-submodules', remote, ref],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENV,
                                **({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if windows else {'start_new_session': True}))
    except OSError:
        return False
    try:
        return proc.wait(timeout) == 0
    except subprocess.TimeoutExpired:
        if windows:
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            import signal
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        return False


def alive(pid):
    if os.name == 'nt':  # os.kill(pid, 0) would end the process on Windows
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.GetLastError() == 5  # access denied: it exists
        code = ctypes.c_ulong()
        try:
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def others_in(top, me):
    """Live sessions other than this one whose folder lies in the checkout; None when that cannot be known."""
    folder = os.path.join(os.environ.get('CLAUDE_CONFIG_DIR') or os.path.expanduser('~/.claude'), 'sessions')
    if not os.path.isdir(folder):
        return None
    root, found = os.path.normcase(os.path.normpath(top)), 0
    for path in glob.glob(os.path.join(folder, '*.json')):
        try:
            with open(path, encoding='utf-8') as f:
                entry = json.load(f)
            cwd = os.path.normcase(os.path.normpath(entry['cwd']))
            pid = int(entry['pid'])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if entry.get('sessionId') == me or pid == os.getppid():
            continue
        if (cwd == root or cwd.startswith(root + os.sep)) and alive(pid):
            found += 1
    return found


def say(text):
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': text}}))


def rules_in(top, old, new):
    return [p for p in (git(top, 'diff', '--name-only', old, new) or '').splitlines() if p.rsplit('/', 1)[-1] in RULES]


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
    if not upstream or not remote or not upstream.startswith(remote + '/') or not fetch(top, remote, upstream[len(remote) + 1:]):
        return
    try:
        ahead, behind = map(int, git(top, 'rev-list', '--left-right', '--count', f'HEAD...{upstream}', timeout=5).split())
    except (AttributeError, ValueError):
        return
    if not behind:
        return
    gitdir = git(top, 'rev-parse', '--absolute-git-dir', timeout=5) or ''
    dirty = git(top, 'status', '--porcelain', '--untracked-files=no', '--ignore-submodules=all', timeout=15)
    others = others_in(top, event.get('session_id'))
    why = ('it holds commits of its own' if ahead else
           'a merge, rebase or cherry-pick is under way' if any(os.path.exists(os.path.join(gitdir, n)) for n in UNDER_WAY) else
           'it has edits to tracked files' if dirty else
           'its state could not be read' if dirty is None else
           'live sessions could not be listed' if others is None else
           f'{others} other live session{"s work" if others > 1 else " works"} in it' if others else None)
    stale = (f'Its files are older than {upstream}: read and search that (`git grep <text> {upstream}`, '
             f'`git show {upstream}:<path>`) before saying what main holds.')
    if why:
        changed = rules_in(top, 'HEAD', upstream)
        return say(f'This checkout ({top}, {branch}) is {behind} commits behind {upstream} and was left as it is because {why}. '
                   + stale + (f' Rule files changed upstream: {", ".join(changed)}; read them with `git show {upstream}:<path>` '
                              'before relying on the copy in your context.' if changed else ''))
    old = git(top, 'rev-parse', 'HEAD', timeout=5)
    if git(top, 'merge', '--ff-only', '--no-overwrite-ignore', '--quiet', upstream, timeout=40) is None:
        left = git(top, 'status', '--porcelain', '--untracked-files=no', '--ignore-submodules=all', timeout=10)
        return say(f'This checkout ({top}, {branch}) is {behind} commits behind {upstream} and the fast-forward failed'
                   + (' part-way: tracked files now differ from HEAD (`git status`); a file another program holds open is the '
                      'usual cause.' if left else '.') + ' ' + stale)
    moved = [line.split('\t', 1)[-1] for line in (git(top, 'diff', '--raw', old, 'HEAD') or '').splitlines()
             if line.startswith(':160000') or ' 160000 ' in line]
    changed = rules_in(top, old, 'HEAD')
    say(f'This checkout ({top}) was {behind} commits behind {upstream} and was fast-forwarded to it at session start.'
        + (f' Changed with it: {", ".join(changed)}. The copy in your context was loaded before this update: read the '
           'file again before relying on its rules.' if changed else '')
        + (f' Submodules moved and were not updated: {", ".join(moved)} (`git submodule update --init <path>` before '
           'building).' if moved else ''))


if __name__ == '__main__':
    try:
        main()
    except Exception:  # a hook that breaks must not stop a session from starting
        pass
