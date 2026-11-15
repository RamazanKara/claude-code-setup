#!/usr/bin/env python3
"""Update the agent harnesses (Claude Code, Codex, OpenCode, Antigravity), Orca and these rules, here
and on the laptop, in one call.

  python update_harnesses.py                 # this machine
  python update_harnesses.py --laptop        # this machine, then the laptop over ssh
  python update_harnesses.py --only-laptop   # the laptop only
  python update_harnesses.py --orca          # also close a running Orca to upgrade it
  python update_harnesses.py --force         # also stop running Codex/OpenCode sessions to update them

In order, on each machine: fast-forward the rules checkout (C:\\src\\claude-code-setup) when it is on main
and clean, and run its install.py when that brought new commits; `claude update`; Codex and OpenCode
from npm when npm has a newer version; `agy update`; Orca through winget when winget offers a newer
version; then `orca skills update`. A before/after table of versions ends each machine's run.

Windows will not replace a running exe. OpenCode's background `serve --service` process is stopped
before its update when nothing else holds the exe; this script does not start it again. Any other running Codex or OpenCode session
(an Orca agent, a DeepSeek worker) makes the script skip that update with a FAILED line, unless --force
is given, which stops those sessions. Claude Code and agy update beside running sessions.

Orca is only upgraded when it is not running, or with --orca. The installer closes Orca, and with it
every agent session inside Orca, so --orca is refused when this script itself runs inside Orca (run it
from a plain terminal instead). After an upgrade that closed it, Orca is started again in the signed-in
user's desktop session (through a one-off interactive scheduled task at normal priority, which also
works over ssh).

The laptop gets a copy of this file (scp to its home folder, deleted afterwards) and runs it there with
the same switches, so both machines run the same version. Every step is safe to repeat. Exit code 0 when
every step worked, 1 when any step failed or was skipped (the FAILED lines say which).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

LAPTOP = 'ramaz@192.168.4.12'
RULES = Path(r'C:\src\claude-code-setup')
NPM_PACKAGES = {'codex': '@openai/codex', 'opencode': '@opencode/cli'}
ORCA_ID = 'StablyAI.Orca'
ORCA_EXE = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'orca' / 'Orca.exe'
WINGET_NO_PACKAGE = {0x8A150014, 0x8A150014 - (1 << 32)}  # "no installed package matches", unsigned and signed
WINGET_NO_UPGRADE = {0x8A15002B, 0x8A15002B - (1 << 32)}  # "no applicable upgrade"
FAILED: list[str] = []


class Result:
    def __init__(self, code, out, err):
        self.code, self.out, self.err = code, out, err

    @property
    def text(self):
        return self.out + self.err


def run(cmd, timeout=900, check=False, quiet=False) -> Result:
    """Run a command list; never raises. On a timeout the whole process tree is killed."""
    exe = shutil.which(cmd[0])
    if exe is None:
        r = Result(127, '', f'{cmd[0]}: not found')
    else:
        try:
            p = subprocess.Popen([exe] + list(cmd[1:]), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 stdin=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace',
                                 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            try:
                out, err = p.communicate(timeout=timeout)
                r = Result(p.returncode, out or '', err or '')
            except KeyboardInterrupt:
                subprocess.run(['taskkill', '/T', '/F', '/PID', str(p.pid)], capture_output=True)
                raise
            except subprocess.TimeoutExpired:
                subprocess.run(['taskkill', '/T', '/F', '/PID', str(p.pid)], capture_output=True)
                try:
                    p.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                r = Result(124, '', f'timed out after {timeout} s')
        except OSError as e:
            r = Result(126, '', str(e))
    if not quiet and r.text.strip():
        print('    ' + '\n    '.join(r.text.strip().splitlines()[-8:]))
    if check and r.code != 0:
        FAILED.append(f'{" ".join(map(str, cmd))} (exit {r.code})')
    return r


def first_line(text):
    return next((line.strip() for line in text.splitlines() if line.strip()), '')


def processes(name):
    """[(pid, exe path, command line)] of running processes with that image name."""
    ps = (f"Get-CimInstance Win32_Process -Filter \"Name='{name}'\" | "
          "ForEach-Object { [string]$_.ProcessId + \"`t\" + $_.ExecutablePath + \"`t\" + $_.CommandLine }")
    r = run(['powershell', '-NoProfile', '-Command', ps], timeout=60, quiet=True)
    if r.code != 0:
        return None  # unknown: callers treat it as running
    return [tuple(line.split('\t', 2)) for line in r.out.splitlines() if line.count('\t') >= 2]


def ancestors():
    """PIDs of this script's parent chain, so --force never stops the session that started it."""
    ps = ("$p = Get-CimInstance Win32_Process -Filter \"ProcessId=$PID\"; "
          "while ($p) { Write-Output $p.ProcessId; "
          "$p = Get-CimInstance Win32_Process -Filter (\"ProcessId=\" + $p.ParentProcessId) }")
    return set(run(['powershell', '-NoProfile', '-Command', ps], timeout=60, quiet=True).out.split())


