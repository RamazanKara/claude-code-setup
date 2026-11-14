#!/usr/bin/env python3
"""Hold back a push to main that carries code no independent review has seen.

  review_gate.py push       PreToolUse hook on the shell tools
  review_gate.py reviewed   SubagentStop hook on the `reviewer` agent

A push of fewer than CLAUDE_REVIEW_MIN_LINES changed code lines (default 60; 0 turns the
gate off) passes, and so does prose. Above that the push needs a note that the reviewer
agent finished in this checkout, which it says with a last line `Reviewed: <repository>`.
The note lists the files it saw and their changed lines, and covers later changes of up
to 150 lines and no more than it saw, so fixing its findings needs no second review. A
note is spent once a push it let through has landed, and after a day. A command that
carries REVIEW_SKIP=<reason>, as a trailing `# REVIEW_SKIP=<reason>` comment in any shell,
passes and is logged, for a change the user wants out fast.

Every decision is appended to ~/.claude/review-gate.log. The hook reads only git; on any
error it lets the push through.
"""
import hashlib, json, os, re, subprocess, sys, tempfile, time

FIX_LINES = 150
MAX_AGE = 24 * 3600
HELPERS = {'scout', 'runner', 'mechanic'}
PROSE = re.compile(r'\.(md|txt|svg|png|jpe?g|gif|webp|lock)$|(^|/)(docs?|notes\.d|licenses)/|/values-[^/]+/strings\.xml$', re.I)
QUOTED = r'(?:"([^"]+)"|\'([^\']+)\'|([^\s;&|)]+))'
START = r'(?:^|[;&|\n({]|\b(?:then|do|else)\s)\s*'  # where a command begins, so text inside a commit message is no push
PUSH = re.compile(START + r'(?:[A-Za-z_]\w*=\S*\s+)*git(?:\.exe)?(?P<options>(?:\s+-[cC]\s+' + QUOTED +
                  r'|\s+--?[\w-]+(?:=\S+)?)*)\s+push\b(?P<rest>[^|;&\n]*)', re.I)
MOVE = re.compile(START + r'(?:cd|Set-Location|sl|pushd)\s+(?:-(?:Literal)?Path\s+)?' + QUOTED, re.I)
SKIP = re.compile(r'REVIEW_SKIP\s*[=:]\s*[\'"]?([^\'";\n]+)', re.I)


def git(repo, *args):
    done = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=15)
    return done.stdout.strip() if done.returncode == 0 else None


def base_of(repo):
    """Where this checkout left the remote's main branch."""
    for name in ('origin/main', 'origin/master'):
        base = git(repo, 'merge-base', name, 'HEAD')
        if base:
            return name, base
    return None, None


def code_lines(repo, base, committed):
    """Changed lines per code file since `base`: the commits alone, or the working tree with its new files."""
    files = {}
    for row in (git(repo, 'diff', '--numstat', base, *(['HEAD'] if committed else [])) or '').splitlines():
        added, removed, path = (row.split('\t') + ['', ''])[:3]
        if added.isdigit() and not PROSE.search(path):
            files[path] = int(added) + int(removed)
    if not committed:
        for path in (git(repo, 'ls-files', '--others', '--exclude-standard') or '').splitlines()[:200]:
            if not PROSE.search(path):
                try:
                    with open(os.path.join(repo, path), 'rb') as f:
                        data = f.read(2_000_000)
                    files[path] = data.count(b'\n') + (1 if data and not data.endswith(b'\n') else 0)
                except OSError:
                    pass
    return files


def note_path(top):
    folder = os.path.join(tempfile.gettempdir(), 'claude-review-gate')
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, hashlib.sha1(os.path.normcase(os.path.normpath(top)).encode()).hexdigest() + '.json')


def log(**entry):
    home = os.environ.get('CLAUDE_REVIEW_LOG') or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                              'review-gate.log')
    with open(home, 'a', encoding='utf-8') as f:
        f.write(json.dumps({'time': time.strftime('%Y-%m-%dT%H:%M:%S'), **entry}) + '\n')


def local(path, cwd):
    """A path from a command as this process can open it: `~`, a Git Bash drive path, one relative to `cwd`."""
    path = os.path.expanduser(os.path.expandvars(path.replace('$HOME', '~').replace('$env:USERPROFILE', '~')))
    if os.name == 'nt':
        path = re.sub(r'^/(?:mnt/)?([A-Za-z])/', lambda m: m.group(1).upper() + ':/', path)
    return os.path.normpath(os.path.join(cwd, path))


