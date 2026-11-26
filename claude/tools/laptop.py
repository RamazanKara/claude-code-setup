#!/usr/bin/env python3
"""Offload CPU builds, tests, or GPU/e2e runs from the PC to the laptop (usable from Claude Code & Antigravity).

Over SSH the laptop's Windows shell runs in Session 0 (elevated), where GPU, WGC, encoder, and host
tests that launch programs fail. `run` executes inside an Interactive Scheduled Task in the signed-in
console user's desktop session (`ramaz`), checks locks (`BUILD-LOCK`, `LAPTOP-LOCK`, live `PRIORITY`,
held build lanes, running offload/check tasks), optionally takes `LAPTOP-LOCK` for `--gpu` runs, and
automatically removes its detached worktree in `finally` so zero garbage piles up on the laptop.

  python laptop.py status [--json]
  python laptop.py run --branch wip/my-fix [--gpu] [--push] [--wait] [--timeout 120] -- <command>
  python laptop.py check wip/my-fix [--wait] [--timeout 180] [--out <dir>]
  python laptop.py clean [--apply] [--prune-pool-under-gb 45]

Exit codes:
  0  succeeded
  1  usage / ssh / remote failure
  2  branch missing on origin (or refused push to main/master)
  3  remote command exited 3 (Rubylight AMD encoder hang sentinel — investigate, NEVER retry)
  4  laptop is busy (rerun with --wait to poll)
  5  nobody (or another user) is signed in at the laptop console
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
DEFAULT_HOST = 'ramaz@192.168.4.12'
DEFAULT_REPO = r'C:\src\Rubylight'
DEFAULT_WORK = r'C:\src\rl-offload'
DEFAULT_LOCKS = r'C:\src\butterpollo-release'
DEFAULT_POOL = r'C:\src\rl-target-pool'
DEFAULT_USER = 'ramaz'
SAFE_NAME = re.compile(r'[^A-Za-z0-9._-]+')


def load_machine():
    path = Path(os.environ.get('CLAUDE_MACHINE_FILE') or (HOME / 'machine.json'))
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def ssh_bins(cfg):
    """Prefer Windows OpenSSH (which holds the keys) over Git Bash's ssh."""
    configured = cfg.get('ssh')
    if isinstance(configured, list) and configured:
        ssh_cmd = list(configured)
        scp_cmd = [str(Path(configured[0]).with_name('scp.exe'))] if configured[0].lower().endswith('ssh.exe') else ['scp']
        return ssh_cmd, scp_cmd
    if isinstance(configured, str) and configured:
        p = Path(configured)
        return [configured], [str(p.with_name('scp.exe')) if p.name.lower() == 'ssh.exe' else 'scp']
    win_ssh = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'OpenSSH' / 'ssh.exe'
    win_scp = win_ssh.with_name('scp.exe')
    if win_ssh.is_file():
        return [str(win_ssh)], [str(win_scp) if win_scp.is_file() else 'scp']
    return [shutil.which('ssh') or 'ssh'], [shutil.which('scp') or 'scp']


def laptop_target(cfg, override=None):
    if override:
        return override
    return str(((cfg.get('hosts') or {}).get('laptop') or {}).get('ssh') or DEFAULT_HOST)


def ps_encoded(script: str) -> str:
    payload = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    return f'powershell -NoProfile -NonInteractive -EncodedCommand {payload}'


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def ssh_run(ssh_cmd, target, ps_script, timeout=45):
    argv = [*ssh_cmd, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', target, ps_encoded(ps_script)]
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=timeout)
        return done.returncode, done.stdout.strip(), done.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, '', f'ssh timed out after {timeout}s'
    except OSError as error:
        return 127, '', str(error)


def scp_put(scp_cmd, target, local_path, remote_path, timeout=45):
    argv = [*scp_cmd, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-q',
            str(local_path), f'{target}:{remote_path.replace(chr(92), "/")}']
    done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout)
    return done.returncode, (done.stderr or done.stdout or '').strip()


