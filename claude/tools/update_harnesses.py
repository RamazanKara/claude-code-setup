#!/usr/bin/env python3
"""Update the agent harnesses (Claude Code, Codex, OpenCode, Antigravity), Orca and these rules, here
and on the laptop, in one call.

  python update_harnesses.py                 # this machine
  python update_harnesses.py --laptop        # this machine, then the laptop over ssh
  python update_harnesses.py --only-laptop   # the laptop only
  python update_harnesses.py --orca          # also close a running Orca to upgrade it

In order, on each machine: fast-forward the rules checkout (C:\\src\\claude-code-setup) when it is on main
and clean, and run its install.py when that brought new commits; `claude update`; Codex and OpenCode
from npm when npm has a newer version (OpenCode's own `opencode.exe` processes from npm, such as the
`serve --service` one, are stopped first, because Windows will not replace a running exe); `agy update`;
Orca through winget when winget offers a newer version; then `orca skills update`. A before/after table
of versions ends each machine's run.

Orca is only upgraded when it is not running, or with --orca. The installer closes Orca, and with it
every agent session inside Orca, so --orca is refused when this script itself runs inside Orca (run it
from a plain terminal instead). After an upgrade that closed it, Orca is started again in the signed-in
user's desktop session (through a one-off interactive scheduled task, which also works over ssh).

The laptop gets a copy of this file (scp to its temp folder) and runs it there with the same switches,
so both machines run the same version. Every step is safe to repeat. Exit code 0 when every step worked,
1 when any step failed (the table and the FAILED lines say which).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LAPTOP = 'ramaz@192.168.4.12'
RULES = Path(r'C:\src\claude-code-setup')
NPM_PACKAGES = {'codex': '@openai/codex', 'opencode': '@opencode/cli'}
ORCA_ID = 'StablyAI.Orca'
ORCA_EXE = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'orca' / 'Orca.exe'
WINGET_NO_UPGRADE = {0x8A15002B, 0x8A15002B - (1 << 32)}  # "no applicable upgrade", as unsigned and signed
FAILED: list[str] = []


def run(cmd, timeout=900, check=False, quiet=False):
    """Run a command (a list, or a string for the shell), return (code, output). Never raises."""
    shell = isinstance(cmd, str)
    if not shell:
        exe = shutil.which(cmd[0])
        if exe is None:
            return 127, f'{cmd[0]}: not found'
        cmd = [exe] + list(cmd[1:])
    try:
        p = subprocess.run(cmd, shell=shell, capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=timeout, stdin=subprocess.DEVNULL)
        out = (p.stdout or '') + (p.stderr or '')
        code = p.returncode
    except subprocess.TimeoutExpired:
        code, out = 124, f'timed out after {timeout} s'
    except OSError as e:
        code, out = 126, str(e)
    if not quiet and out.strip():
        print('    ' + '\n    '.join(out.strip().splitlines()[-8:]))
    if check and code != 0:
        FAILED.append(f'{cmd if shell else " ".join(map(str, cmd))} (exit {code})')
    return code, out


def first_line(text):
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ''


def npm_installed():
    code, out = run(['npm', 'ls', '-g', '--depth=0', '--json'], quiet=True)
    try:
        deps = json.loads(out[out.index('{'):]).get('dependencies', {})
    except (ValueError, AttributeError):
        return {}
    return {name: deps.get(pkg, {}).get('version', '') for name, pkg in NPM_PACKAGES.items()}


def orca_running():
    code, out = run(['tasklist', '/FI', 'IMAGENAME eq Orca.exe', '/NH'], quiet=True)
    return 'orca.exe' in out.lower()


def versions():
    v = {}
    for name, cmd in (('claude', ['claude', '--version']), ('codex', ['codex', '--version']),
                      ('opencode', ['opencode', '--version']), ('agy', ['agy', '--version'])):
        code, out = run(cmd, timeout=60, quiet=True)
        v[name] = first_line(out) if code == 0 else 'missing'
    code, out = run(['winget', 'list', '--id', ORCA_ID, '--exact', '--disable-interactivity'], timeout=120, quiet=True)
    line = next((l for l in out.splitlines() if ORCA_ID in l), '')
    cols = [c for c in line.split(ORCA_ID, 1)[1].split() if c != 'winget'] if line else []
    v['orca'] = (cols[0] + (f' ({cols[1]} available)' if len(cols) > 1 else '')) if cols else 'missing'
    if RULES.is_dir():
        code, out = run(['git', '-C', str(RULES), 'log', '-1', '--format=%h %cs'], quiet=True)
        v['rules'] = first_line(out)
    return v


def update_rules():
    if not (RULES / '.git').exists():
        print(f'  rules: {RULES} not found, skipped')
        return
    _, branch = run(['git', '-C', str(RULES), 'branch', '--show-current'], quiet=True)
    _, dirty = run(['git', '-C', str(RULES), 'status', '--porcelain', '--untracked-files=no'], quiet=True)
    if branch.strip() != 'main' or dirty.strip():
        print(f'  rules: {RULES} is not a clean main checkout, left as it is')
        return
    _, before = run(['git', '-C', str(RULES), 'rev-parse', 'HEAD'], quiet=True)
    if run(['git', '-C', str(RULES), 'pull', '--ff-only', '-q'], check=True)[0] != 0:
        return
    _, after = run(['git', '-C', str(RULES), 'rev-parse', 'HEAD'], quiet=True)
    if before == after:
        print('  rules: current')
        return
    print('  rules: new commits, running install.py')
    run([sys.executable, str(RULES / 'install.py')], check=True)


def update_npm():
    have = npm_installed()
    for name, pkg in NPM_PACKAGES.items():
        _, latest = run(['npm', 'view', pkg, 'version'], timeout=120, quiet=True)
        latest = first_line(latest)
        if not latest or latest == have.get(name):
            print(f'  {name}: {have.get(name) or "?"} is current' if latest else f'  {name}: npm view failed')
            if not latest:
                FAILED.append(f'npm view {pkg}')
            continue
        print(f'  {name}: {have.get(name) or "not installed"} -> {latest}')
        if name == 'opencode':
            stop_npm_opencode()
        run(['npm', 'install', '-g', f'{pkg}@latest'], check=True)


def stop_npm_opencode():
    """Windows will not overwrite a running exe; stop opencode.exe processes started from npm's folder."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='opencode.exe'\" | "
          "Where-Object { $_.ExecutablePath -like '*\\npm\\node_modules\\*' } | "
          "ForEach-Object { Write-Output ('stopping opencode ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }")
    run(['powershell', '-NoProfile', '-Command', ps])


