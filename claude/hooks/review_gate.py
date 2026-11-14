#!/usr/bin/env python3
"""Hold back a push to main that carries code no independent review has seen.

  review_gate.py push       PreToolUse hook on the shell tools
  review_gate.py reviewed   SubagentStop hook on the `reviewer` agent

A push of fewer than CLAUDE_REVIEW_MIN_LINES changed code lines (default 60; 0 turns the
gate off) passes, and so does prose. Above that the push needs a note that the reviewer
agent finished in this checkout, which it says with a last line `Reviewed: <repository>`.
The note holds a mark for every line the change added or removed then, so a rebase onto
other people's edits leaves it valid. Lines the note lacks count as fixes: up to 150 of
them, and no more than the review saw, pass without a second review. A note lasts a day.

A command that commits and pushes at once is judged by everything the checkout holds beyond
main, staged or not, since its commit does not exist yet and a hook cannot tell from the
command text what it will take. If that is held, the message says to commit first and push
in a command of its own, which is then judged by the commit alone. One that merges, picks
or reverts commits and pushes at once is told the same. A command that carries
REVIEW_SKIP=<reason>, as a trailing `# REVIEW_SKIP=<reason>` comment in any shell, passes
and is logged, for a change the user wants out fast.

Holds, passes on a review, skips and errors are appended to ~/.claude/review-gate.log. The
hook only reads the checkout; on any error it lets the push through.
"""
import collections, hashlib, json, os, re, subprocess, sys, tempfile, time

FIX_LINES = 150
MAX_AGE = 24 * 3600
HELPERS = {'scout', 'runner', 'mechanic'}
PROSE = re.compile(r'\.(md|txt|svg|png|jpe?g|gif|webp|lock)$|(^|/)(docs?|notes\.d|licenses)/|/values-[^/]+/strings\.xml$', re.I)
QUOTED = r'(?:"([^"]+)"|\'([^\']+)\'|([^\s;&|)]+))'
START = r'(?:^|[;&|\n({]|\b(?:then|do|else)\s)\s*'  # where a command begins, so text inside a commit message is no push
GIT = r'(?:[A-Za-z_]\w*=\S*\s+)*git(?:\.exe)?(?P<options>(?:\s+-[cC]\s+' + QUOTED + r'|\s+--?[\w-]+(?:=\S+)?)*)\s+'
PUSH = re.compile(START + GIT + r'push\b(?P<rest>[^|;&\n)}]*)', re.I)
MADE = re.compile(START + GIT + r'(?P<verb>commit|merge|cherry-pick|revert|am)\b', re.I)
MOVE = re.compile(START + r'(?:cd|Set-Location|sl|pushd|Push-Location)\s+(?:-(?:Literal)?Path\s+)?' + QUOTED, re.I)
SKIP = re.compile(r'REVIEW_SKIP\s*[=:]\s*[\'"]?([^\'";\n]+)', re.I)


def git(repo, *args):
    done = subprocess.run(['git', '-c', 'core.quotepath=off', '-C', repo, *args], capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=15)
    return done.stdout.strip() if done.returncode == 0 else None


def base_of(repo, tip='HEAD'):
    """Where `tip` left the remote's main branch."""
    return git(repo, 'merge-base', 'origin/main', tip) or git(repo, 'merge-base', 'origin/master', tip)


def changes(top, base, tip='HEAD', checkout=False):
    """Per code file, a mark for every line added or removed since `base`: in the commits up to `tip`, or with
    `checkout` in everything the checkout holds, which is the index, the working tree and the files git does not
    track yet. A mark is the line without its indentation, so the marks of a change survive a rebase onto other edits
    of the same file. Lines too short to tell apart (a brace, a blank) are marked `~`: they count as changed lines but
    never as lines a review covered."""
    marks = {}

    def mark(found, path, sign, text):
        text = text.strip()
        key = ('~' if len(text) < 4 else '') + hashlib.blake2b((sign + text).encode('utf-8', 'replace'), digest_size=6).hexdigest()
        found.setdefault(path, collections.Counter())[key] += 1

    def diff(*args):
        found, path, body = {}, None, False
        rows = git(top, 'diff', '--no-renames', '--no-color', '--no-ext-diff', '--src-prefix=a/', '--dst-prefix=b/', '-U0', *args)
        for row in (rows or '').split('\n'):
            if row.startswith('diff --git a/'):
                names = row[len('diff --git '):]
                name = names[2:2 + (len(names) - 5) // 2]
                path, body = (name if names == f'a/{name} b/{name}' and not PROSE.search(name) else None), False
            elif row.startswith('@@'):
                body = True
            elif path and body and row[:1] in ('+', '-'):
                mark(found, path, row[0], row[1:])
        marks.update(found)

    if not checkout:
        diff(base, tip)
        return marks
    diff('--cached', base)  # a file staged and then changed back still goes into a plain `git commit`
    diff(base)
    for name in (git(top, 'ls-files', '--others', '--exclude-standard', '--full-name') or '').splitlines()[:200]:
        if not PROSE.search(name):
            try:
                with open(os.path.join(top, name), 'rb') as f:
                    data = f.read(2_000_000)
            except OSError:
                continue
            if b'\0' not in data[:8000]:  # git would call it binary and count no lines
                found = {}
                for text in data.decode('utf-8', 'replace').replace('\r\n', '\n').split('\n')[:-1 if data.endswith(b'\n') else None]:
                    mark(found, name, '+', text)
                marks.update(found)
    return marks


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


def pushed(command, cwd):
    """For a push to main in this command: the checkout, the command text before it, and what it pushes."""
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
        branch, tip = git(top, 'branch', '--show-current'), None
        refs = [w.strip('"\'') for w in rest.split() if not w.startswith('-') and not re.match(r'\d*[<>]', w)][1:]
        for ref in refs:
            to_main = re.fullmatch(r'\+?(?:([^:]*):)?(?:refs/heads/)?(main|master)', ref)
            if to_main and to_main.group(1) != '':  # `:main` would delete it
                tip = to_main.group(1) or to_main.group(2)
        if not tip and branch in ('main', 'master') and (
                any(ref in ('HEAD', '@') for ref in refs) or not refs and not re.search(r'--(?:tags|all|mirror)\b', rest)):
            tip = 'HEAD'
        if tip:
            return top, command[:m.start()], 'HEAD' if tip in ('HEAD', '@', branch) else tip
    return None, '', None


def unreviewed(top, marks):
    """Changed lines the reviewer's note lacks, lines of substance it has, and whether there is a note at all."""
    total = sum(sum(file.values()) for file in marks.values())
    try:
        with open(note_path(top), encoding='utf-8') as f:
            note = json.load(f)
    except (OSError, ValueError):
        return total, 0, False
    if time.time() - note.get('time', 0) > MAX_AGE:
        os.remove(note_path(top))
        return total, 0, False
    new = covered = 0
    for path, now in marks.items():
        seen = collections.Counter((note.get('files') or {}).get(path) or {})
        new += sum((now - seen).values())
        covered += sum(n for key, n in (now & seen).items() if not key.startswith('~'))
    return new, covered, True


def deny(why):
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                             'permissionDecisionReason': why}}))


