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

~/.claude/machine.json says where the folders go on this machine:

  {"artifacts": "D:/CodexArtifacts",       one folder per task below this
   "roots": ["C:/tests"],                  further places a `dir` entry may lie in, besides the temp folder
   "keep": ["next-release", "C:/tests/x"], never deleted, with what they hold and what holds them
   "hosts": {"nuc": {"ssh": "nuc", "shell": "sh", "roots": ["/mnt/hdd2"]}},
   "ssh": "C:/Windows/System32/OpenSSH/ssh.exe"}   only when the ssh on PATH is the wrong one

Only `close` deletes, and only what the task itself can have made. A folder elsewhere must lie in
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
            if abandoned(lock):
                with contextlib.suppress(OSError):
                    os.remove(lock)
            elif time.time() > deadline:
                fail(f'another task.py is working on {name} ({lock}); try again when it is done')
            else:
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


def daily():
    """One line for a new session when task folders look finished, at most once a day; never an error."""
    stamp = os.environ.get('CLAUDE_TASK_STAMP') or os.path.join(HOME, 'task-daily.stamp')
    try:
        if time.time() - os.path.getmtime(stamp) < 86400:
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
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext':
              f'{finished} task folders in {cfg["artifacts"]} look finished and {closed} closed tasks still have a worktree: '
              f'`python "{tool}" sweep` lists them. Close or remove only your own, or what the user asks for.'}}))


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
    sub.add_parser('daily')
    args = ap.parse_args()
    if args.action == 'daily':
        try:
            daily()
        except BaseException:  # a hook must never break a session's start
            pass
        return
    cfg = settings()
    if args.action == 'open':
        open_task(cfg, args.name)
    elif args.action == 'add':
        add(cfg, args)
    elif args.action == 'close':
        close(cfg, args.name)
    else:
        sweep(cfg, args.json)


if __name__ == '__main__':
    main()