def probe_laptop_ps(locks=DEFAULT_LOCKS, pool=DEFAULT_POOL, user=DEFAULT_USER):
    return f"""
$ErrorActionPreference = 'SilentlyContinue'
$locks = {ps_quote(locks)}
$pool = {ps_quote(pool)}
function Test-Held([string]$p) {{
  try {{ [IO.File]::Open($p, 'Open', 'ReadWrite', 'None').Dispose(); return $false }} catch {{ return $true }}
}}
$busy = @()
foreach ($n in 'BUILD-LOCK', 'LAPTOP-LOCK', 'PRIORITY') {{
  $f = Join-Path $locks $n
  if (-not (Test-Path -LiteralPath $f)) {{ continue }}
  $t = (Get-Content -LiteralPath $f -Raw)
  if ($n -eq 'PRIORITY' -and $t -match 'pid=(\\d+)' -and -not (Get-Process -Id ([int]$Matches[1]) -ErrorAction SilentlyContinue)) {{ continue }}
  $busy += "$n`: $($t.Trim())"
}}
foreach ($d in (Join-Path $locks 'lanes'), $pool) {{
  if (Test-Path -LiteralPath $d) {{
    foreach ($f in Get-ChildItem -LiteralPath $d -Filter '*.lock') {{
      if (Test-Held $f.FullName) {{ $busy += "lane $($f.Name)" }}
    }}
  }}
}}
foreach ($t in Get-ScheduledTask -TaskName 'rl-gate-*','rl-remote-check-*','rl-offload-*' -ErrorAction SilentlyContinue) {{
  if ($t.State -eq 'Running') {{ $busy += "task $($t.TaskName)" }}
}}
$console = (Get-CimInstance Win32_ComputerSystem).UserName
$consoleUser = if ($console) {{ ($console -split '\\\\')[-1] }} else {{ '' }}
$drive = Get-PSDrive C
$freeGb = [math]::Round($drive.Free / 1GB, 1)
$out = [ordered]@{{
  host = $env:COMPUTERNAME
  console_user = $consoleUser
  console_ready = ($consoleUser -ieq {ps_quote(user)})
  free_gb = $freeGb
  busy = $busy
  ready_cpu = ($busy.Count -eq 0 -and $consoleUser -ieq {ps_quote(user)})
  ready_gpu = ($busy.Count -eq 0 -and $consoleUser -ieq {ps_quote(user)})
}}
$out | ConvertTo-Json -Compress -Depth 4
"""


def probe_local():
    info = {'host': os.environ.get('COMPUTERNAME', 'local'), 'drives': {}}
    for letter in ('C', 'D'):
        root = f'{letter}:\\'
        if os.path.isdir(root):
            usage = shutil.disk_usage(root)
            info['drives'][letter] = round(usage.free / (1024 ** 3), 1)
    locks_dir = Path(DEFAULT_LOCKS)
    busy = []
    for name in ('BUILD-LOCK', 'PRIORITY'):
        p = locks_dir / name
        if p.is_file():
            busy.append(f'{name}: {p.read_text(encoding="utf-8", errors="replace").strip()[:120]}')
    info['busy'] = busy
    return info


