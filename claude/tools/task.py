#!/usr/bin/env python3
"""One folder per task for what a session builds or tests, and a record of what it leaves elsewhere.

  task.py open NAME                  make <artifacts>/NAME and print its path; outputs go in there
  task.py add NAME dir PATH          a folder this task made outside its own
  task.py add NAME process PID       a process this task started and must not leave running
  task.py add NAME remote HOST PATH  a folder this task made on another machine
  task.py add NAME command TEXT      anything else: the command that puts it back (--shell, --why)
  task.py close NAME                 undo those, newest first, then delete the folder
  task.py sweep [--json]             list task folders that look finished; deletes nothing
  task.py daily                      the SessionStart hook: one line, at most once a day
  task.py caches [--days N] [--apply] [--json]
                                     list Cargo target folders idle N days (3); --apply deletes them
  task.py sessions [--days N] [--apply] [--json]
                                     list dead Orca project dirs & Claude transcripts older than N days (14)

~/.claude/machine.json says where the folders go on this machine:

  {"artifacts": "D:/CodexArtifacts",       one folder per task below this
   "roots": ["C:/tests"],                  further places a `dir` entry may lie in, besides the temp folder
   "keep": ["next-release", "C:/tests/x"], never deleted, with what they hold and what holds them
   "hosts": {"nuc": {"ssh": "nuc", "shell": "sh", "roots": ["/mnt/hdd2"]}},
   "ssh": "C:/Windows/System32/OpenSSH/ssh.exe"}   only when the ssh on PATH is the wrong one

`caches` looks below the artifact folder and the roots for Cargo target folders, whose content Cargo
can build again. It leaves out what is kept, links, the pools of build locks (Rubylight prunes those
with `tools/build_lock.py --prune`) and a target beside or in a checkout that git worked in lately.
It skips, and names, a target that holds anything Cargo does not write ("mixed") or a git checkout,
and with --apply one whose build lock is held, whose program runs or that was built in meanwhile.

Apart from `caches --apply`, only `close` deletes, and only what the task itself can have made. A folder elsewhere must lie in
the temp folder or a root and hold none of those, be no link, hold no checkout and nothing kept, and
have been made after the task was opened; where the file system keeps no creation time (Linux) it
must be <root>/NAME or lie in it. A process must still have the start time it was recorded with. A
folder on another machine must be <that host's root>/NAME or lie in it. The task's own folder is not
deleted while it holds a checkout, nor on Windows while a shell or a program still uses it. A
worktree is never removed here; `sweep` names the ones that closed tasks left, for removal through
Orca or git.
"""
import argparse, base64, contextlib, json, os, platform, re, shlex, shutil, signal, stat, subprocess, sys, tempfile, time

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
SLACK = 2  # seconds a clock or a file system may be off
IDLE_DAYS = 3
DEPTH = 4  # how far below the artifact folder or a root `caches` looks for a target folder
MARKERS = ('.rustc_info.json', 'CACHEDIR.TAG')
LOCKS = ('.cargo-lock', '.cargo-build-lock', '.cargo-artifact-lock')
CARGO_FILES = (*MARKERS, *LOCKS, '.rustc_info.json.lock', '.package-cache', '.future-incompat-report.json',
               '.rubylight-checkout')  # the last: Rubylight's dev.py
# What Cargo and its subcommands put beside the profiles and target triples.
CARGO_DIRS = re.compile(r'debug|release|doc|package|tmp|cargo-timings|nextest|llvm-cov-target|miri|rust-analyzer|flycheck\d*')
RUSTDOC_FILES = re.compile(r'\.lock|\.rustdoc_fingerprint\.json|(?:crates|src-files|source-files|search-index|search-desc)\.js|(?:help|settings|index)\.html')
RUSTDOC_DIRS = ('static.files', 'src', 'search.desc', 'search.index', 'trait.impl', 'type.impl', 'implementors')
SHELLS = {'powershell': ['powershell', '-NoProfile', '-NonInteractive', '-Command'], 'pwsh': ['pwsh', '-NoProfile', '-NonInteractive', '-Command'],
          'bash': ['bash', '-c'], 'sh': ['sh', '-c'], 'cmd': ['cmd', '/c']}


def fail(why):
    sys.exit(f'task.py: {why}')


def real(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(str(path)))))


def inside(path, root):
    """`path` lies below `root` and is not `root` itself; links are followed first."""
    path, root = real(path), real(root)
    try:
        return path != root and os.path.commonpath([path, root]) == root
    except ValueError:  # another drive
        return False


def linked(path):
    """A symlink, or on Windows a junction: it looks like a folder here and lies somewhere else."""
    try:
        return os.path.islink(path) or bool(getattr(os.lstat(path), 'st_file_attributes', 0) & 0x400)  # a reparse point
    except OSError:
        return False


def checkout_in(folder):
    """The first folder in `folder`, itself included, that is a git checkout; links are not followed."""
    todo = [folder]
    while todo:
        here = todo.pop()
        try:
            with os.scandir(here) as found:
                for entry in found:
                    if entry.name == '.git':
                        return here
                    if entry.is_dir(follow_symlinks=False) and not linked(entry.path):
                        todo.append(entry.path)
        except OSError:
            continue
    return None


def settings():
    path = os.environ.get('CLAUDE_MACHINE_FILE') or os.path.join(HOME, 'machine.json')
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError) as error:
        fail(f'cannot read {path} ({error}). It says where task folders go on this machine, for example '
             '{"artifacts": "D:/CodexArtifacts", "roots": [], "keep": [], "hosts": {}}')
    if not isinstance(data, dict) or not data.get('artifacts'):
        fail(f'{path} names no "artifacts" folder')
    root, ssh = os.path.realpath(os.path.abspath(os.path.expanduser(str(data['artifacts'])))), data.get('ssh') or 'ssh'
    return {'artifacts': root, 'roots': [real(p) for p in data.get('roots') or []],
            'keep': [real(p if os.path.isabs(os.path.expanduser(p)) else os.path.join(root, p)) for p in data.get('keep') or []],
            'hosts': data.get('hosts') or {}, 'ssh': [ssh] if isinstance(ssh, str) else list(ssh)}


def kept(path, cfg):
    """Never deleted: a kept folder, anything in one, and anything that holds one."""
    path = real(path)
    return any(path == keep or inside(path, keep) or inside(keep, path) for keep in cfg['keep'])


def folder_of(cfg, name):
    if not NAME.match(name):
        fail(f'"{name}" is no task name: letters, digits, dot, dash and underscore, at most 64')
    return os.path.join(cfg['artifacts'], name)


def read(path):
    try:
        with open(path, encoding='utf-8') as f:
            task = json.load(f)
        return task if isinstance(task, dict) and isinstance(task.get('entries', []), list) else None
    except (OSError, ValueError):
        return None