def push(event, least):
    command = str((event.get('tool_input') or {}).get('command') or '')
    if least <= 0 or 'push' not in command:
        return
    top, before, tip = pushed(command, event.get('cwd') or os.getcwd())
    base = base_of(top, tip) if top else None
    if not base:
        return
    made = {m.group('verb').lower() for m in MADE.finditer(before)}
    skip = SKIP.search(command)
    reason = skip.group(1).strip()[:200] if skip else None
    if made - {'commit'}:  # commits that do not exist yet and are not in the working tree either
        if skip:
            return log(repo=top, decision='skipped', reason=reason)
        log(repo=top, decision='held', reason='commits brought in by the same command', agent=event.get('agent_type'))
        return deny('Push held back: this command brings in commits and pushes them at once, so the review hook cannot see '
                    'what the push carries. Run the push as a command of its own.')
    tree = 'commit' in made and tip == 'HEAD'  # that commit does not exist yet, and what it takes is not ours to guess
    marks = changes(top, base, tip, tree)
    total = sum(sum(file.values()) for file in marks.values())
    if total < least:
        return
    if skip:
        return log(repo=top, lines=total, decision='skipped', reason=reason)
    new, covered, noted = unreviewed(top, marks)
    if noted and (new == 0 or covered and new <= min(FIX_LINES, covered)):  # fixes, not a change larger than what was reviewed
        return log(repo=top, lines=total, decision='reviewed', since_review=new)
    log(repo=top, lines=total, decision='held', since_review=new if covered else None, agent=event.get('agent_type'),
        **({'counted': 'checkout'} if tree else {}))
    what = (f'{new} changed code lines came after the last review, more than its margin for fixes' if covered else
            f'{total} changed code lines in {len(marks)} files that no independent review has seen')
    if tree:  # the usual false hold: other work lying in the tree while a small commit goes out
        what += ('. This command commits and pushes at once, so that is everything the checkout holds beyond main, staged '
                 'or not. If the commit takes less, make it first and push in a command of its own, which is judged by '
                 'the commit alone. If it takes all of it')
        if event.get('agent_type') in HELPERS:
            return deny(f'Push held back: {what}, do not retry; report this to the agent that sent you, which must have the '
                        'diff reviewed first.')
        what += ', it needs a review'
    elif event.get('agent_type') in HELPERS:
        return deny(f'Push held back: {what}. Do not retry; report this to the agent that sent you, which must have the diff reviewed first.')
    span = f'the working tree against {base[:10]}' if tree else f'the range {base[:10]}..{tip}'
    deny(f'Push held back: {what}. Start the `reviewer` agent with the repository path ({top}), {span} and the request this '
         f'change answers, fix what it finds, then push again; fixes of up to {FIX_LINES} lines need no second review unless a '
         'finding was High. Its report must end with the line `Reviewed: <repository path>` to count. If the user asked for '
         'this change to go out fast, repeat the push with the comment `# REVIEW_SKIP=<reason>` at the end of the command; '
         'skips are logged.')


def reviewed(event):
    if event.get('agent_type') not in (None, 'reviewer'):
        return
    said = re.search(r'^\s*Reviewed:\s*`?([^`\n]+?)`?\s*$', str(event.get('last_assistant_message') or ''), re.M)
    repo = local(said.group(1).strip(), event.get('cwd') or os.getcwd()) if said else ''
    top = git(repo, 'rev-parse', '--show-toplevel') if os.path.isdir(repo) else None
    base = base_of(top) if top else None
    if not base:  # interrupted, or a review of something that is not a checkout: it unlocks nothing
        return log(repo=repo or event.get('cwd'), decision='review not counted')
    marks = changes(top, base, 'HEAD', True)
    with open(note_path(top), 'w', encoding='utf-8') as f:
        json.dump({'time': time.time(), 'files': marks}, f)
    log(repo=top, lines=sum(sum(file.values()) for file in marks.values()), decision='review ran')


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
    except Exception as error:  # a hook must never break the session or hold a push it could not judge
        try:
            log(decision='error', error=repr(error)[:200])
        except Exception:
            pass