def cmd_status(args, cfg):
    ssh_cmd, _ = ssh_bins(cfg)
    target = laptop_target(cfg, args.ssh)
    local_info = probe_local()
    code, out, err = ssh_run(ssh_cmd, target, probe_laptop_ps(args.locks, args.pool, args.user), timeout=30)
    if code != 0:
        laptop_info = {'reachable': False, 'ssh': target, 'error': err or out or f'exit {code}'}
    else:
        try:
            laptop_info = {'reachable': True, 'ssh': target, **json.loads(out.splitlines()[-1])}
        except (ValueError, IndexError):
            laptop_info = {'reachable': False, 'ssh': target, 'error': f'unparseable output: {out[:200]}'}
    result = {'pc': local_info, 'laptop': laptop_info}
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if laptop_info.get('reachable') else 1
    print(f"PC ({local_info['host']}): free={local_info['drives']}, busy={local_info['busy'] or 'none'}")
    if not laptop_info.get('reachable'):
        print(f"Laptop ({target}): UNREACHABLE ({laptop_info.get('error')})")
        return 1
    busy = laptop_info.get('busy') or []
    print(f"Laptop ({laptop_info.get('host')} / {target}): C: {laptop_info.get('free_gb')} GB free, "
          f"console={laptop_info.get('console_user') or 'none'}, "
          f"ready_cpu={laptop_info.get('ready_cpu')}, ready_gpu={laptop_info.get('ready_gpu')}")
    if busy:
        print('  busy: ' + '; '.join(busy))
    return 0


def build_remote_runner_ps(branch: str, name: str, repo: str, work: str, locks: str,
                           command: str, gpu: bool, keep_worktree: bool) -> str:
    """Build the ASCII PowerShell runner executed in the laptop's Interactive desktop session."""
    cmd_b64 = base64.b64encode(command.encode('utf-8')).decode('ascii')
    gpu_flag = '$true' if gpu else '$false'
    keep_flag = '$true' if keep_worktree else '$false'
    return f"""# Auto-generated by laptop.py (runs in console user's Interactive session). ASCII only.
$ErrorActionPreference = 'Continue'
$Branch = {ps_quote(branch)}
$Name = {ps_quote(name)}
$Repo = {ps_quote(repo)}
$Work = {ps_quote(work)}
$Locks = {ps_quote(locks)}
$UseGpu = {gpu_flag}
$KeepWorktree = {keep_flag}
$Tree = Join-Path $Work $Name
$Log = Join-Path $Work "$Name.log"
$LockFile = Join-Path $Locks 'LAPTOP-LOCK'
$TookLock = $false
$Code = 1
$CmdText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{cmd_b64}'))
try {{
    "offload $Name ($Branch) on $env:COMPUTERNAME as $env:USERNAME in session $((Get-Process -Id $PID).SessionId) gpu=$UseGpu" | Out-File $Log -Encoding utf8
    $env:PATH = "$env:USERPROFILE\\.cargo\\bin;$env:PATH;C:\\msys64\\ucrt64\\bin"
    if ($UseGpu) {{
        New-Item -ItemType Directory -Force -Path $Locks | Out-Null
        "laptop.py offload $Name ($(Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'))" | Out-File $LockFile -Encoding utf8
        $TookLock = $true
    }}
    git -C $Repo fetch -q origin 2>&1 | Out-File -Append $Log -Encoding utf8
    if (Test-Path -LiteralPath $Tree) {{ git -C $Repo worktree remove --force $Tree 2>&1 | Out-File -Append $Log -Encoding utf8 }}
    git -C $Repo worktree prune 2>&1 | Out-File -Append $Log -Encoding utf8
    git -C $Repo worktree add -q --detach $Tree "origin/$Branch" 2>&1 | Out-File -Append $Log -Encoding utf8
    if ($LASTEXITCODE) {{ throw "could not check out origin/$Branch" }}
    "checked out $(git -C $Tree rev-parse HEAD)" | Out-File -Append $Log -Encoding utf8
    if (Test-Path -LiteralPath (Join-Path $Tree '.gitmodules')) {{
        git -C $Tree submodule update --init --recursive -q 2>&1 | Out-File -Append $Log -Encoding utf8
    }}
    Push-Location $Tree
    try {{
        $pwsh = Get-Command pwsh.exe -ErrorAction SilentlyContinue
        if ($pwsh) {{
            & $pwsh.Source -NoProfile -ExecutionPolicy Bypass -Command $CmdText *>> $Log
        }} else {{
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -Command $CmdText *>> $Log
        }}
        $Code = $LASTEXITCODE
    }} finally {{
        Pop-Location
    }}
}} catch {{
    "offload failed: $_" | Out-File -Append $Log -Encoding utf8
}} finally {{
    if (-not $KeepWorktree -and (Test-Path -LiteralPath $Tree)) {{
        git -C $Repo worktree remove --force $Tree 2>&1 | Out-File -Append $Log -Encoding utf8
        git -C $Repo worktree prune 2>&1 | Out-File -Append $Log -Encoding utf8
    }}
    if ($TookLock) {{
        Remove-Item -LiteralPath $LockFile -ErrorAction SilentlyContinue
    }}
    "EXIT=$Code" | Out-File -Append $Log -Encoding utf8
}}
"""


