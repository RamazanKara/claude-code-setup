#!/usr/bin/env python3
"""Keep a memory folder the same on this machine and one other, both ways. Run it on this machine;
the other one only hands out a copy over scp and takes files back.

  memory_sync.py [NAME ...] [--dry]             every pair machine.json names, or the ones named
  memory_sync.py [NAME] --mine FILE [FILE ...]  where a file needs a hand: this machine's copy counts
  memory_sync.py [NAME] --theirs FILE [...]     the other machine's copy counts

~/.claude/machine.json:
  "memory": {"myproject": {"local": "~/.claude/projects/C--src-x/memory", "host": "laptop",
                           "remote": "C:/Users/me/.claude/projects/C--src-X/memory", "exclude": ["build-env.md"]}}
  "host" is a name under "hosts", which gives its ssh address and shell. Files in "exclude" stay
  each machine's own.

Each *.md file is compared with the copy both machines had after the last run, kept in
~/.claude/memory-sync/NAME/base. Who saved a file and when, which every save changes, does not count.
  changed or new on one machine   copied to the other
  changed on both                 merged by line when the changes lie apart; otherwise both stay as they
                                  are and the file is named
  deleted on one machine          named, and left alone on the other: never copied back, never deleted
  different and never synced      named, and left alone
MEMORY.md is merged by the file each line points to, and no machine gets a pointer to a file it lacks.
For a named file the other machine's copy is saved in ~/.claude/memory-sync/NAME/theirs, to read before
--mine or --theirs. No memory file is deleted on either machine, and before a run writes, both machines'
files are saved in .../NAME/backup/<time> (the last 20 stay).

Exit 0 when both are in step, 1 when files need a hand, 2 when it could not run.
"""
import argparse, base64, contextlib, datetime, json, os, re, shlex, shutil, subprocess, sys, tempfile, time

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.environ.get('CLAUDE_MEMORY_SYNC_DIR') or os.path.join(HOME, 'memory-sync')
INDEX = 'MEMORY.md'
SAVED = re.compile(rb'^[ \t]+(originSessionId|modified):[^\n]*\n?', re.M)
POINTER = re.compile(rb'\s*[-*]\s*\[[^\]\n]*\]\(([^)\n]+)\)')
SAFE = re.compile(r'[A-Za-z0-9_./:~\\-]+$')
BACKUPS = 20


class Stop(Exception):
    """This pair cannot be synced now; nothing was written."""


def head(data):
    """The length of the frontmatter a memory file starts with; 0 without one."""
    end = data.find(b'\n---', 3) if data.startswith(b'---') else -1
    return end + 1 if end > 0 else 0


def plain(data):
    """A file without who saved it and when."""
    return SAVED.sub(b'', data[:head(data)]) + data[head(data):]


def stamped(data, like):
    """`data` with those lines as `like` has them, so that a merge does not see them as changed."""
    lines = {found.group(1): found.group(0) for found in SAVED.finditer(like[:head(like)])}
    return SAVED.sub(lambda found: lines.get(found.group(1), found.group(0)), data[:head(data)]) + data[head(data):]


def saved(data, changed):
    """When a file was last saved: what it says itself, else the file system's time."""
    found = re.search(rb'^[ \t]+modified:\s*(\S+)', data[:head(data)], re.M)
    with contextlib.suppress(ValueError):
        if found:
            return datetime.datetime.fromisoformat(found.group(1).decode('ascii', 'replace').replace('Z', '+00:00')).timestamp()
    return changed


def read_all(folder):
    """({name: content}, {name: time of change}) of the memory files in a folder."""
    data, times = {}, {}
    if os.path.isdir(folder):
        with os.scandir(folder) as found:
            for entry in found:
                if entry.name.endswith('.md') and entry.is_file(follow_symlinks=False):
                    with open(entry.path, 'rb') as f:
                        data[entry.name] = f.read()
                    times[entry.name] = entry.stat().st_mtime
    return data, times


