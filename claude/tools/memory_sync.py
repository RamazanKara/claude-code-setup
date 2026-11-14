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
~/.claude/memory-sync/NAME/base. Who saved a file and when, which every save changes, does not count,
nor do line endings.
  changed or new on one machine   copied to the other
  changed on both                 merged by line when the changes lie apart; otherwise both stay as they
                                  are and the file is named
  deleted on one machine          named, and left alone on the other: never copied back, never deleted
  different and never synced      named, and left alone
MEMORY.md is merged by the file each line points to. A pointer stays on a machine for as long as that
machine has the file, and no machine gets a pointer to a file it lacks. Where both machines changed
the same pointer, or both the headings, it is named like any other file.
For a named file the other machine's copy is saved in ~/.claude/memory-sync/NAME/theirs, to read before
--mine or --theirs. No memory file is deleted on either machine, a file that changes while a run is
under way is left for the next run, and before a run writes, both machines' files are saved in
.../NAME/backup/<time> (the last 20 stay).

Exit 0 when both are in step, 1 when files need a hand or another run, 2 when it could not run.
"""
import argparse, base64, contextlib, datetime, hashlib, json, os, re, shlex, shutil, subprocess, sys, tempfile, time

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.environ.get('CLAUDE_MEMORY_SYNC_DIR') or os.path.join(HOME, 'memory-sync')
INDEX = 'MEMORY.md'
SAVED = re.compile(rb'^[ \t]+(originSessionId|modified):[^\n]*\n?', re.M)
POINTER = re.compile(rb'\s*[-*]\s*\[[^\]\n]*\]\(([^)\n]+)\)')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*\.md$')  # what goes into a command on the other machine
SAFE = re.compile(r'[A-Za-z0-9_./:\\-]+$')
BACKUPS = 20


class Stop(Exception):
    """This pair cannot be synced now."""


def head(data):
    """The length of the frontmatter a memory file starts with; 0 without one."""
    end = data.find(b'\n---', 3) if data.startswith(b'---') else -1
    return end + 1 if end > 0 else 0


def plain(data):
    """A file without who saved it and when, and with one kind of line ending."""
    data = data.replace(b'\r\n', b'\n')
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


def read_all(folder, every=False):
    """({name: content}, {name: time of change}) of the memory files in a folder; `every`: of all its files."""
    data, times = {}, {}
    if os.path.isdir(folder):
        with os.scandir(folder) as found:
            for entry in found:
                if (every or entry.name.endswith('.md')) and entry.is_file(follow_symlinks=False):
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
    """A command's output; Stop when it fails."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, errors='replace', timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Stop(f'{what}: {type(error).__name__}')
    if done.returncode:
        said = (done.stderr or done.stdout or '').strip().splitlines()
        raise Stop(f'{what}: exit {done.returncode}: {said[-1][:200] if said else "no output"}')
    return done.stdout