def build_remote_starter_ps(name: str, work: str, locks: str, pool: str, user: str, hours: int, script_path: str) -> str:
    return f"""
$ErrorActionPreference = 'Stop'
$locks = {ps_quote(locks)}
$pool = {ps_quote(pool)}
function Test-Held([string]$p) {{
  try {{ [IO.File]::Open($p, 'Open', 'ReadWrite', 'None').Dispose(); return $false }} catch {{ return $true }}
}}
$busy = @()
foreach ($n in 'BUILD-LOCK', 'LAPTOP-LOCK', 'PRIORITY') {{
  $f = Join-Path $locks $n
  if (-not (Test-Path -LiteralPath $f)) {{ continue }}
  $t = Get-Content -LiteralPath $f -Raw -ErrorAction SilentlyContinue
  if ($n -eq 'PRIORITY' -and $t -match 'pid=(\\d+)' -and -not (Get-Process -Id ([int]$Matches[1]) -ErrorAction SilentlyContinue)) {{ continue }}
  $busy += "$n`: $($t.Trim())"
}}
foreach ($d in (Join-Path $locks 'lanes'), $pool) {{
  if (Test-Path -LiteralPath $d) {{
    foreach ($f in Get-ChildItem -LiteralPath $d -Filter '*.lock') {{
      if (Test-Held $f.FullName) {{ $busy += "lane $($f.Name)" }}
    }}
  }}
}}
foreach ($t in Get-ScheduledTask -TaskName 'rl-gate-*','rl-remote-check-*','rl-offload-*' -ErrorAction SilentlyContinue) {{
  if ($t.State -eq 'Running') {{ $busy += "task $($t.TaskName) is running" }}
}}
if ($busy.Count) {{ 'BUSY'; $busy; exit 4 }}
$console = (Get-CimInstance Win32_ComputerSystem).UserName
if (-not $console -or ($console -split '\\\\')[-1] -ine {ps_quote(user)}) {{
  "NO SESSION: console user is '$console', not {user}"
  exit 5
}}
New-Item -ItemType Directory -Force -Path {ps_quote(work)} | Out-Null
$log = Join-Path {ps_quote(work)} {ps_quote(name + '.log')}
Remove-Item -LiteralPath $log -ErrorAction SilentlyContinue
$taskName = {ps_quote('rl-offload-' + name)}
$exe = if (Get-Command pwsh.exe -ErrorAction SilentlyContinue) {{ 'pwsh.exe' }} else {{ 'powershell.exe' }}
$arg = "-NoProfile -ExecutionPolicy Bypass -File `"{script_path}`""
$action = New-ScheduledTaskAction -Execute $exe -Argument $arg
$principal = New-ScheduledTaskPrincipal -UserId {ps_quote(user)} -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours {hours}) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
"STARTED $taskName"
exit 0
"""