def write(folder, name, data):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name + '.sync-tmp'), 'wb') as f:
        f.write(data)
    os.replace(os.path.join(folder, name + '.sync-tmp'), os.path.join(folder, name))


def settings():
    path = os.environ.get('CLAUDE_MACHINE_FILE') or os.path.join(HOME, 'machine.json')
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError) as error:
        print(f'memory_sync.py: cannot read {path} ({error})', file=sys.stderr)
        sys.exit(2)
    ssh = data.get('ssh') or 'ssh'
    ssh = [ssh] if isinstance(ssh, str) else list(ssh)
    beside = os.path.join(os.path.dirname(ssh[0]), 'scp' + os.path.splitext(ssh[0])[1]) if os.path.dirname(ssh[0]) else 'scp'
    scp = data.get('scp') or beside
    return {'pairs': data.get('memory') or {}, 'hosts': data.get('hosts') or {}, 'ssh': ssh, 'scp': [scp] if isinstance(scp, str) else list(scp)}


def run(argv, what, timeout=60):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, errors='replace', timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Stop(f'{what}: {type(error).__name__}')
    if done.returncode:
        said = (done.stderr or done.stdout or '').strip().splitlines()
        raise Stop(f'{what}: exit {done.returncode}: {said[-1][:200] if said else "no output"}')