def update_orca(allow_close):
    code, out = run(['winget', 'list', '--id', ORCA_ID, '--exact', '--upgrade-available',
                     '--disable-interactivity', '--accept-source-agreements'], timeout=180, quiet=True)
    if code != 0 or ORCA_ID not in out:
        print('  orca: current (winget offers nothing newer)')
        return
    was_running = orca_running()
    if was_running and not allow_close:
        print('  orca: an upgrade is available but Orca is running; rerun with --orca from a plain terminal')
        FAILED.append('orca upgrade skipped: Orca running (use --orca)')
        return
    if was_running and os.environ.get('ORCA_APP_VERSION'):
        print('  orca: refusing --orca from inside Orca (the installer would close this script); use a plain terminal')
        FAILED.append('orca upgrade skipped: run from inside Orca')
        return
    print('  orca: upgrading' + (' (closes the running Orca)' if was_running else ''))
    code, _ = run(['winget', 'upgrade', '--id', ORCA_ID, '--exact', '--silent', '--disable-interactivity',
                   '--accept-package-agreements', '--accept-source-agreements'], timeout=1200)
    if code != 0 and code not in WINGET_NO_UPGRADE:
        FAILED.append(f'winget upgrade {ORCA_ID} (exit {code})')
        return
    if was_running and not orca_running() and ORCA_EXE.exists():
        start_in_desktop(ORCA_EXE)


def start_in_desktop(exe: Path):
    """Start a GUI program in the signed-in user's desktop session, also from an ssh (session 0) shell."""
    task = 'UpdateHarnessesRelaunch'
    run(['schtasks', '/Create', '/TN', task, '/TR', f'"{exe}"', '/SC', 'ONCE', '/ST', '23:59', '/IT', '/F'], quiet=True)
    code, _ = run(['schtasks', '/Run', '/TN', task], quiet=True)
    run(['schtasks', '/Delete', '/TN', task, '/F'], quiet=True)
    print('  orca: started again' if code == 0 else '  orca: could not start it again; start Orca by hand')


def update_here(args):
    host = os.environ.get('COMPUTERNAME', 'this machine')
    print(f'== {host}')
    before = versions()
    update_rules()
    print('  claude:')
    run(['claude', 'update'], timeout=600, check=True)
    update_npm()
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
    code, _ = run(['scp', '-q', str(Path(__file__).resolve()), f'{LAPTOP}:{remote}'], timeout=60)
    if code != 0:
        FAILED.append('laptop: scp failed (laptop off or unreachable?)')
        return
    flags = ' --orca' if args.orca else ''
    # The laptop's ssh shell is Windows PowerShell 5.1; the file lands in the home folder.
    cmd = ['ssh', '-o', 'ConnectTimeout=10', LAPTOP, f'python $HOME\\{remote}{flags}; exit $LASTEXITCODE']
    exe = shutil.which('ssh')
    p = subprocess.run([exe] + cmd[1:], stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        FAILED.append(f'laptop: run reported failures (exit {p.returncode})')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--laptop', action='store_true', help='also update the laptop over ssh')
    ap.add_argument('--only-laptop', action='store_true', help='update the laptop only')
    ap.add_argument('--orca', action='store_true', help='close a running Orca to upgrade it')
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