def cmd_run(args, cfg):
    command_parts = list(args.command)
    if command_parts and command_parts[0] == '--':
        command_parts = command_parts[1:]
    command = ' '.join(command_parts).strip()
    if not command:
        print('laptop.py: missing remote command after `--`', file=sys.stderr)
        return 1

    branch = re.sub(r'^(refs/heads/|origin/)', '', args.branch.strip())
    if not branch:
        print('laptop.py: empty branch name', file=sys.stderr)
        return 1
    slug = SAFE_NAME.sub('-', args.name or branch).strip('-')[:48] or 'job'

    if args.push:
        if branch in ('main', 'master'):
            print('laptop.py: refusing --push to main/master; push a wip/<topic> branch instead', file=sys.stderr)
            return 2
        push = subprocess.run(['git', 'push', '--force-with-lease', 'origin', f'HEAD:refs/heads/{branch}'],
                              capture_output=True, text=True, encoding='utf-8', errors='replace')
        if push.returncode != 0:
            print(f'laptop.py: git push failed: {(push.stderr or push.stdout).strip()}', file=sys.stderr)
            return 2
    else:
        ls = subprocess.run(['git', 'ls-remote', 'origin', f'refs/heads/{branch}'],
                            capture_output=True, text=True, encoding='utf-8', errors='replace')
        if ls.returncode == 0 and not ls.stdout.strip():
            print(f'laptop.py: origin has no branch {branch}; push it first or pass --push', file=sys.stderr)
            return 2

    ssh_cmd, scp_cmd = ssh_bins(cfg)
    target = laptop_target(cfg, args.ssh)
    scripts_dir = rf'{args.work}\scripts'
    remote_script = rf'{scripts_dir}\run-{slug}.ps1'
    remote_log = rf'{args.work}\{slug}.log'
    task_name = f'rl-offload-{slug}'

    code, out, err = ssh_run(ssh_cmd, target, f"New-Item -ItemType Directory -Force -Path {ps_quote(scripts_dir)} | Out-Null")
    if code != 0:
        print(f'laptop.py: ssh to {target} failed: {err or out}', file=sys.stderr)
        return 1

    runner_ps = build_remote_runner_ps(branch, slug, args.repo, args.work, args.locks, command, args.gpu, args.keep_worktree)
    with tempfile.NamedTemporaryFile('w', suffix='.ps1', encoding='utf-8', newline='\n', delete=False) as tf:
        tf.write(runner_ps)
        local_runner = tf.name
    try:
        sc_code, sc_err = scp_put(scp_cmd, target, local_runner, remote_script)
        if sc_code != 0:
            print(f'laptop.py: scp runner failed: {sc_err}', file=sys.stderr)
            return 1
    finally:
        try:
            os.unlink(local_runner)
        except OSError:
            pass

    hours = max(1, (args.timeout + 59) // 60)
    starter_ps = build_remote_starter_ps(slug, args.work, args.locks, args.pool, args.user, hours, remote_script)
    deadline = time.monotonic() + args.timeout * 60
    while True:
        code, out, err = ssh_run(ssh_cmd, target, starter_ps, timeout=45)
        if code == 0:
            print(f'laptop.py: {out} (branch={branch}, gpu={args.gpu})', flush=True)
            break
        if code != 4 or not args.wait or time.monotonic() > deadline:
            print(out or err or f'start failed (exit {code})', file=sys.stderr)
            return code if code in (4, 5) else 1
        print(f'laptop.py: laptop busy, retrying in 30s: {" ".join((out or err).split())}', flush=True)
        time.sleep(30)

    shown = 0
    log_text = ''
    try:
        while True:
            time.sleep(15)
            _, text, _ = ssh_run(ssh_cmd, target, f"Get-Content -LiteralPath {ps_quote(remote_log)} -ErrorAction SilentlyContinue", timeout=30)
            lines = text.splitlines() if text else []
            for line in lines[shown:]:
                if line.startswith(('offload ', 'checked out ', '-- ', 'PASS', 'FAIL', 'ERROR', 'EXIT=')):
                    print(f'  [laptop] {line}', flush=True)
            shown = max(shown, len(lines))
            if re.search(r'(?m)^EXIT=-?\d+', text):
                log_text = text
                break
            if time.monotonic() > deadline:
                print(f'laptop.py: timed out after {args.timeout} min; task {task_name} is still running on laptop', file=sys.stderr)
                return 1
    finally:
        ssh_run(ssh_cmd, target,
                f"Unregister-ScheduledTask -TaskName {ps_quote(task_name)} -Confirm:$false -ErrorAction SilentlyContinue; "
                f"Remove-Item -LiteralPath {ps_quote(remote_script)} -ErrorAction SilentlyContinue",
                timeout=30)

    m = re.search(r'(?m)^EXIT=(-?\d+)', log_text)
    exit_code = int(m.group(1)) if m else 1
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f'{slug}.log').write_text(log_text + '\n', encoding='utf-8', newline='\n')
    tail = '\n'.join(log_text.splitlines()[-40:])
    if tail:
        print(tail)
    if exit_code == 3:
        print('laptop.py: EXIT 3 — AMD encoder hang sentinel detected on laptop; investigate logs and do NOT retry.', file=sys.stderr)
    return exit_code


