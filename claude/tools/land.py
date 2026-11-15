#!/usr/bin/env python3
"""Land the commits of a checkout on the remote's main in one call, and optionally wait for their CI.

  python land.py [--repo <checkout>] [--check "<command>"] [--skip "<reason>"] [--watch]
  python land.py --ci <sha> [--repo <checkout>]          # wait for that commit's CI only

In order: fetch origin/main; stop when the checkout has nothing beyond it; rebase onto it (never with a
stash: tracked edits must be committed first, untracked files and edits inside submodules may stay); when
the rebase brought in other people's commits, run --check in the checkout (PowerShell on Windows, sh
elsewhere) and stop if it fails; ask the review gate about the exact commit; push that commit to main.
When main moved again before the push lands, it fetches, rebases and asks again, three times at most.
It never force-pushes, and works from a branch or a detached HEAD alike.

The review gate (`hooks/review_gate.py` beside this folder) is the same judgement its PreToolUse hook
gives a `git push` in Claude Code, and writes the same log line. This command has no `git push` in its
text, so the hook itself never sees it; this tool asks instead. A gate that cannot be run holds the push.
--skip "<reason>" is the logged way past it, for a change the user wants out at once. Outside Claude Code
(no CLAUDECODE=1) there is no reviewer agent to satisfy it, so the gate is not asked and the output says so.

--watch then waits for every CI run of the pushed commit (one look a minute, as GitHub's rate limits ask),
reports each, and prints the tail of a failed job's log. A run can take longer than a foreground tool call
allows: run it in the background. `cancelled` usually means a later push to main superseded the run.

Exit codes: 0 landed (or nothing to land), 1 usage or checkout problem, 2 rebase conflict (aborted, the
checkout is as it was), 3 --check failed, 4 held by the review gate, 5 push failed, 6 CI failed.
"""
import argparse, json, os, re, subprocess, sys, tempfile, time

GATE = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), 'hooks', 'review_gate.py')
ENV = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never'}
TRIES = 3


def say(text):
    print(f'land: {text}', flush=True)


def kill_tree(proc):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        import signal
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass


def run(cmd, cwd=None, timeout=120, stdin=None):
    """(exit code, output). Output goes through a file, not a pipe: a credential helper that git leaves running would
    hold a pipe open. Past the deadline the command and everything it started are killed (exit code None)."""
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT, env=ENV,
                                    **({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt'
                                       else {'start_new_session': True}))
        except OSError as error:
            return 127, str(error)
        try:
            if stdin is not None:
                proc.stdin.write(stdin.encode('utf-8'))
                proc.stdin.close()
            code = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            code = None
        out.seek(0)
        return code, out.read().decode('utf-8', 'replace').strip()


def git(top, *args, timeout=60):
    code, out = run(['git', '-C', top, '-c', 'core.quotepath=off', *args], timeout=timeout)
    return out if code == 0 else None


def tail(text, lines=15):
    return '\n'.join(text.splitlines()[-lines:])


def local(path):
    """A path as given on a command line, Git Bash drive paths included."""
    if os.name == 'nt':
        path = re.sub(r'^/(?:mnt/)?([A-Za-z])(/|$)', lambda m: m.group(1).upper() + ':/', path)
    return os.path.abspath(os.path.expanduser(path))


def gate(top, sha, skip):
    """None when the push may go, else why it is held."""
    if os.environ.get('CLAUDECODE') != '1':
        say('review gate not asked: outside Claude Code there is no reviewer agent to satisfy it')
        return None
    if not os.path.isfile(GATE):
        return f'the review gate is missing ({GATE}), so nothing can judge this push; reinstall claude-code-setup'
    command = f'git push origin {sha}:main' + (f' # REVIEW_SKIP={skip}' if skip else '')
    event = {'cwd': top, 'tool_name': 'Bash', 'tool_input': {'command': command}, 'hook_event_name': 'PreToolUse'}
    code, out = run([sys.executable, GATE, 'push'], stdin=json.dumps(event), timeout=90)
    if code != 0:
        return f'the review gate could not judge this push (exit {code}): {tail(out, 5)}'
    if not out:
        return None
    try:
        reason = json.loads(out.splitlines()[-1])['hookSpecificOutput']['permissionDecisionReason']
    except (ValueError, KeyError, IndexError, TypeError):
        return f'the review gate answered something unreadable: {tail(out, 5)}'
    reason = re.sub(r'If the user asked for this change to go out fast, repeat the push with the comment `# REVIEW_SKIP=<reason>` '
                    r'at the end of the command; skips are logged\.',
                    'If the user asked for this change to go out fast, run land.py again with --skip "<reason>"; skips are logged.',
                    reason)
    return reason.replace('then push again', 'then run land.py again')


def check(top, command):
    shell = (['pwsh', '-NoProfile', '-Command', command] if os.name == 'nt' else ['sh', '-c', command])
    code, out = run(shell, cwd=top, timeout=None)
    if code == 127 and os.name == 'nt':
        code, out = run(['powershell', '-NoProfile', '-Command', command], cwd=top, timeout=None)
    return code == 0, out