def pushed_repo(command, cwd):
    """The checkout a push to main in this command acts on, or None."""
    for m in PUSH.finditer(command):
        rest = m.group('rest').split(' #')[0]
        if re.search(r'--dry-run|--delete\b', rest):
            continue
        repo = local(cwd, '.')
        for move in MOVE.findall(command[:m.start()]) + re.findall(r'-C\s+' + QUOTED, m.group('options')):
            step = local(next(p for p in move if p), repo)
            repo = step if os.path.isdir(step) else repo  # a path this hook cannot resolve: judge where we stand
        top = git(repo, 'rev-parse', '--show-toplevel')
        if not top:
            continue
        refs = [w.strip('"\'') for w in rest.split() if not w.startswith('-') and not re.match(r'\d*[<>]', w)][1:]
        named = any(re.fullmatch(r'\+?(?:[^:]*:)?(?:refs/heads/)?(?:main|master)', r) for r in refs)
        current = any(r in ('HEAD', '@') for r in refs) or not refs and not re.search(r'--(?:tags|all|mirror)\b', rest)
        if named or (current and git(top, 'branch', '--show-current') in ('main', 'master')):
            return top
    return None


def unreviewed(top, files):
    """Lines the reviewer's note does not cover, and the note itself."""
    try:
        with open(note_path(top), encoding='utf-8') as f:
            note = json.load(f)
    except (OSError, ValueError):
        return sum(files.values()), None
    name, _ = base_of(top)
    landed = note.get('passed') and subprocess.run(['git', '-C', top, 'merge-base', '--is-ancestor', note['passed'], name],
                                                   capture_output=True, timeout=15).returncode == 0
    if landed or time.time() - note.get('time', 0) > MAX_AGE:
        os.remove(note_path(top))
        return sum(files.values()), None
    seen = note.get('files') or {}
    return sum(max(0, n - seen.get(path, 0)) for path, n in files.items()), note


def push(event, least):
    command = str((event.get('tool_input') or {}).get('command') or '')
    if least <= 0 or 'push' not in command:
        return
    top = pushed_repo(command, event.get('cwd') or os.getcwd())
    _, base = base_of(top) if top else (None, None)
    if not base:
        return
    files = code_lines(top, base, True)
    total = sum(files.values())
    if total < least:
        return
    skip = SKIP.search(command)
    if skip:
        return log(repo=top, lines=total, decision='skipped', reason=skip.group(1).strip()[:200])
    new, note = unreviewed(top, files)
    if note and new <= min(FIX_LINES, total - new):  # fixes, not a change larger than what was reviewed
        note['passed'] = git(top, 'rev-parse', 'HEAD')
        with open(note_path(top), 'w', encoding='utf-8') as f:
            json.dump(note, f)
        return log(repo=top, lines=total, decision='reviewed', since_review=new)
    log(repo=top, lines=total, decision='held', since_review=new if note else None, agent=event.get('agent_type'))
    what = (f'{new} changed code lines came after the last review' if note else
            f'{total} changed code lines in {len(files)} files that no independent review has seen')
    if event.get('agent_type') in HELPERS:
        why = f'Push held back: {what}. Do not retry; report this to the agent that sent you, which must have the diff reviewed first.'
    else:
        why = (f'Push held back: {what}. Start the `reviewer` agent with the repository path ({top}), the range '
               f'{base[:10]}..HEAD and the request this change answers, fix what it finds, then push again; a second '
               'review is needed only after a High finding. Its report must end with the line `Reviewed: <repository '
               'path>` to count. If the user asked for this change to go out fast, repeat the push with the comment '
               '`# REVIEW_SKIP=<reason>` at the end of the command; skips are logged.')
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                             'permissionDecisionReason': why}}))


def reviewed(event):
    if event.get('agent_type') not in (None, 'reviewer'):
        return
    said = re.search(r'^\s*Reviewed:\s*`?([^`\n]+?)`?\s*$', str(event.get('last_assistant_message') or ''), re.M)
    repo = local(said.group(1).strip(), event.get('cwd') or os.getcwd()) if said else ''
    top = git(repo, 'rev-parse', '--show-toplevel') if os.path.isdir(repo) else None
    _, base = base_of(top) if top else (None, None)
    if not base:  # interrupted, or a review of something that is not a checkout: it unlocks nothing
        return log(repo=repo or event.get('cwd'), decision='review not counted')
    files = code_lines(top, base, False)
    with open(note_path(top), 'w', encoding='utf-8') as f:
        json.dump({'time': time.time(), 'files': files}, f)
    log(repo=top, lines=sum(files.values()), decision='review ran')


def main():
    least = int(os.environ.get('CLAUDE_REVIEW_MIN_LINES', '60') or 0)
    event = json.load(sys.stdin)
    if sys.argv[1:] == ['reviewed']:
        reviewed(event)
    else:
        push(event, least)


if __name__ == '__main__':
    try:
        main()
    except Exception:  # a hook must never break the session or hold a push it could not judge
        pass