def cmd_check(args, cfg):
    script = Path('tools') / 'remote-check.ps1'
    if not script.is_file():
        print(f'laptop.py: {script} not found in current checkout; run from a Rubylight checkout or use `laptop.py run`', file=sys.stderr)
        return 1
    pwsh = shutil.which('pwsh') or shutil.which('powershell') or 'pwsh'
    argv = [pwsh, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script),
            '-Host', 'laptop', '-Branch', args.branch, '-TimeoutMinutes', str(args.timeout)]
    if args.wait:
        argv.append('-Wait')
    if args.out:
        argv += ['-Out', args.out]
    if args.ssh:
        argv += ['-Ssh', args.ssh]
    return subprocess.run(argv).returncode


def cmd_clean(args, cfg):
    ssh_cmd, _ = ssh_bins(cfg)
    target = laptop_target(cfg, args.ssh)
    apply_flag = '$true' if args.apply else '$false'
    ps = f"""
$ErrorActionPreference = 'Continue'
$Apply = {apply_flag}
$Repo = {ps_quote(args.repo)}
$Roots = @({ps_quote(args.work)}, 'C:\\src\\rl-check')
$PruneUnder = {float(args.prune_pool_under_gb)}
$running = @(Get-ScheduledTask -TaskName 'rl-offload-*','rl-remote-check-*' -ErrorAction SilentlyContinue | Where-Object {{ $_.State -eq 'Running' }} | ForEach-Object {{ $_.TaskName }})
$removed = @()
$kept = @()
foreach ($root in $Roots) {{
  if (-not (Test-Path -LiteralPath $root)) {{ continue }}
  foreach ($d in Get-ChildItem -LiteralPath $root -Directory) {{
    if ($d.Name -in 'scripts','remote','logs') {{ continue }}
    if ($running | Where-Object {{ $_ -like "*$($d.Name)" }}) {{
      $kept += "$($d.FullName) (running task)"
      continue
    }}
    if ($Apply) {{
      git -C $Repo worktree remove --force $d.FullName 2>$null | Out-Null
      if (Test-Path -LiteralPath $d.FullName) {{ Remove-Item -LiteralPath $d.FullName -Recurse -Force -ErrorAction SilentlyContinue }}
    }}
    $removed += $d.FullName
  }}
}}
if ($Apply) {{
  git -C $Repo worktree prune 2>$null | Out-Null
}}
$freeGb = [math]::Round((Get-PSDrive C).Free / 1GB, 1)
$prunedPool = $false
if ($Apply -and $freeGb -lt $PruneUnder -and (Test-Path -LiteralPath (Join-Path $Repo 'tools\\build_lock.py'))) {{
  python (Join-Path $Repo 'tools\\build_lock.py') --prune 2>&1 | Out-Null
  $prunedPool = $true
  $freeGb = [math]::Round((Get-PSDrive C).Free / 1GB, 1)
}}
[ordered]@{{
  applied = $Apply
  free_gb = $freeGb
  removed = $removed
  kept = $kept
  pruned_pool = $prunedPool
}} | ConvertTo-Json -Compress -Depth 4
"""
    code, out, err = ssh_run(ssh_cmd, target, ps, timeout=120)
    if code != 0:
        print(f'laptop.py: clean failed: {err or out}', file=sys.stderr)
        return 1
    try:
        data = json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        print(out)
        return 0
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    verb = 'removed' if data['applied'] else 'would remove'
    print(f"Laptop C: {data['free_gb']} GB free; {verb} {len(data['removed'])} detached worktree(s), "
          f"kept {len(data['kept'])}, pruned_pool={data['pruned_pool']}")
    for p in data['removed']:
        print(f'  {verb}: {p}')
    for p in data['kept']:
        print(f'  kept: {p}')
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ssh', default=None, help='override laptop ssh target (default from machine.json or ramaz@192.168.4.12)')
    sub = ap.add_subparsers(dest='action', required=True)

    st = sub.add_parser('status', help='check PC and laptop locks, active lanes, console session, and free disk')
    st.add_argument('--locks', default=DEFAULT_LOCKS)
    st.add_argument('--pool', default=DEFAULT_POOL)
    st.add_argument('--user', default=DEFAULT_USER)
    st.add_argument('--json', action='store_true')

    rn = sub.add_parser('run', help='run any CPU or GPU command on the laptop inside an Interactive desktop task')
    rn.add_argument('--branch', required=True, help='remote branch name (e.g. wip/my-topic)')
    rn.add_argument('--gpu', action='store_true', help='claim LAPTOP-LOCK for the duration of a GPU/e2e/hardware run')
    rn.add_argument('--push', action='store_true', help='push local HEAD to origin/<branch> first (refuses main/master)')
    rn.add_argument('--wait', action='store_true', help='wait for the laptop to become free instead of exiting 4')
    rn.add_argument('--timeout', type=int, default=120, help='timeout in minutes (default 120)')
    rn.add_argument('--repo', default=DEFAULT_REPO)
    rn.add_argument('--work', default=DEFAULT_WORK)
    rn.add_argument('--locks', default=DEFAULT_LOCKS)
    rn.add_argument('--pool', default=DEFAULT_POOL)
    rn.add_argument('--user', default=DEFAULT_USER)
    rn.add_argument('--name', default=None, help='override task/worktree slug')
    rn.add_argument('--out', default=None, help='copy full remote log into this local directory')
    rn.add_argument('--keep-worktree', action='store_true', help='do not auto-remove detached worktree after run')
    rn.add_argument('command', nargs=argparse.REMAINDER, help='command to run on laptop after `--`')

    ck = sub.add_parser('check', help='run Rubylight tools/remote-check.ps1 on the laptop for a pushed branch')
    ck.add_argument('branch', help='pushed branch name')
    ck.add_argument('--wait', action='store_true')
    ck.add_argument('--timeout', type=int, default=180)
    ck.add_argument('--out', default=None)

    cl = sub.add_parser('clean', help='prune finished detached offload/check worktrees (and pool if disk is tight)')
    cl.add_argument('--apply', action='store_true')
    cl.add_argument('--repo', default=DEFAULT_REPO)
    cl.add_argument('--work', default=DEFAULT_WORK)
    cl.add_argument('--prune-pool-under-gb', type=float, default=45.0)
    cl.add_argument('--json', action='store_true')

    args = ap.parse_args()
    cfg = load_machine()
    if args.action == 'status':
        return cmd_status(args, cfg)
    if args.action == 'run':
        return cmd_run(args, cfg)
    if args.action == 'check':
        return cmd_check(args, cfg)
    return cmd_clean(args, cfg)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.exit(main())