def stop(pid):
    run(['taskkill', '/F', '/PID', pid], quiet=True)


def orca_running():
    procs = processes('Orca.exe')
    return procs is None or bool(procs)


def versions():
    v = {}
    for name in ('claude', 'codex', 'opencode', 'agy'):
        r = run([name, '--version'], timeout=60, quiet=True)
        v[name] = first_line(r.text) if r.code == 0 else 'missing'
    r = run(['winget', 'list', '--id', ORCA_ID, '--exact', '--disable-interactivity'], timeout=120, quiet=True)
    line = next((l for l in r.out.splitlines() if ORCA_ID in l), '')
    cols = [c for c in line.split(ORCA_ID, 1)[1].split() if c != 'winget'] if line else []
    v['orca'] = (cols[0] + (f' ({cols[1]} available)' if len(cols) > 1 else '')) if cols else 'missing'
    if RULES.is_dir():
        v['rules'] = first_line(run(['git', '-C', str(RULES), 'log', '-1', '--format=%h %cs'], quiet=True).out)
    return v


def update_rules():
    if not (RULES / '.git').exists():
        print(f'  rules: {RULES} not found, skipped')
        return
    branch = run(['git', '-C', str(RULES), 'branch', '--show-current'], quiet=True).out.strip()
    dirty = run(['git', '-C', str(RULES), 'status', '--porcelain', '--untracked-files=no'], quiet=True).out.strip()
    if branch != 'main' or dirty:
        print(f'  rules: {RULES} is not a clean main checkout, left as it is')
        return
    before = run(['git', '-C', str(RULES), 'rev-parse', 'HEAD'], quiet=True).out
    if run(['git', '-C', str(RULES), 'pull', '--ff-only', '-q'], check=True).code != 0:
        return
    if run(['git', '-C', str(RULES), 'rev-parse', 'HEAD'], quiet=True).out == before:
        print('  rules: current')
        return
    print('  rules: new commits, running install.py')
    run([sys.executable, str(RULES / 'install.py')], check=True)


def npm_installed():
    r = run(['npm', 'ls', '-g', '--depth=0', '--json'], timeout=120, quiet=True)
    try:
        deps = json.loads(r.out).get('dependencies', {})
    except ValueError:
        return None
    return {name: deps.get(pkg, {}).get('version', '') for name, pkg in NPM_PACKAGES.items()}


def update_npm(force):
    have = npm_installed()
    if have is None:
        FAILED.append('npm ls -g: output not readable, Codex and OpenCode not updated')
        return
    for name, pkg in NPM_PACKAGES.items():
        r = run(['npm', 'view', pkg, 'version'], timeout=120, quiet=True)
        latest = first_line(r.out)
        if r.code != 0 or not latest[:1].isdigit():
            FAILED.append(f'npm view {pkg} (exit {r.code})')
            continue
        if latest == have[name]:
            print(f'  {name}: {latest} is current')
            continue
        print(f'  {name}: {have[name] or "not installed"} -> {latest}')
        if not free_exe(name, force):
            continue
        run(['npm', 'install', '-g', f'{pkg}@latest'], timeout=600, check=True)


def free_exe(name, force):
    """Stop what holds npm's <name>.exe: the OpenCode service always, sessions only with --force."""
    found = processes(f'{name}.exe')
    if found is None:
        FAILED.append(f'{name} update skipped: could not list running processes')
        return False
    procs = [p for p in found if '\\npm\\node_modules\\' in p[1].lower()]
    service = [p for p in procs if 'serve --service' in p[2]]
    sessions = [p for p in procs if p not in service]
    if sessions and force and {p[0] for p in sessions} & ancestors():
        FAILED.append(f'{name} update skipped: --force from inside a {name} session would stop this script')
        return False
    if sessions and not force:
        print(f'  {name}: skipped, {len(sessions)} session(s) running (rerun with --force to stop them)')
        FAILED.append(f'{name} update skipped: {len(sessions)} running session(s) (use --force)')
        return False
    for pid, _, _ in service + sessions:
        print(f'  {name}: stopping process {pid}')
        stop(pid)
    if procs:
        time.sleep(2)
    return True