def born(path):
    """When a folder was made, where the file system keeps that; None where it does not."""
    info = os.stat(path)
    if hasattr(info, 'st_birthtime'):
        return info.st_birthtime
    return info.st_ctime if os.name == 'nt' else None


def load(cfg, name):
    """The task's folder and its record. A close that could not finish left the folder under another name."""
    for folder in (folder_of(cfg, name), os.path.join(cfg['artifacts'], '.closing-' + name)):
        task = read(os.path.join(folder, 'task.json'))
        if task:
            if linked(folder) or not inside(folder, cfg['artifacts']):
                fail(f'{folder} is a link to another place, so it is no task folder; nothing was touched')
            # The name is the folder's and the task cannot be older than its folder, whatever the file says.
            task.update(name=name, entries=task.get('entries') or [])
            try:
                task['opened'] = max(float(task.get('opened') or 0), born(folder) or 0)
            except (OSError, TypeError, ValueError):
                task['opened'] = time.time()
            return folder, task
    fail(f'{folder_of(cfg, name)} has no task.json: open the task first (task.py open {name})')


def save(folder, task):
    with open(os.path.join(folder, 'task.json.tmp'), 'w', encoding='utf-8') as f:
        json.dump(task, f, indent=1)
    os.replace(os.path.join(folder, 'task.json.tmp'), os.path.join(folder, 'task.json'))


def abandoned(lock):
    """The task.py that wrote this lock runs no more; where that cannot be asked, the lock is 15 minutes old."""
    try:
        with open(lock, encoding='utf-8') as f:
            pid, started = f.read().split()
        now = process(int(pid)) if int(pid) else 'denied'
        if now is None:
            return True
        if now != 'denied':
            return abs(now[0] - float(started)) > SLACK
    except (OSError, ValueError):  # just made and not yet written, or gone again
        pass
    try:
        return time.time() - os.path.getmtime(lock) > 900
    except OSError:
        return False


@contextlib.contextmanager
def held(cfg, name):
    """One task.py at a time changes a task's record."""
    lock, deadline, me = os.path.join(cfg['artifacts'], f'.lock-{name}'), time.time() + 20, process(os.getpid())
    while True:
        try:
            with os.fdopen(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY), 'w', encoding='utf-8') as f:
                f.write(f'{os.getpid()} {me[0]}' if isinstance(me, tuple) else '0 0')  # a system that cannot say: by age
            break
        except FileExistsError:
            if time.time() > deadline:  # also when an abandoned lock cannot be removed
                fail(f'another task.py is working on {name}, or {lock} cannot be removed; try again when it is done')
            if abandoned(lock):
                with contextlib.suppress(OSError):
                    os.remove(lock)
            time.sleep(0.1)
        except OSError as error:
            fail(f'cannot write in {cfg["artifacts"]}: {error}')
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            os.remove(lock)