def fetch(cfg, pair, to):
    """A copy of the other machine's folder in `to`, which must not exist yet."""
    run([*cfg['scp'], '-r', '-p', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', f'{pair["address"]}:{pair["remote"]}', to],
        f'cannot fetch {pair["remote"]} from {pair["host"]}')
    if not os.path.isdir(to):
        raise Stop(f'{pair["remote"]} on {pair["host"]} is no folder')
    return read_all(to)


def push(cfg, pair, files, was, tmp):
    """Give the other machine these files and return the names it took. They are copied beside its folder
    first and moved in one by one, so that nothing there reads a half-written file, and a file is moved
    only while the one it replaces is still what this run read (`was`: name -> content, absent = no file)."""
    remote, staged, out = pair['remote'], pair['remote'] + '.incoming', os.path.join(tmp, 'out')
    for name, data in files.items():
        write(out, name, data)
    # What each file replaces, in a file of its own (a command line holds few), the index after the files it points to.
    write(out, '.want', ''.join(f'{name} {hashlib.sha256(was[name]).hexdigest() if name in was else ""}\n'
                               for name in sorted(files, key=lambda name: (name == INDEX, name))).encode('ascii'))
    if pair['shell'] in ('powershell', 'pwsh'):
        def shell(script):
            # Its own errors arrive as XML: say them in a line instead.
            script = "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; try { " + script + " } catch { 'failed: ' + $_.Exception.Message; exit 1 }"
            return f'{pair["shell"]} -NoProfile -NonInteractive -EncodedCommand ' + base64.b64encode(script.encode('utf-16-le')).decode()
        make = shell(f"if (Test-Path -LiteralPath '{staged}') {{ Remove-Item -LiteralPath '{staged}' -Recurse -Force }}")
        move = shell(f"$sha = [Security.Cryptography.SHA256]::Create(); foreach ($line in [IO.File]::ReadAllLines((Join-Path '{staged}' '.want'))) {{ "
                     f"$name, $digest = $line.Split(' '); $to = Join-Path '{remote}' $name; "
                     "$now = if (Test-Path -LiteralPath $to) { [BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($to))).Replace('-', '').ToLower() } else { '' }; "
                     f"if ($now -eq $digest) {{ Move-Item -LiteralPath (Join-Path '{staged}' $name) -Destination $to -Force; 'moved: ' + $name }} }}; "
                     f"Remove-Item -LiteralPath '{staged}' -Recurse -Force")
    else:
        there, here = shlex.quote(remote), shlex.quote(staged)
        make = f'rm -rf -- {here}'
        move = ('set -e; sum() { if command -v sha256sum >/dev/null 2>&1; then sha256sum < "$1"; else shasum -a 256 < "$1"; fi | cut -d" " -f1; }; '
                f'while read -r name digest; do now=""; if [ -f {there}/"$name" ]; then now=$(sum {there}/"$name"); fi; '
                f'if [ "$now" = "$digest" ]; then mv -f -- {here}/"$name" {there}/"$name"; echo "moved: $name"; fi; done < {here}/.want; rm -rf -- {here}')
    ssh = [*cfg['ssh'], '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', pair['address']]
    run([*ssh, make], f'cannot prepare {staged} on {pair["host"]}')
    run([*cfg['scp'], '-r', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', out, f'{pair["address"]}:{staged}'], f'cannot copy to {pair["host"]}')
    said = run([*ssh, move], f'the move into {pair["remote"]} on {pair["host"]} did not finish; the next run sorts out what arrived')
    return {line[len('moved: '):].strip() for line in said.splitlines() if line.startswith('moved: ')} & set(files)


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
    return [line if line.endswith(b'\n') else line + b'\n' for line in data.replace(b'\r\n', b'\n').splitlines(keepends=True)]


def target(line):
    """The file an index line points to, as written there."""
    found = POINTER.match(line)
    return found.group(1).strip() if found else None


def pointers(data):
    return {target(line): line for line in lines(data) if target(line)}


def fold(names):
    """Names as a Windows machine tells them apart."""
    return {(name.decode('utf-8', 'replace') if isinstance(name, bytes) else name).casefold() for name in names}


def index(mine, theirs, base, mine_was, theirs_was, only_mine=(), only_theirs=(), prefer=None):
    """MEMORY.md of both machines as one, or None when both changed the same pointer, or both the headings,
    in different ways (`prefer`: 'mine' or 'theirs' then counts). A pointer follows the machine that
    changed it since the last run (`mine_was`, `theirs_was`: what each was left with then; `base`: the
    whole of then). While a file is on one machine alone (`only_mine`, `only_theirs`) its pointer is
    that machine's. Headings and order are those of the machine that changed them."""
    def layout(data):
        return [line for line in lines(data) if not target(line)]
    ours, others, old, ours_was, others_was = (pointers(data) for data in (mine, theirs, base, mine_was, theirs_was))
    only_mine, only_theirs, chosen = fold(only_mine), fold(only_theirs), {}
    for name in {*ours, *others, *old}:
        a, b = ours.get(name), others.get(name)
        here, there = a != ours_was.get(name), b != others_was.get(name)
        if fold([name]) & only_mine:
            chosen[name] = a
        elif fold([name]) & only_theirs:
            chosen[name] = b
        elif here and there and a != b:
            if not prefer:
                return None
            chosen[name] = a if prefer == 'mine' else b
        else:
            chosen[name] = a if here else b if there else old.get(name)
    ours_lay, others_lay, old_lay = layout(mine), layout(theirs), layout(base)
    if ours_lay != old_lay and others_lay != old_lay and ours_lay != others_lay:
        if not prefer:
            return None
        lead = mine if prefer == 'mine' else theirs
    else:
        lead = theirs if ours_lay == old_lay else mine
    out, placed = [], set()
    for line in lines(lead):
        name = target(line)
        if not name:
            out.append(line)
        elif chosen.get(name) and name not in placed:
            out.append(chosen[name])
            placed.add(name)
    for source in (theirs if lead is mine else mine, base):
        after = None  # a pointer the leading machine lacks goes after the line it follows on the other, or in last time's
        for line in lines(source):
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
    names = fold(names)

    def there(line):
        name = target(line)
        return not name or not name.endswith(b'.md') or b'/' in name or b':' in name or fold([name]) & names
    return b''.join(line for line in lines(data) if there(line))


def plan(local, remote, base, first, mine=(), theirs=()):
    """What each machine gets: (for this one, for the other, the new base, {file: why it needs a hand}).
    `first(name)` says whether this machine's copy is the newer one. MEMORY.md is not planned here."""
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


def unfinished(state, local, remote):
    """What a run that did not finish had already done. Its plan lies in `pending`: the base it was
    heading for, and which machine each file was meant for. A file counts as synced once every machine
    it was meant for holds exactly it; a machine's index, once that machine holds it."""
    pending = read_all(os.path.join(state, 'pending'), every=True)[0]
    try:
        meant = json.loads(pending.pop('.plan.json'))
    except (KeyError, ValueError):
        return {}
    took = {name: pending[name] for name, sides in meant.items() if name in pending and name != INDEX and sides
            and all((local if side == 'here' else remote).get(name) == pending[name] for side in sides)}
    sides = {INDEX + end: pending[INDEX + end] for end, found in (('.here', local), ('.there', remote))
             if pending.get(INDEX + end) is not None and found.get(INDEX) == pending[INDEX + end]}
    if sides and INDEX in pending:  # the whole it came from goes with it: what the other machine still lacks is then seen as owed
        took.update({INDEX: pending[INDEX], **sides})
    return took


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
    """Sync one pair and say what happened. Returns whether both machines are in step now."""
    pair = dict(cfg['pairs'][name])
    host = cfg['hosts'].get(pair.get('host'))
    if not isinstance(host, dict) or not pair.get('local') or not pair.get('remote'):
        raise Stop('machine.json must give it "local", "remote" and a "host" that "hosts" names')
    pair.update(address=str(host.get('ssh') or pair['host']), shell=host.get('shell'), remote=str(pair['remote']).replace('\\', '/').rstrip('/'))
    if not SAFE.match(pair['remote']):
        raise Stop(f'the remote path may hold letters, digits and _ . / : - only, written out in full: {pair["remote"]}')
    folder, state = os.path.expanduser(str(pair['local'])), os.path.join(STATE, name)
    if not os.path.isdir(folder):
        raise Stop(f'{folder} is no folder')
    with one_run(state), tempfile.TemporaryDirectory() as tmp:
        (all_here, times_here), (all_there, times_there) = read_all(folder), fetch(cfg, pair, os.path.join(tmp, 'there'))
        odd = sorted(n for n in {*all_here, *all_there} if not NAME.match(n))
        skip = {*(pair.get('exclude') or []), *odd}
        local, remote = ({n: d for n, d in found.items() if n not in skip} for found in (all_here, all_there))
        took, stored = unfinished(state, local, remote), read_all(os.path.join(state, 'base'), every=True)[0]
        stored.update(took)
        for n, data in took.items() if not dry else ():  # at once: this run may be cut off too
            write(os.path.join(state, 'base'), n, data)
        base = {n: d for n, d in stored.items() if n.endswith('.md')}
        was = [stored.get(INDEX + end, base.get(INDEX, b'')) for end in ('.here', '.there')]  # each machine's index after the last run
        for where, found, before in ((('this machine', local, was[0]), (pair['host'], remote, was[1]))):
            meant = f'; if that is meant, delete {os.path.join(state, "base")} and run again as for the first time'
            if len(base) >= 4 and len(found) < len(base) / 2:  # a wrong path or an emptied folder, not an edit
                raise Stop(f'{where} has {len(found)} memory files where the last run left {len(base)}' + meant)
            had, has = len(pointers(before)), len(pointers(found.get(INDEX, before)))
            if had >= 4 and has < had / 2 or had and not has:
                raise Stop(f'{INDEX} on {where} has {has} pointers where the last run left {had}' + meant)
        unknown = sorted({*mine, *theirs} - {*local, *remote, *base})
        if unknown:
            raise Stop(f'no such memory file on either machine: {", ".join(unknown)}')

        def first(n):
            return saved(local.get(n, b''), times_here.get(n, 0)) >= saved(remote.get(n, b''), times_there.get(n, 0))
        here, there, keep, hand = plan(local, remote, base, first, mine, theirs)

        def whole_index():
            """(MEMORY.md of both machines as one, why it needs a hand instead) for the plan as it stands."""
            a, z, b = local.get(INDEX), base.get(INDEX), remote.get(INDEX)
            pick = 'mine' if INDEX in mine else 'theirs' if INDEX in theirs else None
            has = [fold({*found, *writes}) for found, writes in ((all_here, here), (all_there, there))]
            if a is not None and b is not None:
                both = index(a, b, z or b'', *was, has[0] - has[1], has[1] - has[0], pick)
                return both, None if both is not None else 'was changed on both machines in the same lines: read both, then --mine or --theirs'
            if pick and (a if pick == 'mine' else b) is not None:
                return (a if pick == 'mine' else b), None
            if z is not None and (a is not None or b is not None):
                return None, 'was deleted on one machine and is still on the other: bring it back with --mine or --theirs, or delete the other by hand'
            return (a if a is not None else b), None  # new on one machine, or on neither

        def index_writes():
            """Put MEMORY.md into the plan: each machine gets the whole without pointers to files it lacks."""
            for writes in (here, there):
                writes.pop(INDEX, None)
            keep.pop(INDEX, None), hand.pop(INDEX, None)
            whole, why = whole_index()
            sides = {}
            if why:
                hand[INDEX] = why
                keep.update({INDEX: base[INDEX]} if INDEX in base else {})
            elif whole is not None:
                whole = keep[INDEX] = pointing_at(whole, {*all_here, *here, *all_there, *there})  # a file on neither machine has no pointer
                for side, found, writes in (('here', all_here, here), ('there', all_there, there)):
                    sides[side] = pointing_at(whole, {*found, *writes})
                    if sides[side] != (local if side == 'here' else remote).get(INDEX):
                        writes[INDEX] = sides[side]
            return sides
        sides, late = index_writes(), set()
        if not dry and (here or there):
            now_here, now_there = read_all(folder)[0], fetch(cfg, pair, os.path.join(tmp, 'again'))[0]
            late = {n for n in {*here, *there, INDEX} if now_here.get(n) != all_here.get(n) or now_there.get(n) != all_there.get(n)}

            def leave(names):
                """Changed while this ran: the next run takes these, from the base as it was."""
                for n in names:
                    here.pop(n, None), there.pop(n, None), keep.pop(n, None)
                    keep.update({n: base[n]} if n in base else {})
            leave(late)
            sides = index_writes() if INDEX not in late else {}  # again: a pointer needs its file
            backup(state, all_here, all_there)
            # The plan, before anything is written: a run that dies from here on is finished by the next one.
            shutil.rmtree(os.path.join(state, 'pending'), ignore_errors=True)
            for n in {*here, *there} - {INDEX}:
                write(os.path.join(state, 'pending'), n, keep[n])
            for end, data in (('', keep.get(INDEX)), ('.here', sides.get('here')), ('.there', sides.get('there'))) if sides else ():
                write(os.path.join(state, 'pending'), INDEX + end, data)
            write(os.path.join(state, 'pending'), '.plan.json', json.dumps({n: [side for side, writes in (('here', here), ('there', there)) if n in writes]
                                                                             for n in {*here, *there}}).encode())
            if there:  # first: what the other machine does not take is not written here either
                refused = set(there) - push(cfg, pair, there, {n: d for n, d in now_there.items() if n in there}, tmp)
                late |= refused
                leave(refused)
            for n, data in list(here.items()):
                try:
                    with open(os.path.join(folder, n), 'rb') as f:
                        current = f.read()
                except OSError:
                    current = None
                if current != all_here.get(n):  # edited during the push
                    late.add(n)
                    leave([n])
                else:
                    write(folder, n, data)
        if not dry:
            for n, data in keep.items():
                if n not in late and stored.get(n) != data:
                    write(os.path.join(state, 'base'), n, data)
            for n in set(base) - set(keep) - late:
                os.remove(os.path.join(state, 'base', n))
            for side, data in sides.items() if INDEX not in late and INDEX not in hand else ():
                write(os.path.join(state, 'base'), f'{INDEX}.{side}', data)
            shutil.rmtree(os.path.join(state, 'pending'), ignore_errors=True)
            shutil.rmtree(os.path.join(state, 'theirs'), ignore_errors=True)
            for n in hand:
                if n in remote:
                    write(os.path.join(state, 'theirs'), n, remote[n])
    print(f'{name} ({pair["host"]}): {len({*local, *remote})} files' + (', dry run: nothing written' if dry else ''))
    both = sorted(set(here) & set(there) - {INDEX})
    for title, names in ((f'to {pair["host"]}', sorted(set(there) - set(both))), ('to this machine', sorted(set(here) - set(both))), ('merged on both', both),
                         ('changed while this ran, left for the next run', sorted(late)), ('left alone, a name this tool does not pass on', odd)):
        if names:
            print(f'  {title}: {", ".join(names)}')
    for n, why in sorted(hand.items()):
        print(f'  needs a hand: {n} {why}' + (f' (the other copy: {os.path.join(state, "theirs", n)})' if n in remote and not dry else ''))
    if not (here or there or hand or late):
        print('  in step')
    return not hand and not late


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
            worst = max(worst, 0 if sync(cfg, name, args.dry, set(args.mine), set(args.theirs)) else 1)
        except (Stop, OSError) as why:
            print(f'{name}: not synced: {why}', file=sys.stderr)
            worst = 2
    sys.exit(worst)


if __name__ == '__main__':
    main()