def fetch(cfg, pair, to):
    """A copy of the other machine's folder in `to`, which must not exist yet."""
    run([*cfg['scp'], '-r', '-p', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', f'{pair["address"]}:{pair["remote"]}', to],
        f'cannot fetch {pair["remote"]} from {pair["host"]}')
    if not os.path.isdir(to):
        raise Stop(f'{pair["remote"]} on {pair["host"]} is no folder')
    return read_all(to)


def push(cfg, pair, files, tmp):
    """Give the other machine these files: copied beside its folder first, then moved in, so that nothing
    there reads a half-written file."""
    staged, out = pair['remote'] + '.incoming', os.path.join(tmp, 'out')
    for name, data in files.items():
        write(out, name, data)
    if pair['shell'] == 'powershell':
        def shell(script):
            return 'powershell -NoProfile -NonInteractive -EncodedCommand ' + base64.b64encode(("$ErrorActionPreference = 'Stop'; " + script).encode('utf-16-le')).decode()
        make = shell(f"if (Test-Path -LiteralPath '{staged}') {{ Remove-Item -LiteralPath '{staged}' -Recurse -Force }}; New-Item -ItemType Directory -Path '{staged}' | Out-Null")
        move = shell(f"Get-ChildItem -LiteralPath '{staged}' -File | Move-Item -Destination '{pair['remote']}' -Force; Remove-Item -LiteralPath '{staged}' -Recurse -Force")
    else:
        make = f'rm -rf -- {shlex.quote(staged)} && mkdir -p -- {shlex.quote(staged)}'
        move = f'mv -f -- {shlex.quote(staged)}/*.md {shlex.quote(pair["remote"])}/ && rmdir -- {shlex.quote(staged)}'
    ssh = [*cfg['ssh'], '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', pair['address']]
    run([*ssh, make], f'cannot prepare {staged} on {pair["host"]}')
    run([*cfg['scp'], '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', *(os.path.join(out, name) for name in sorted(files)), f'{pair["address"]}:{staged}/'],
        f'cannot copy to {pair["host"]}')
    run([*ssh, move], f'cannot move the files into {pair["remote"]} on {pair["host"]}')


def merged(mine, base, theirs, mine_first):
    """A line merge of two edits of `base`, or None when both touch the same lines."""
    like = mine if mine_first else theirs
    with tempfile.TemporaryDirectory() as tmp:
        paths = [os.path.join(tmp, label) for label in ('mine', 'base', 'theirs')]
        for path, data in zip(paths, (mine, base, theirs)):
            with open(path, 'wb') as f:
                f.write(stamped(data, like))
        try:
            done = subprocess.run(['git', 'merge-file', '-p', *paths], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
    return done.stdout if done.returncode == 0 else None


def lines(data):
    return [line if line.endswith(b'\n') else line + b'\n' for line in data.splitlines(keepends=True)]


def target(line):
    found = POINTER.match(line)
    return found.group(1).strip() if found else None


def index(mine, theirs, base, mine_was, theirs_was, mine_first):
    """MEMORY.md of both machines as one. A pointer follows the machine that changed it since the last run
    (`mine_was`, `theirs_was`: what each was left with then), the newer one where both did; an unchanged
    one stays as `base`, the whole of last time, has it. Headings and order are those of the machine
    that changed them."""
    def pointers(data):
        return {target(line): line for line in lines(data) if target(line)}

    def layout(data):
        return [line for line in lines(data) if not target(line)]
    ours, others, old, ours_was, others_was = (pointers(data) for data in (mine, theirs, base, mine_was, theirs_was))
    chosen = {}
    for name in {*ours, *others, *old}:
        a, b = ours.get(name), others.get(name)
        here, there = a != ours_was.get(name), b != others_was.get(name)
        chosen[name] = old.get(name) if not here and not there else a if not there or a == b else b if not here else a if mine_first else b
    if layout(mine) == layout(base) != layout(theirs):
        lead = theirs
    elif layout(theirs) == layout(base):
        lead = mine
    else:
        lead = mine if mine_first else theirs
    out, placed = [], set()
    for line in lines(lead):
        name = target(line)
        if not name:
            out.append(line)
        elif chosen.get(name) and name not in placed:
            out.append(chosen[name])
            placed.add(name)
    after = None  # a pointer only the other machine has goes after the line it follows there
    for line in lines(theirs if lead is mine else mine):
        name = target(line)
        if name and chosen.get(name) and name not in placed:
            out.insert(out.index(after) + 1 if after in out else len(out), chosen[name])
            placed.add(name)
        if name:
            after = chosen.get(name) or after
        elif line.strip():
            after = line
    return b''.join(out)


def pointing_at(data, names):
    """MEMORY.md without the pointers to memory files a machine does not have."""
    def there(line):
        name = target(line)
        return not name or not name.endswith(b'.md') or b'/' in name or b':' in name or name.decode('utf-8', 'replace') in names
    return b''.join(line for line in lines(data) if there(line))


def plan(local, remote, base, first, mine=(), theirs=()):
    """What each machine gets: (for this one, for the other, the new base, {file: why it needs a hand}).
    `first(name)` says whether this machine's copy is the newer one."""
    here, there, keep, hand = {}, {}, {}, {}

    def same(a, b):
        return a is not None and b is not None and plain(a) == plain(b)
    for name in sorted({*local, *remote, *base} - {INDEX}):
        a, z, b = local.get(name), base.get(name), remote.get(name)
        if a is None and b is None:
            continue  # gone on both: forgotten
        if same(a, b):
            keep[name] = a
        elif name in mine or name in theirs:
            won = a if name in mine else b
            if won is None:
                hand[name] = 'is gone on the machine whose copy you asked for; to drop it, delete the other copy by hand'
                keep.update({name: z} if z is not None else {})
            else:
                (there if name in mine else here)[name] = keep[name] = won
        elif a is None or b is None:
            if z is None:  # new on one machine
                (here if a is None else there)[name] = keep[name] = b if a is None else a
            else:
                keep[name] = z
                hand[name] = ('was deleted here and is still on the other machine' if a is None else 'was deleted on the other machine and is still here') + \
                    ': delete that copy by hand too, or bring the file back with ' + ('--theirs' if a is None else '--mine')
        elif z is None:
            hand[name] = 'differs and was never synced: read both, then --mine or --theirs'
        elif same(a, z):
            here[name] = keep[name] = b
        elif same(b, z):
            there[name] = keep[name] = a
        else:
            both = merged(a, z, b, first(name))
            if both is None:
                keep[name] = z
                hand[name] = 'was changed on both machines in the same lines: bring them together here, then --mine'
            else:
                keep[name] = both
                here.update({name: both} if both != a else {})
                there.update({name: both} if both != b else {})
    return here, there, keep, hand


def backup(state, local, remote):
    folder = os.path.join(state, 'backup')
    now = os.path.join(folder, datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    for side, files in (('here', local), ('there', remote)):
        for name, data in files.items():
            write(os.path.join(now, side), name, data)
    for old in sorted(os.listdir(folder))[:-BACKUPS]:
        shutil.rmtree(os.path.join(folder, old), ignore_errors=True)


@contextlib.contextmanager
def one_run(state):
    os.makedirs(state, exist_ok=True)
    lock = os.path.join(state, 'lock')
    with contextlib.suppress(OSError):
        if time.time() - os.path.getmtime(lock) > 600:
            os.remove(lock)
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except OSError:
        raise Stop(f'another sync of it is running ({lock})')
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            os.remove(lock)


def sync(cfg, name, dry=False, mine=(), theirs=()):
    """Sync one pair and say what happened. Returns how many files need a hand."""
    pair = dict(cfg['pairs'][name])
    host = cfg['hosts'].get(pair.get('host'))
    if not isinstance(host, dict) or not pair.get('local') or not pair.get('remote'):
        raise Stop('machine.json must give it "local", "remote" and a "host" that "hosts" names')
    pair.update(address=str(host.get('ssh') or pair['host']), shell=host.get('shell'), remote=str(pair['remote']).replace('\\', '/').rstrip('/'))
    if not SAFE.match(pair['remote']):
        raise Stop(f'the remote path may hold letters, digits and _ . / : ~ - only: {pair["remote"]}')
    folder, state, skip = os.path.expanduser(str(pair['local'])), os.path.join(STATE, name), set(pair.get('exclude') or [])
    if not os.path.isdir(folder):
        raise Stop(f'{folder} is no folder')
    with one_run(state), tempfile.TemporaryDirectory() as tmp:
        (all_here, times_here), (all_there, times_there) = read_all(folder), fetch(cfg, pair, os.path.join(tmp, 'there'))
        base = read_all(os.path.join(state, 'base'))[0]
        local, remote = ({n: d for n, d in found.items() if n not in skip} for found in (all_here, all_there))
        for where, found in (('this machine', local), (pair['host'], remote)):
            if len(base) >= 4 and len(found) < len(base) / 2:  # a wrong path or an emptied folder, not an edit
                raise Stop(f'{where} has {len(found)} memory files where the last run left {len(base)}')
        unknown = sorted({*mine, *theirs} - {*local, *remote, *base})
        if unknown:
            raise Stop(f'no such memory file on either machine: {", ".join(unknown)}')

        def first(n):
            return saved(local.get(n, b''), times_here.get(n, 0)) >= saved(remote.get(n, b''), times_there.get(n, 0))
        here, there, keep, hand = plan(local, remote, base, first, mine, theirs)
        if INDEX in local and INDEX in remote:
            was = [base.get(INDEX, b'')] * 2  # what each machine's index was left as: without pointers to files it lacked
            for at, side in enumerate(('here', 'there')):
                with contextlib.suppress(OSError), open(os.path.join(state, 'base', f'{INDEX}.{side}'), 'rb') as f:
                    was[at] = f.read()
            whole = index(local[INDEX], remote[INDEX], base.get(INDEX, b''), *was, first(INDEX))
        elif INDEX in base and (INDEX in local or INDEX in remote):  # deleted on one machine, like any other file
            whole, hand[INDEX] = base[INDEX], 'was deleted on one machine and is still on the other: write it again there, or delete the other by hand'
        else:
            whole = local.get(INDEX, remote.get(INDEX))  # new on one machine, or on neither
        if whole is not None:
            keep[INDEX] = whole
            for found, writes, has in ((local, here, {*all_here, *here}), (remote, there, {*all_there, *there})) if INDEX not in hand else ():
                if pointing_at(whole, has) != found.get(INDEX):
                    writes[INDEX] = pointing_at(whole, has)
        late = []
        if not dry and (here or there):
            backup(state, all_here, all_there)
            now_here, now_there = read_all(folder)[0], fetch(cfg, pair, os.path.join(tmp, 'again'))[0] if there else all_there
            late = sorted(n for n in {*here, *there} if now_here.get(n) != all_here.get(n) or now_there.get(n) != all_there.get(n))
            for n in late:  # changed while this ran: the next run takes it, from the old base
                here.pop(n, None), there.pop(n, None), keep.pop(n, None)
                keep.update({n: base[n]} if n in base else {})
            if there:
                push(cfg, pair, there, tmp)  # first: if it fails, nothing is written here and the next run starts as this one did
            for n, data in here.items():
                write(folder, n, data)
        if not dry:
            for n, data in keep.items():
                if base.get(n) != data:
                    write(os.path.join(state, 'base'), n, data)
            for side, has in (('here', {*all_here, *here}), ('there', {*all_there, *there})) if INDEX in keep and INDEX not in hand and INDEX not in late else ():
                write(os.path.join(state, 'base'), f'{INDEX}.{side}', pointing_at(keep[INDEX], has))
            for n in set(base) - set(keep):
                os.remove(os.path.join(state, 'base', n))
            shutil.rmtree(os.path.join(state, 'theirs'), ignore_errors=True)
            for n in hand:
                if n in remote:
                    write(os.path.join(state, 'theirs'), n, remote[n])
    print(f'{name} ({pair["host"]}): {len({*local, *remote})} files' + (', dry run: nothing written' if dry else ''))
    both = sorted(set(here) & set(there) - {INDEX})
    for title, names in ((f'to {pair["host"]}', sorted(set(there) - set(both))), ('to this machine', sorted(set(here) - set(both))), ('merged on both', both),
                         ('changed while this ran, left for the next run', late)):
        if names:
            print(f'  {title}: {", ".join(names)}')
    for n, why in sorted(hand.items()):
        print(f'  needs a hand: {n} {why}' + (f' (the other copy: {os.path.join(state, "theirs", n)})' if n in remote and not dry else ''))
    if not (here or there or hand or late):
        print('  in step')
    return len(hand)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('names', nargs='*', metavar='NAME')
    ap.add_argument('--dry', action='store_true', help='say what a run would do; write nothing')
    ap.add_argument('--mine', nargs='+', default=[], metavar='FILE')
    ap.add_argument('--theirs', nargs='+', default=[], metavar='FILE')
    args = ap.parse_args()
    cfg = settings()
    names = args.names or sorted(cfg['pairs'])
    missing = [name for name in names if name not in cfg['pairs']]
    if missing or not names or (args.mine or args.theirs) and len(names) != 1 or set(args.mine) & set(args.theirs):
        print('memory_sync.py: ' + (f'machine.json names no memory pair "{missing[0]}"' if missing else 'machine.json names no memory pairs under "memory"' if not names
                                    else 'a file is either --mine or --theirs' if set(args.mine) & set(args.theirs) else '--mine and --theirs need the one pair they are about'), file=sys.stderr)
        sys.exit(2)
    worst = 0
    for name in names:
        try:
            worst = max(worst, 1 if sync(cfg, name, args.dry, set(args.mine), set(args.theirs)) else 0)
        except Stop as why:
            print(f'{name}: not synced: {why}', file=sys.stderr)
            worst = 2
    sys.exit(worst)


if __name__ == '__main__':
    main()