def git(cwd, *args):
    """A git command's output, or None when it failed."""
    try:
        done = subprocess.run(['git', '-C', cwd, *args], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def not_ours(cfg, task, path):
    """Why this task may not delete the folder `path`, or None when it may."""
    if linked(path):
        return 'it is a link, not a folder of its own'
    if any(real(path) == real(shared) or inside(shared, path) for shared in (cfg['artifacts'], *cfg['roots'], tempfile.gettempdir())):
        return 'it is, or holds, the artifact folder, a root or the temp folder, which all tasks share'
    if inside(path, cfg['artifacts']):
        return 'folders in the artifact folder belong to their own tasks; this task\'s own goes at close'
    roots = [root for root in [*cfg['roots'], tempfile.gettempdir()] if inside(path, root)]
    if not roots:
        return 'it lies outside the temp folder and the roots in machine.json'
    root = max(roots, key=lambda root: len(real(root)))  # of roots that lie in each other, the one nearest to the folder
    if kept(path, cfg):
        return 'it is on the keep list, or holds something that is'
    nested = checkout_in(path)
    if nested:
        return f'it holds a checkout ({nested}); a worktree or clone goes through Orca or git'
    made = born(path)
    if made is None:
        if os.path.relpath(real(path), real(root)).replace('\\', '/').split('/')[0] != os.path.normcase(task['name']):
            return f'this system keeps no creation time, so it must be {os.path.join(root, task["name"])} or lie in it'
    elif made < task['opened'] - SLACK:
        return 'it was there before the task was opened'
    return None


def process(pid):
    """(start time, program) of a running process, 'denied' when it may not be asked, None when there is none."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes as w
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.OpenProcess.restype, k.OpenProcess.argtypes = w.HANDLE, (w.DWORD, w.BOOL, w.DWORD)
        k.GetExitCodeProcess.argtypes = (w.HANDLE, ctypes.POINTER(w.DWORD))
        k.GetProcessTimes.argtypes = (w.HANDLE,) + (ctypes.POINTER(w.FILETIME),) * 4
        k.QueryFullProcessImageNameW.argtypes = (w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD))
        k.CloseHandle.argtypes = (w.HANDLE,)
        handle = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return 'denied' if ctypes.get_last_error() == 5 else None
        try:
            code, times, size = w.DWORD(), [w.FILETIME() for _ in range(4)], w.DWORD(32768)
            name = ctypes.create_unicode_buffer(size.value)
            if not k.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
                return None
            if not k.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return 'denied'
            k.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size))
            return ((times[0].dwHighDateTime << 32 | times[0].dwLowDateTime) - 116444736000000000) / 1e7, name.value
        finally:
            k.CloseHandle(handle)
    try:
        with open(f'/proc/{pid}/stat') as f:
            fields = f.read().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':  # ended, not yet collected by its parent
            return None
        with open('/proc/stat') as f:
            boot = next(int(line.split()[1]) for line in f if line.startswith('btime'))
        started = boot + int(fields[19]) / os.sysconf('SC_CLK_TCK')
    except (OSError, IndexError, ValueError, StopIteration):
        return None
    try:
        return started, os.readlink(f'/proc/{pid}/exe')
    except OSError:
        return started, ''


def family(pid):
    """On Linux, a process and everything it started, the youngest first."""
    children = {}
    for name in os.listdir('/proc'):
        if name.isdigit():
            try:
                with open(f'/proc/{name}/stat') as f:
                    children.setdefault(int(f.read().rsplit(')', 1)[1].split()[1]), []).append(int(name))
            except (OSError, IndexError, ValueError):
                pass
    found, todo = [], [pid]
    while todo:
        found.append(todo.pop())
        todo.extend(children.get(found[-1], []))
    return found[::-1]


def end(entry):
    """End a recorded process and what it started, if it is still the process that was recorded."""
    now = process(entry['pid'])
    if now is None:
        return True, 'already ended'
    if now == 'denied':
        return False, 'this account may not ask about or end it'
    # A program may replace itself (exec) and keep its number and start time, so on Linux the time alone decides.
    if abs(now[0] - entry['started']) > SLACK or os.name == 'nt' and os.path.normcase(now[1]) != os.path.normcase(entry.get('program') or ''):
        return True, 'its number now belongs to another process, which stays'
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(entry['pid']), '/T', '/F'], capture_output=True, timeout=30)
    else:
        ours = family(entry['pid'])
    for attempt in range(50):
        if os.name != 'nt' and attempt in (0, 25):
            for pid in ours:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL if attempt else signal.SIGTERM)
        if process(entry['pid']) is None:
            return True, 'ended'
        time.sleep(0.2)
    return False, 'it did not end'


def remote_path(cfg, task, host, path):
    """The path as it is deleted on `host`; exits when this task may not name it."""
    there = cfg['hosts'].get(host)
    if not isinstance(there, dict):
        fail(f'machine.json has no host "{host}" (it has: {", ".join(sorted(cfg["hosts"])) or "none"})')
    path = str(path).replace('\\', '/').rstrip('/')
    fold = str.lower if there.get('shell') == 'powershell' else str
    roots = [fold(str(root).replace('\\', '/').rstrip('/')) + '/' for root in there.get('roots') or []]
    below = [fold(path)[len(root):].split('/') for root in roots if fold(path).startswith(root)]
    if not below or any(root.startswith(fold(path) + '/') for root in roots):
        fail(f'{path} is not below the roots machine.json gives for {host}')
    parts = min(below, key=len)  # of roots that lie in each other, the one nearest to the folder
    if parts[0] != fold(task['name']) or {'', '.', '..'} & set(parts):
        fail(f'a folder on another machine must be <root>/{task["name"]} or lie in it, with no relative parts, so that only this task\'s folder can go')
    return path


def remove(path):
    """Delete a file, a link or a folder with what it holds; a link goes, what it points to stays."""
    def writable(target):  # a read-only file, as git and some builds leave them; never called for a link
        if os.name == 'nt':
            os.chmod(target, stat.S_IWRITE)
        else:  # deleting needs a writable folder, not a writable file
            parent = os.path.dirname(target)
            os.chmod(parent, os.stat(parent).st_mode | stat.S_IWUSR | stat.S_IXUSR)

    def again(function, target, info):
        if linked(target) or function not in (os.unlink, os.remove, os.rmdir):  # only a delete can be tried again
            raise info if isinstance(info, BaseException) else info[1]
        writable(target)
        function(target)
    if linked(path):
        try:
            os.remove(path)
        except OSError:
            os.rmdir(path)  # a link to a folder, on Windows
    elif os.path.isdir(path):
        shutil.rmtree(path, **({'onexc': again} if sys.version_info >= (3, 12) else {'onerror': again}))
    else:
        try:
            os.remove(path)
        except OSError:
            writable(path)
            os.remove(path)


def undo(cfg, task, entry):
    """Put one recorded thing back. Returns whether that worked, and what happened."""
    kind = entry.get('type')
    if kind == 'dir':
        if not os.path.lexists(entry['path']):
            return True, 'already gone'
        why = not_ours(cfg, task, entry['path'])
        if why:
            return False, why
        remove(entry['path'])
        return not os.path.lexists(entry['path']), 'deleted'
    if kind == 'process':
        return end(entry)
    if kind == 'remote':
        path, there = remote_path(cfg, task, entry['host'], entry['path']), cfg['hosts'][entry['host']]
        if there.get('shell') == 'powershell':
            quoted = re.sub('[\'\u2018\u2019\u201a\u201b]', lambda found: found.group(0) * 2, path)  # each ends a string there
            script = f"$ErrorActionPreference = 'Stop'; if (Test-Path -LiteralPath '{quoted}') {{ Remove-Item -LiteralPath '{quoted}' -Recurse -Force }}"
            command = 'powershell -NoProfile -NonInteractive -EncodedCommand ' + base64.b64encode(script.encode('utf-16-le')).decode()
        else:
            command = 'rm -rf -- ' + shlex.quote(path)
        argv = [*cfg['ssh'], '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', str(there.get('ssh') or entry['host']), command]
    elif kind == 'command':
        if entry.get('shell') not in SHELLS:
            return False, f'unknown shell: {entry.get("shell")}'
        argv = [*SHELLS[entry['shell']], str(entry['text'])]
    else:
        return False, f'unknown kind of entry: {kind}'
    done = subprocess.run(argv, capture_output=True, text=True, errors='replace', timeout=300)
    said = (done.stderr or done.stdout or '').strip().splitlines()
    return done.returncode == 0, 'done' if done.returncode == 0 else f'exit {done.returncode}: {said[-1][:200] if said else "no output"}'


def shown(entry):
    if not isinstance(entry, dict):
        return repr(entry)[:80]
    return {'dir': entry.get('path'), 'process': f'{entry.get("pid")} {entry.get("program")}', 'remote': f'{entry.get("host")}:{entry.get("path")}',
            'command': entry.get('why') or entry.get('text')}.get(entry.get('type'), '?')


def open_task(cfg, name):
    folder = folder_of(cfg, name)
    if kept(folder, cfg):
        fail(f'{folder} is on the keep list; pick another name')
    if os.path.lexists(os.path.join(cfg['artifacts'], '.closing-' + name)):
        fail(f'a close of {name} did not finish; run `task.py close {name}` again first')
    os.makedirs(cfg['artifacts'], exist_ok=True)
    with held(cfg, name):
        if linked(folder) or not read(os.path.join(folder, 'task.json')):
            if os.path.lexists(folder) and (linked(folder) or not os.path.isdir(folder) or os.listdir(folder)):
                fail(f'{folder} is there already and was not opened by this tool; pick another name')
            os.makedirs(folder, exist_ok=True)
            top = git(os.getcwd(), 'rev-parse', '--show-toplevel')
            save(folder, {'name': name, 'opened': time.time(), 'machine': platform.node(), 'worktree': top and os.path.normpath(top),
                          'branch': top and git(top, 'branch', '--show-current'), 'entries': []})
    print(folder)


def add(cfg, args):
    with held(cfg, args.name):
        folder, task = load(cfg, args.name)
        entry = {'type': args.kind, 'added': time.time()}
        if len(args.value) != {'dir': 1, 'process': 1, 'remote': 2}.get(args.kind, len(args.value)):
            fail(f'usage: add NAME {args.kind} ' + ('HOST PATH' if args.kind == 'remote' else 'VALUE'))
        if args.kind == 'dir':
            if not os.path.isdir(args.value[0]):
                fail(f'{args.value[0]} is no folder')
            why = not_ours(cfg, task, args.value[0])
            if why:
                fail(f'{args.value[0]} cannot be this task\'s to delete: {why}')
            entry['path'] = os.path.abspath(args.value[0])
        elif args.kind == 'process':
            try:
                entry['pid'] = int(args.value[0])
            except ValueError:
                fail(f'{args.value[0]} is no process number')
            now = process(entry['pid'])
            if not now or now == 'denied':
                fail(f'process {entry["pid"]} ' + ('may not be asked about by this account' if now else 'is not running'))
            if now[0] < task['opened'] - SLACK:
                fail(f'process {entry["pid"]} was running before the task was opened')
            entry['started'], entry['program'] = now
        elif args.kind == 'remote':
            entry['host'], entry['path'] = args.value[0], remote_path(cfg, task, *args.value)
        else:
            entry.update(text=' '.join(args.value), shell=args.shell or ('powershell' if os.name == 'nt' else 'sh'), why=args.why)
        same = {key: value for key, value in entry.items() if key != 'added'}
        if not any(isinstance(old, dict) and {key: value for key, value in old.items() if key != 'added'} == same for old in task['entries']):
            task['entries'].append(entry)
            save(folder, task)
    print(f'recorded for {args.name}: {args.kind} {shown(entry)}')


def note_closed(cfg, task):
    """Remember a closed task while its worktree is still there, for `sweep` to name."""
    closed = os.path.join(cfg['artifacts'], '.closed')
    os.makedirs(closed, exist_ok=True)
    for old in os.listdir(closed):  # a closed task whose worktree is gone as well leaves nothing to say
        seen = read(os.path.join(closed, old))
        if seen is None or not seen.get('worktree') or not os.path.isdir(seen['worktree']):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(closed, old))
    if task.get('worktree') and os.path.isdir(task['worktree']):
        with open(os.path.join(closed, task['name'] + '.json'), 'w', encoding='utf-8') as f:
            json.dump({**{key: task.get(key) for key in ('name', 'opened', 'machine', 'worktree', 'branch')}, 'closed': time.time()}, f)


def close(cfg, name):
    with held(cfg, name):
        closing = os.path.join(cfg['artifacts'], '.closing-' + name)
        if os.path.lexists(closing) and not any(read(os.path.join(folder, 'task.json')) for folder in (folder_of(cfg, name), closing)):
            try:  # task.json goes last: all that was recorded is put back, and only the emptied folder could not go
                os.rmdir(closing)
            except OSError as error:
                fail(f'{closing} could not be deleted ({error}); run `task.py close {name}` again once nothing uses it')
            return print(f'closed {name}: {folder_of(cfg, name)} deleted')
        folder, task = load(cfg, name)
        if kept(folder_of(cfg, name), cfg):
            fail(f'{folder} is on the keep list')
        nested = checkout_in(folder)
        if nested:
            fail(f'{nested} is a checkout inside the task folder. Nothing was undone or deleted: move it out or remove it yourself '
                 '(a worktree through Orca or git), then close again.')
        pending, left = list(task['entries']), []
        while pending:
            entry = pending.pop()
            try:
                done, what = undo(cfg, task, entry) if isinstance(entry, dict) else (False, 'not an entry')
            except SystemExit as refused:  # machine.json no longer allows what was recorded
                done, what = False, str(refused.code)
            except Exception as error:  # a broken entry, a program that is missing, a timeout
                done, what = False, f'{type(error).__name__}: {error}'
            print(f'{"ok  " if done else "LEFT"} {entry.get("type") if isinstance(entry, dict) else "?"} {shown(entry)}: {what}')
            if not done:
                left.insert(0, entry)
            save(folder, {**task, 'entries': pending + left})  # after each one, so a close that dies can be run again
        if left:
            fail(f'{len(left)} of this task\'s leftovers could not be put back; {folder} stays. Fix them and close again.')
        if folder != closing:
            try:
                os.rename(folder, closing)  # refused while a shell or a program still uses the folder, before anything is deleted
            except OSError as error:
                fail(f'{folder} is in use ({error}). Nothing in it was deleted; close again once nothing has it open or stands in it.')
        try:
            with os.scandir(closing) as found:
                for path in [entry.path for entry in found if entry.name != 'task.json']:
                    remove(path)
            with contextlib.suppress(OSError):
                note_closed(cfg, task)
            os.remove(os.path.join(closing, 'task.json'))  # last, so that a close that fails here can be run again
            os.rmdir(closing)
        except OSError as error:
            fail(f'{closing} could not be deleted ({error}); run `task.py close {name}` again once nothing uses it')
    print(f'closed {name}: {folder_of(cfg, name)} deleted')


def newest(folder):
    """The latest change to a folder or to what lies directly in it."""
    try:
        with os.scandir(folder) as found:
            return max([os.path.getmtime(folder)] + [entry.stat(follow_symlinks=False).st_mtime for entry in found])
    except OSError:
        return 0


def last_used(tree):
    """When git last worked in a checkout: its index is written by nearly everything a session does there."""
    index = git(tree, 'rev-parse', '--path-format=absolute', '--git-path', 'index')
    try:
        return max(newest(tree), os.path.getmtime(index) if index else 0, int(git(tree, 'log', '-1', '--format=%ct') or 0))
    except (OSError, ValueError):
        return newest(tree)


def tree_state(tree, quick=False):
    if not tree:
        return 'none'
    if not os.path.isdir(tree):
        return 'gone'
    if quick or git(tree, 'rev-parse', '--verify', '-q', 'origin/main') is None:
        return 'there'
    return 'merged' if git(tree, 'merge-base', '--is-ancestor', 'HEAD', 'origin/main') is not None else 'unmerged'


def rows(cfg, quick=False):
    """Every folder in the artifact root with what is known of its task, and the worktrees that closed tasks left.
    `quick` asks git nothing, for the hook that must not take long."""
    now, found = time.time(), []
    try:
        names = sorted(os.listdir(cfg['artifacts']))
    except OSError:
        return found
    for name in names:
        folder = os.path.join(cfg['artifacts'], name)
        if name == '.closed' or name.startswith('.lock-') or not os.path.isdir(folder):
            continue
        task, idle = read(os.path.join(folder, 'task.json')), (now - newest(folder)) / 86400
        if name.startswith('.closing-'):
            found.append({'name': name[len('.closing-'):], 'state': 'closing', 'idle_days': round(idle, 1), 'finished': True})
        elif not task:
            found.append({'name': name, 'state': 'unlisted', 'idle_days': round(idle, 1), 'kept': kept(folder, cfg)})
        else:
            tree = tree_state(task.get('worktree'), quick)
            if tree not in ('none', 'gone') and not quick:
                idle = min(idle, (now - last_used(task['worktree'])) / 86400)
            found.append({'name': name, 'state': 'open', 'idle_days': round(idle, 1), 'worktree': task.get('worktree'), 'tree': tree,
                          'entries': len(task.get('entries') or []), 'finished': tree == 'gone' or idle >= IDLE_DAYS})
    closed = os.path.join(cfg['artifacts'], '.closed')
    for name in sorted(os.listdir(closed)) if os.path.isdir(closed) else []:
        task = read(os.path.join(closed, name))
        if task and task.get('worktree') and os.path.isdir(task['worktree']):
            found.append({'name': task.get('name') or name[:-5], 'state': 'closed', 'worktree': task['worktree'], 'tree': tree_state(task['worktree'], quick)})
    return found


def sweep(cfg, as_json):
    found = rows(cfg)
    if as_json:
        return print(json.dumps(found, indent=1))
    print(f'{cfg["artifacts"]}: this deletes nothing; `task.py close NAME` does, for a task with a task.json.')
    for title, some in (('Look finished (worktree gone, or untouched for %d days)' % IDLE_DAYS, [r for r in found if r.get('finished')]),
                        ('Open', [r for r in found if r['state'] == 'open' and not r.get('finished')])):
        print(f'\n{title}: {len(some)}')
        for r in some:
            print(f'  {r["name"]}: a close did not finish, run it again' if r['state'] == 'closing' else
                  f'  {r["name"]}: idle {r["idle_days"]} days, worktree {r["tree"]}' + (f' ({r["worktree"]})' if r.get('worktree') else '') +
                  (f', {r["entries"]} recorded outside its folder' if r["entries"] else ''))
    some = [r for r in found if r['state'] == 'closed']
    print(f'\nClosed, worktree still there (remove it through Orca or git once no session lives in it): {len(some)}')
    for r in some:
        print(f'  {r["name"]}: {r["worktree"]}, {r["tree"]}')
    some = [r for r in found if r['state'] == 'unlisted']
    print(f'\nNo task.json, so nothing here will touch them: {len(some)}')
    for r in some:
        print(f'  {r["name"]}: idle {r["idle_days"]} days' + (', on the keep list' if r['kept'] else ''))


def entries(folder):
    try:
        with os.scandir(folder) as found:
            return list(found)
    except OSError:
        return []


def folders(folder):
    """The folders directly in `folder` that are no links."""
    return [entry.path for entry in entries(folder) if entry.is_dir(follow_symlinks=False) and not linked(entry.path)]


def profile(folder):
    """A build profile, or a target triple that holds some."""
    return any(os.path.isdir(os.path.join(here, '.fingerprint')) for here in (folder, *folders(folder)))


def tagged(folder):
    try:
        with open(os.path.join(folder, 'CACHEDIR.TAG'), encoding='utf-8', errors='replace') as f:
            return 'created by cargo' in f.read(1000) and os.path.isfile(os.path.join(folder, '.rustc_info.json'))
    except OSError:
        return False


def pages(folder):
    """A folder of rustdoc pages: only .html and .js files, at any depth."""
    return all(pages(item.path) if item.is_dir(follow_symlinks=False) else item.name.endswith(('.html', '.js'))
               for item in entries(folder))


def rustdoc(folder):
    """Only what rustdoc writes: its own files and folders, and one folder of pages for each crate."""
    for entry in entries(folder):
        if not entry.is_dir(follow_symlinks=False):
            if not RUSTDOC_FILES.fullmatch(entry.name):
                return False
        elif entry.name not in RUSTDOC_DIRS and not pages(entry.path):
            return False
    return True


def recognised(entry):
    """doc, package or tmp of a folder without a tag, judged by what it holds."""
    if entry.name == 'doc':
        return rustdoc(entry.path)
    if entry.name == 'tmp':
        return not entries(entry.path)
    return all(re.fullmatch(r'tmp-crate|tmp-registry|.+-\d+\.\d+\.\d+.*', item.name) if item.is_dir(follow_symlinks=False) else
               item.name.endswith('.crate') or item.name in LOCKS for item in entries(entry.path))


def only_cargo(folder, marked):
    """Nothing in `folder` but what Cargo writes. `marked`: its tag says so, so doc, package and tmp are not looked into."""
    for entry in entries(folder):
        if not entry.is_dir(follow_symlinks=False):
            if entry.name not in CARGO_FILES:
                return False
        elif entry.name in ('doc', 'package', 'tmp'):
            if not (marked or recognised(entry)):
                return False
        elif not (CARGO_DIRS.fullmatch(entry.name) or os.path.isdir(os.path.join(entry.path, '.fingerprint'))
                  or profile(entry.path) and only_cargo(entry.path, marked or tagged(entry.path))):
            return False
    return True


def cargo_target(folder):
    """None for a folder Cargo did not build into, else whether it holds nothing but Cargo's. A folder Cargo made says
    so in its CACHEDIR.TAG, one that was there before has none; either may be a task folder with logs and evidence."""
    marked = tagged(folder)
    if not marked and not any(os.path.isdir(os.path.join(folder, name, '.fingerprint')) for name in ('debug', 'release')):
        return None
    return only_cargo(folder, marked)


def pooled(path):
    """A slot of a build lock's target pool, or a pool itself; whoever holds the locks prunes it."""
    return (any('target-pool' in part.lower() for part in real(path).replace('\\', '/').split('/'))
            or any(os.path.exists(path + suffix) for suffix in ('.lock', '.holder')))


def last_built(target):
    """The latest change to a target folder, its profiles (also below a target triple) and their fingerprints."""
    latest = newest(target)
    for outer in folders(target):
        latest = max(latest, newest(outer))
        for here in (outer, *folders(outer)):
            fingerprints = os.path.join(here, '.fingerprint')
            if os.path.isdir(fingerprints):
                latest = max(latest, newest(here), newest(fingerprints))
    return latest


def size_of(folder):
    total, todo = 0, [folder]
    while todo:
        for entry in entries(todo.pop()):
            if entry.is_dir(follow_symlinks=False):
                if not linked(entry.path):
                    todo.append(entry.path)
            else:
                with contextlib.suppress(OSError):
                    total += entry.stat(follow_symlinks=False).st_size
    return total


def idle_days(path, trees, days):
    """How long a target has been idle; None when it or a checkout beside it was used within `days`."""
    now = time.time()
    idle = (now - last_built(path)) / 86400
    if idle < days or any((now - last_used(tree)) / 86400 < days for tree in set(trees) - {path}):
        return None
    return idle


def targets(cfg, days):
    """The Cargo target folders that may go: below the artifact folder and the roots, and idle `days` days. Also
    those left alone for what they hold."""
    found, skipped, seen = [], [], set()
    todo = [(root, 0, ()) for root in (cfg['artifacts'], *cfg['roots'])]
    while todo:
        folder, depth, trees = todo.pop()
        inner = folders(folder)
        trees += tuple(tree for tree in (folder, *inner) if os.path.exists(os.path.join(tree, '.git')))
        for path in inner:
            if real(path) in seen or pooled(path):
                continue
            seen.add(real(path))
            pure = cargo_target(path)
            if not pure:
                if pure is False and not kept(path, cfg):
                    skipped.append({'path': path, 'why': 'mixed'})
                if depth + 1 < DEPTH:
                    todo.append((path, depth + 1, trees))
                continue
            if kept(path, cfg) or os.path.exists(os.path.join(path, 'task.json')):
                continue
            idle = idle_days(path, trees, days)
            if idle is None:
                continue
            if checkout_in(path):
                skipped.append({'path': path, 'why': 'holds a checkout'})
                continue
            found.append({'path': path, 'bytes': size_of(path), 'idle_days': round(idle, 1), 'trees': trees})
    return sorted(found, key=lambda row: -row['bytes']), sorted(skipped, key=lambda row: row['path'])


def profiles(target, depth=3):
    """The folders in a target that Cargo locks or builds into: profiles, also below triples and nested targets."""
    for path in folders(target):
        if any(os.path.lexists(os.path.join(path, name)) for name in (*LOCKS, '.fingerprint')):
            yield path
        elif depth > 1:
            yield from profiles(path, depth - 1)


def lock(path):
    """Take Cargo's lock on this file without waiting; the open file that holds it, or OSError when it is held."""
    handle = open(path, 'r+b')
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # one byte of the whole file that Cargo locks
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise
    return handle


def running(program):
    """A program that cannot be opened for writing alone: it runs, or something holds it."""
    if os.name != 'nt':
        try:
            with open(program, 'r+b'):
                return False
        except OSError:
            return True
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                   wintypes.DWORD, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW('\\\\?\\' + os.path.abspath(program), 0x40000000, 0, None, 3, 0, None)  # write, share nothing
    if handle == wintypes.HANDLE(-1).value:
        return True
    kernel.CloseHandle(handle)
    return False


def prune(folder, errors):
    """Delete what `folder` holds except what a later run needs to know it as a target: fingerprints, tags and locks."""
    for entry in entries(folder):
        if entry.name == '.fingerprint' or entry.name in (*MARKERS, *LOCKS):
            continue
        try:
            if entry.is_dir(follow_symlinks=False) and not linked(entry.path):
                prune(entry.path, errors)
                if not entries(entry.path):
                    remove(entry.path)
            else:
                remove(entry.path)
        except OSError as error:
            errors.append(error)


def clear(target, held=()):
    """Delete a target folder. What marks it as one goes last, so that a later run finds what a locked file left."""
    errors = []
    prune(target, errors)
    if errors:
        raise errors[0]
    for handle in held:  # Windows deletes no open file
        handle.close()
    for entry in sorted(entries(target), key=lambda entry: entry.name in MARKERS):
        remove(entry.path)
    remove(target)


def delete(row, days):
    """Delete a listed target and return None, or why it is in use after all."""
    held = []
    try:
        built = list(profiles(row['path']))
        for path in built:
            for name in LOCKS:
                file = os.path.join(path, name)
                if os.path.isfile(file):
                    try:
                        held.append(lock(file))
                    except OSError:
                        return f'in use: {file} is locked'
        if idle_days(row['path'], row['trees'], days) is None:
            return 'in use: no longer idle'
        for path in built:
            for folder in (path, os.path.join(path, 'deps'), os.path.join(path, 'examples')):
                for entry in entries(folder):
                    if entry.name.lower().endswith('.exe') and entry.is_file(follow_symlinks=False) and running(entry.path):
                        return f'in use: {entry.path} is running or locked'
        clear(row['path'], held)
    finally:
        for handle in held:
            handle.close()


def caches(cfg, days, apply, as_json):
    (found, skipped), freed, failed = targets(cfg, days), 0, []
    total = sum(row['bytes'] for row in found)
    for row in found if apply else []:
        try:
            why = delete(row, days)
            if why:
                skipped.append({'path': row['path'], 'why': why})
        except OSError as error:
            failed.append({'path': row['path'], 'error': str(error)})
        freed += row['bytes'] - (size_of(row['path']) if os.path.lexists(row['path']) else 0)
    for row in found:
        del row['trees']
    if as_json:
        return print(json.dumps({'applied': apply, 'days': days, 'targets': found, 'total_bytes': total, 'freed_bytes': freed,
                                 'skipped': skipped, 'failed': failed}, indent=1))
    for row in found:
        print(f'{row["bytes"] / 1e9:8.2f} GB  idle {row["idle_days"]:6.1f} days  {row["path"]}')
    print(f'{len(found)} Cargo target folders idle {days:g} days or more, {total / 1e9:.2f} GB')
    for row in skipped:
        print(f'skipped {row["path"]}: {row["why"]}')
    if not apply:
        tool = os.path.abspath(__file__).replace('\\', '/')
        return print(f'dry run, nothing deleted; to delete them: python "{tool}" caches --days {days:g} --apply')
    for row in failed:
        print(f'FAILED {row["path"]}: {row["error"]}')
    print(f'freed {freed / 1e9:.2f} GB, {len(failed)} failed')


def _claude_session_dirs(claude_home):
    """Return every live Claude sessions/ folder: claude_home, CLAUDE_CONFIG_DIR, ~/.claude, and Orca Claude profiles."""
    homes = [claude_home, os.environ.get('CLAUDE_CONFIG_DIR'), os.path.expanduser('~/.claude')]
    orca = os.environ.get('ORCA_USER_DATA_PATH') or (os.path.join(os.environ['APPDATA'], 'orca') if os.environ.get('APPDATA') else '')
    if orca:
        profiles = os.path.join(orca, 'claude-profiles')
        if os.path.isdir(profiles):
            for entry in entries(profiles):
                if entry.is_dir(follow_symlinks=False):
                    homes.append(os.path.join(entry.path, 'home'))
    seen, dirs = set(), []
    for h in homes:
        if not h:
            continue
        sdir = os.path.join(h, 'sessions')
        key = real(sdir)
        if key not in seen and os.path.isdir(sdir):
            seen.add(key)
            dirs.append(sdir)
    return dirs


def _live_sessions(claude_home):
    """Return (active_session_ids, active_cwds) for currently running Claude Code processes (incl. Orca profiles)."""
    sids, cwds = set(), set()
    for sdir in _claude_session_dirs(claude_home):
        for entry in entries(sdir):
            if not entry.name.endswith('.json'):
                continue
            try:
                with open(entry.path, encoding='utf-8') as f:
                    data = json.load(f)
                pid = int(data.get('pid', 0))
            except (OSError, ValueError, TypeError):
                continue
            if pid and process(pid) is not None:
                if data.get('sessionId'):
                    sids.add(str(data['sessionId']))
                if data.get('cwd'):
                    cwds.add(real(data['cwd']))
    return sids, cwds


def _claude_proj_key(path):
    """Match Claude Code's project folder slug (non-alphanumeric characters -> `-`)."""
    key = re.sub(r'[^A-Za-z0-9-]', '-', os.path.abspath(path))
    return key.lower() if os.name == 'nt' else key


def _existing_orca_keys(workspaces_root):
    """Normalized Claude project-folder names for every existing Orca workspace directory."""
    keys = set()
    if not os.path.isdir(workspaces_root):
        return keys
    for repo in entries(workspaces_root):
        if not repo.is_dir(follow_symlinks=False):
            continue
        for ws in entries(repo.path):
            if ws.is_dir(follow_symlinks=False):
                keys.add(_claude_proj_key(ws.path))
    return keys


def _newest_recursive(folder):
    """Newest mtime anywhere inside `folder` (Antigravity writes logs/steps at depth 2-3 inside brain/<cid>)."""
    latest = 0.0
    try:
        latest = os.lstat(folder).st_mtime
    except OSError:
        return 0.0
    for dirpath, dirnames, filenames in os.walk(folder, topdown=True):
        dirnames[:] = [d for d in dirnames if not linked(os.path.join(dirpath, d))]
        for name in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
                if st.st_mtime > latest:
                    latest = st.st_mtime
            except OSError:
                continue
    return latest


def _scan_gemini_sessions(gemini_dir, gemini_days):
    """Scan Antigravity CLI and Desktop `{conversations,brain}` folders for size and stale items (>gemini_days).

    Pairs `conversations/<cid>.db*` with `brain/<cid>/` per app data dir so a live or recently updated
    session's `brain/<cid>` is never deleted while its `.db` is open/recent or has recent nested turns.
    """
    roots = ([gemini_dir] if gemini_dir else [
        os.path.expanduser(os.path.join('~', '.gemini', 'antigravity-cli')),
        os.path.expanduser(os.path.join('~', '.gemini', 'antigravity')),
    ])
    existing = [r for r in roots if os.path.isdir(r)]
    if not existing:
        return {'exists': False, 'total_bytes': 0, 'stale': []}
    active_cid = (os.environ.get('ANTIGRAVITY_CONVERSATION_ID') or '').strip().lower()
    cutoff = time.time() - gemini_days * 86400.0 if gemini_days is not None else None
    total_bytes = 0
    by_cid = {}
    for root in existing:
        for sub in ('conversations', 'brain'):
            d = os.path.join(root, sub)
            if not os.path.isdir(d):
                continue
            for entry in sorted(entries(d), key=lambda e: e.name):
                if linked(entry.path):
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                b = size_of(entry.path) if is_dir else st.st_size
                total_bytes += b
                if cutoff is None:
                    continue
                cid = entry.name.split('.', 1)[0].lower()
                bucket = by_cid.setdefault((root, cid), {'items': [], 'mtime': 0.0, 'locked': False})
                mtime = _newest_recursive(entry.path) if is_dir else st.st_mtime
                if mtime > bucket['mtime']:
                    bucket['mtime'] = mtime
                if not is_dir and running(entry.path):
                    bucket['locked'] = True
                bucket['items'].append({'path': entry.path, 'bytes': b})
    stale = []
    if cutoff is not None:
        for (_root, cid), bucket in sorted(by_cid.items()):
            if active_cid and cid == active_cid:
                continue
            if bucket['locked'] or bucket['mtime'] >= cutoff:
                continue
            stale.extend(bucket['items'])
    return {'exists': True, 'roots': existing, 'total_bytes': total_bytes, 'stale': stale}


def _has_memory_dir(folder):
    for dirpath, dirnames, _ in os.walk(folder, topdown=True):
        dirnames[:] = [d for d in dirnames if not linked(os.path.join(dirpath, d))]
        if any(d.lower() == 'memory' for d in dirnames):
            return True
    return False


def sessions(days, apply, as_json, projects_dir=None, workspaces_root=None, gemini_days=None, gemini_dir=None):
    """List (and with --apply delete) dead Orca project dirs, old Claude transcripts, and optionally old Antigravity sessions.

    Strictly preserves `memory/` directories, any live Claude session in `~/.claude/sessions/` or Orca profiles,
    live Orca workspace subdirectories, and active/recent/locked Antigravity sessions.
    """
    claude_home = os.path.dirname(projects_dir) if projects_dir else HOME
    proj_root = projects_dir or os.path.join(claude_home, 'projects')
    ws_root = workspaces_root or os.path.expanduser(os.path.join('~', 'orca', 'workspaces'))
    gem = _scan_gemini_sessions(gemini_dir, gemini_days)
    dead_dirs, old_files = [], []

    if os.path.isdir(proj_root):
        live_sids, live_cwds = _live_sessions(claude_home)
        live_keys = {_claude_proj_key(p) for p in live_cwds}
        has_ws_root = os.path.isdir(ws_root)
        existing_orca = _existing_orca_keys(ws_root) if has_ws_root else set()
        active_prefixes = existing_orca | live_keys
        cutoff = time.time() - days * 86400.0
        for proj in sorted(entries(proj_root), key=lambda e: e.name):
            if not proj.is_dir(follow_symlinks=False) or linked(proj.path):
                continue
            proj_key = proj.name.lower() if os.name == 'nt' else proj.name
            alive_ws = any(proj_key == k or proj_key.startswith(k + '-') for k in active_prefixes)
            if has_ws_root and '-orca-workspaces-' in proj_key and not alive_ws and not _has_memory_dir(proj.path):
                dead_dirs.append({'path': proj.path, 'bytes': size_of(proj.path)})
                continue
            for dirpath, dirnames, filenames in os.walk(proj.path, topdown=True):
                # Never touch `memory/` or symlinked subdirectories.
                dirnames[:] = [d for d in dirnames if d.lower() != 'memory' and not linked(os.path.join(dirpath, d))]
                for fname in filenames:
                    fpath = os.path.join(dirpath, fname)
                    if linked(fpath):
                        continue
                    if any(sid and sid in fname for sid in live_sids) or any(sid and sid in dirpath for sid in live_sids):
                        continue
                    try:
                        st = os.lstat(fpath)
                    except OSError:
                        continue
                    if st.st_mtime < cutoff:
                        old_files.append({'path': fpath, 'bytes': st.st_size})

    gem_stale = gem['stale']
    total = sum(r['bytes'] for r in dead_dirs) + sum(r['bytes'] for r in old_files) + sum(r['bytes'] for r in gem_stale)
    freed, failed = 0, []
    if apply:
        for row in dead_dirs:
            try:
                remove(row['path'])
                freed += row['bytes']
            except OSError as error:
                failed.append({'path': row['path'], 'error': str(error)})
        touched_dirs = set()
        for row in old_files:
            try:
                remove(row['path'])
                freed += row['bytes']
                touched_dirs.add(os.path.dirname(row['path']))
            except OSError as error:
                failed.append({'path': row['path'], 'error': str(error)})
        for d in sorted(touched_dirs, key=len, reverse=True):
            if inside(d, proj_root) and os.path.basename(d).lower() != 'memory':
                with contextlib.suppress(OSError):
                    if not os.listdir(d):
                        os.rmdir(d)
        for row in gem_stale:
            try:
                remove(row['path'])
                freed += row['bytes']
            except OSError as error:
                failed.append({'path': row['path'], 'error': str(error)})

    if as_json:
        return print(json.dumps({'applied': apply, 'days': days, 'dead_dirs': dead_dirs, 'old_files_count': len(old_files),
                                 'old_files_bytes': sum(r['bytes'] for r in old_files),
                                 'gemini_total_bytes': gem['total_bytes'], 'gemini_stale_count': len(gem_stale),
                                 'gemini_stale_bytes': sum(r['bytes'] for r in gem_stale),
                                 'total_bytes': total, 'freed_bytes': freed, 'failed': failed}, indent=1))
    print(f'{len(dead_dirs)} dead Orca workspace dirs ({sum(r["bytes"] for r in dead_dirs) / 1e6:.1f} MB), '
          f'{len(old_files)} transcript files older than {days:g} days ({sum(r["bytes"] for r in old_files) / 1e6:.1f} MB) '
          f'— Claude total {(sum(r["bytes"] for r in dead_dirs) + sum(r["bytes"] for r in old_files)) / 1e9:.2f} GB')
    if gem['exists']:
        if gemini_days is not None:
            print(f'Antigravity: {len(gem_stale)} entries older than {gemini_days:g} days '
                  f'({sum(r["bytes"] for r in gem_stale) / 1e9:.2f} GB of {gem["total_bytes"] / 1e9:.2f} GB)')
        elif gem['total_bytes'] > 500_000_000:
            print(f'Antigravity cache: {gem["total_bytes"] / 1e9:.2f} GB (pass `--gemini-days 3` to include stale entries)')
    if not apply:
        for row in dead_dirs[:20]:
            print(f'  dead-dir: {row["path"]} ({row["bytes"] / 1e6:.1f} MB)')
        if len(dead_dirs) > 20:
            print(f'  ... and {len(dead_dirs) - 20} more dead dirs')
        tool = os.path.abspath(__file__).replace('\\', '/')
        gflag = f' --gemini-days {gemini_days:g}' if gemini_days is not None else ''
        return print(f'dry run, nothing deleted; to clean: python "{tool}" sessions --days {days:g}{gflag} --apply')
    for row in failed:
        print(f'FAILED {row["path"]}: {row["error"]}')
    print(f'freed {freed / 1e9:.2f} GB, {len(failed)} failed')


def daily(agy=False):
    """One line for a new session when task folders look finished, at most once a day; never an error."""
    if os.environ.get('CLAUDE_TASK_DAILY', '1').strip() == '0':
        if agy:
            print('{}')
        return
    stamp = os.environ.get('CLAUDE_TASK_STAMP') or os.path.join(HOME, 'task-daily.stamp')
    try:
        if time.time() - os.path.getmtime(stamp) < 86400:
            if agy:
                print('{}')
            return
    except OSError:
        pass
    with open(stamp, 'w'):
        pass
    cfg = settings()
    found = rows(cfg, quick=True)
    finished, closed = sum(1 for r in found if r.get('finished')), sum(1 for r in found if r['state'] == 'closed')
    if finished or closed:
        tool = os.path.abspath(__file__).replace('\\', '/')
        msg = (f'{finished} task folders in {cfg["artifacts"]} look finished and {closed} closed tasks still have a worktree: '
               f'`python "{tool}" sweep` lists them. Close or remove only your own, or what the user asks for.')
        if agy:
            print(json.dumps({'injectSteps': [{'systemMessage': {'systemMessage': msg}}]}))
        else:
            print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': msg}}))
    elif agy:
        print('{}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='action', required=True)
    for action in ('open', 'close'):
        sub.add_parser(action).add_argument('name')
    adding = sub.add_parser('add')
    adding.add_argument('name')
    adding.add_argument('kind', choices=('dir', 'process', 'remote', 'command'))
    adding.add_argument('value', nargs='+')
    adding.add_argument('--shell', choices=sorted(SHELLS))
    adding.add_argument('--why', help='what the command puts back')
    sub.add_parser('sweep').add_argument('--json', action='store_true')
    sub.add_parser('daily').add_argument('--antigravity', action='store_true', help='emit Antigravity SessionStart JSON')
    caching = sub.add_parser('caches')
    caching.add_argument('--days', type=float, default=IDLE_DAYS)
    caching.add_argument('--apply', action='store_true')
    caching.add_argument('--json', action='store_true')
    sess = sub.add_parser('sessions')
    sess.add_argument('--days', type=float, default=14)
    sess.add_argument('--apply', action='store_true')
    sess.add_argument('--json', action='store_true')
    sess.add_argument('--projects-dir', default=None)
    sess.add_argument('--workspaces-root', default=None)
    sess.add_argument('--gemini-days', type=float, default=None, help='also prune Antigravity conversations/brain older than N days')
    sess.add_argument('--gemini-dir', default=None)
    args = ap.parse_args()
    if args.action == 'daily':
        try:
            daily(agy=args.antigravity)
        except BaseException:  # a hook must never break a session's start
            if args.antigravity:
                print('{}')
        return
    if args.action == 'sessions':
        return sessions(args.days, args.apply, args.json, args.projects_dir, args.workspaces_root,
                        args.gemini_days, args.gemini_dir)
    cfg = settings()
    if args.action == 'open':
        open_task(cfg, args.name)
    elif args.action == 'add':
        add(cfg, args)
    elif args.action == 'close':
        close(cfg, args.name)
    elif args.action == 'caches':
        caches(cfg, args.days, args.apply, args.json)
    else:
        sweep(cfg, args.json)


if __name__ == '__main__':
    main()