def land(args):
    top = git(local(args.repo), 'rev-parse', '--show-toplevel')
    if not top:
        say(f'{args.repo} is not a git checkout')
        return 1
    if git(top, 'remote', 'get-url', 'origin') is None:
        say('no remote named origin')
        return 1
    gitdir = git(top, 'rev-parse', '--absolute-git-dir') or ''
    if any(os.path.exists(os.path.join(gitdir, n)) for n in ('rebase-merge', 'rebase-apply', 'MERGE_HEAD', 'CHERRY_PICK_HEAD')):
        say('a rebase, merge or cherry-pick is under way here: finish or abort it first')
        return 1
    dirty = git(top, 'status', '--porcelain', '--untracked-files=no', '--ignore-submodules=dirty')
    if dirty is None or dirty:
        say('tracked files have uncommitted edits: commit what lands, and keep other work out of this checkout '
            '(land.py never stashes)\n' + tail(dirty or '', 10))
        return 1
    for attempt in range(1, TRIES + 1):
        if git(top, 'fetch', '--quiet', '--no-tags', 'origin', 'main', timeout=120) is None:
            say('fetching origin main failed')
            return 5
        ahead = int(git(top, 'rev-list', '--count', 'origin/main..HEAD') or 0)
        if not ahead:
            say('nothing to land: HEAD has no commits beyond origin/main')
            return 0
        upstream = git(top, 'rev-parse', 'origin/main')
        moved = git(top, 'merge-base', 'HEAD', 'origin/main') != upstream
        if moved:
            code, out = run(['git', '-C', top, 'rebase', '--no-autostash', '--quiet', 'origin/main'], timeout=600)
            if code != 0:
                conflicts = git(top, 'diff', '--name-only', '--diff-filter=U') or ''
                git(top, 'rebase', '--abort')
                say(f'rebase onto origin/main stopped on a conflict; aborted, the checkout is as it was.\n'
                    f'{conflicts or tail(out, 10)}')
                return 2
            say(f'rebased {ahead} commit{"s" if ahead > 1 else ""} onto origin/main {upstream[:9]}')
            if args.check:
                say(f'main moved, so running the check: {args.check}')
                ok, out = check(top, args.check)
                if not ok:
                    say('check failed; nothing was pushed\n' + tail(out, 30))
                    return 3
        sha = git(top, 'rev-parse', 'HEAD')
        held = gate(top, sha, args.skip)
        if held:
            say('held by the review gate; nothing was pushed\n' + held)
            return 4
        code, out = run(['git', '-C', top, 'push', '--quiet', 'origin', f'{sha}:refs/heads/main'], timeout=180)
        fetched = git(top, 'fetch', '--quiet', '--no-tags', 'origin', 'main', timeout=120) is not None
        now = git(top, 'rev-parse', 'origin/main') if fetched else None
        if now and git(top, 'merge-base', '--is-ancestor', sha, now) is not None:
            files = git(top, 'diff', '--name-only', f'{upstream}..{sha}') or ''
            say(f'landed {sha[:9]} on main ({ahead} commit{"s" if ahead > 1 else ""}, {len(files.splitlines())} files)')
            return watch(top, sha, args.timeout) if args.watch else 0
        if now and now != upstream:
            say(f'main moved during the push (attempt {attempt} of {TRIES}); fetching and rebasing again')
            continue
        say('push failed\n' + tail(out, 15))
        return 5
    say(f'main kept moving; gave up after {TRIES} attempts. Run land.py again.')
    return 5


def runs(top, sha):
    # Only the runs the push started: issue and comment workflows report the same head commit.
    code, out = run(['gh', 'run', 'list', '--commit', sha, '--event', 'push', '--limit', '50',
                     '--json', 'databaseId,name,status,conclusion,url'], cwd=top, timeout=60)
    if code != 0:
        return None, tail(out, 3)
    try:
        return json.loads(out or '[]'), ''
    except ValueError:
        return None, tail(out, 3)


def watch(top, sha, minutes):
    """Wait for every CI run of `sha`; 0 when none failed, 6 when one did, 1 when CI could not be read."""
    deadline, first = time.time() + minutes * 60, time.time() + 240
    say(f'waiting for CI of {sha[:9]} (one look a minute, up to {minutes} min)')
    while True:
        found, why = runs(top, sha)
        if found is None:
            say(f'cannot read CI with gh: {why}')
            return 1
        if not found and time.time() > first:
            say('no CI run started for this commit (a docs-only change may start none)')
            return 0
        if found and all(r['status'] == 'completed' for r in found):
            break
        if time.time() > deadline:
            pending = [r['name'] for r in found if r['status'] != 'completed']
            say(f'still running after {minutes} min: {", ".join(pending)}; look again with land.py --ci {sha[:12]}')
            return 1
        time.sleep(60)
    failed = 0
    for r in sorted(found, key=lambda r: r['name']):
        verdict = r['conclusion'] or 'unknown'
        note = ' (usually superseded by a later push to main)' if verdict == 'cancelled' else ''
        say(f'CI {r["name"]}: {verdict}{note}  {r["url"]}')
        if verdict in ('failure', 'timed_out', 'startup_failure'):
            failed += 1
            code, out = run(['gh', 'run', 'view', str(r['databaseId']), '--log-failed'], cwd=top, timeout=120)
            print(tail(out, 40) if out else '  (no failed-job log)', flush=True)
    return 6 if failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--repo', default='.', help='the checkout to land from (default: the current folder)')
    ap.add_argument('--check', help='run when the rebase brought in other commits; a failure stops the landing')
    ap.add_argument('--skip', help='pass the review gate, logged with this reason (only when the user asked for speed)')
    ap.add_argument('--watch', action='store_true', help='after the push, wait for the commit\'s CI and report it')
    ap.add_argument('--ci', metavar='SHA', help='only wait for and report the CI of this commit')
    ap.add_argument('--timeout', type=int, default=60, help='minutes to wait for CI (default 60)')
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if args.ci:
        top = git(local(args.repo), 'rev-parse', '--show-toplevel')
        sha = top and git(top, 'rev-parse', '--verify', '--quiet', args.ci + '^{commit}')
        if not sha:
            say(f'{args.ci} is not a commit in {args.repo}')
            return 1
        return watch(top, sha, args.timeout)
    return land(args)


if __name__ == '__main__':
    sys.exit(main())