def update_orca(allow_close):
    r = run(['winget', 'list', '--id', ORCA_ID, '--exact', '--upgrade-available',
             '--disable-interactivity', '--accept-source-agreements'], timeout=180, quiet=True)
    if r.code in WINGET_NO_PACKAGE or (r.code == 0 and ORCA_ID not in r.out):
        print('  orca: current (winget offers nothing newer)')
        return
    if r.code != 0:
        FAILED.append(f'winget list {ORCA_ID} (exit {r.code}): {first_line(r.text[-300:])}')
        return
    was_running = orca_running()
    if os.environ.get('ORCA_APP_VERSION') and (was_running or allow_close):
        print('  orca: refusing to upgrade from inside Orca (the installer would close this script); use a plain terminal')
        FAILED.append('orca upgrade skipped: run from inside Orca')
        return
    if was_running and not allow_close:
        print('  orca: an upgrade is available but Orca is running; rerun with --orca from a plain terminal')
        FAILED.append('orca upgrade skipped: Orca running (use --orca)')
        return
    print('  orca: upgrading' + (' (closes the running Orca)' if was_running else ''))
    r = run(['winget', 'upgrade', '--id', ORCA_ID, '--exact', '--silent', '--disable-interactivity',
             '--accept-package-agreements', '--accept-source-agreements'], timeout=1200)
    if r.code != 0 and r.code not in WINGET_NO_UPGRADE:
        FAILED.append(f'winget upgrade {ORCA_ID} (exit {r.code})')
    if was_running and not orca_running():
        start_in_desktop(ORCA_EXE)


def start_in_desktop(exe: Path):
    """Start a GUI program in the signed-in user's desktop session at normal priority, also from ssh."""
    task = 'UpdateHarnessesRelaunch'
    ps = (f"$a = New-ScheduledTaskAction -Execute '{exe}'; "
          "$p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive; "
          "$s = New-ScheduledTaskSettingsSet -Priority 4 -ExecutionTimeLimit 0 -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; "
          f"Register-ScheduledTask -TaskName '{task}' -Action $a -Principal $p -Settings $s -Force | Out-Null; "
          f"Start-ScheduledTask -TaskName '{task}'")
    ok = exe.exists() and run(['powershell', '-NoProfile', '-Command', ps], timeout=60, quiet=True).code == 0
    for _ in range(15):
        if not ok or orca_running():
            break
        time.sleep(1)
    ok = ok and orca_running()
    run(['powershell', '-NoProfile', '-Command',
         f"Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false -ErrorAction SilentlyContinue"], quiet=True)
    if ok:
        print('  orca: started again')
    else:
        FAILED.append('orca: could not start it again after the upgrade; start Orca by hand')


def update_here(args):
    print(f'== {os.environ.get("COMPUTERNAME", "this machine")}')
    before = versions()
    update_rules()
    print('  claude:')
    run(['claude', 'update'], timeout=600, check=True)
    update_npm(args.force)
    print('  agy:')
    run(['agy', 'update'], timeout=600, check=True)
    update_orca(args.orca)
    if shutil.which('orca'):
        run(['orca', 'skills', 'update'], timeout=300, check=True, quiet=True)
    after = versions()
    print(f'\n  {"":10} {"before":28} after')
    for k in after:
        mark = '' if before.get(k) == after[k] else '  *'
        print(f'  {k:10} {before.get(k, ""):28} {after[k]}{mark}')
    print()


def update_laptop(args):
    print(f'== laptop ({LAPTOP})')
    remote = 'update_harnesses_remote.py'
    if run(['scp', '-q', '-o', 'ConnectTimeout=10', str(Path(__file__).resolve()), f'{LAPTOP}:{remote}'],
           timeout=60).code != 0:
        FAILED.append('laptop: scp failed (laptop off or unreachable?)')
        return
    flags = ''.join(f' --{f}' for f in ('orca', 'force') if getattr(args, f))
    # The laptop's ssh shell is Windows PowerShell 5.1; the copy lands in the home folder.
    remote_cmd = (f'python $HOME\\{remote}{flags}; $c = $LASTEXITCODE; '
                  f'Remove-Item $HOME\\{remote} -ErrorAction SilentlyContinue; exit $c')
    try:
        code = subprocess.run([shutil.which('ssh'), '-o', 'ConnectTimeout=10', LAPTOP, remote_cmd],
                              stdin=subprocess.DEVNULL, timeout=3600).returncode
    except subprocess.TimeoutExpired:
        code = 124
    if code != 0:
        FAILED.append(f'laptop: see its FAILED lines above (exit {code})')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--laptop', action='store_true', help='also update the laptop over ssh')
    ap.add_argument('--only-laptop', action='store_true', help='update the laptop only')
    ap.add_argument('--orca', action='store_true', help='close a running Orca to upgrade it')
    ap.add_argument('--force', action='store_true', help='stop running Codex/OpenCode sessions to update them')
    args = ap.parse_args()
    # agy prints symbols a cp1252 console (the laptop over ssh) cannot show; keep the order with ssh's output.
    sys.stdout.reconfigure(errors='replace', line_buffering=True)
    if sys.platform != 'win32':
        sys.exit('This script is for the Windows machines.')
    if not args.only_laptop:
        update_here(args)
    if args.laptop or args.only_laptop:
        update_laptop(args)
    if FAILED:
        print('FAILED:\n  ' + '\n  '.join(FAILED))
        sys.exit(1)
    print('All updates done.')


if __name__ == '__main__':
    main()
