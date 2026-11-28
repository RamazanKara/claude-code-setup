#!/usr/bin/env python3
"""Scoped MCP server for Rubylight workstation hardware validation on WinSta0\\Default.

Supports both Antigravity MCP transports from one zero-dependency stdlib script:
  - Stdio transport:           python hw_mcp.py --stdio
  - Streamable HTTP transport: python hw_mcp.py --http --port 48765 --token-file ~/.claude/secrets/hw-mcp-token

Setup helpers:
  python hw_mcp.py status                  # inspect window station, desktop, session, host, locks
  python hw_mcp.py install-stdio           # register rubylight-hw as a stdio MCP server in Antigravity
  python hw_mcp.py install-http            # run ONCE from a normal WinSta0\\Default PowerShell to install logon HTTP daemon
  python hw_mcp.py uninstall-http          # stop daemon, remove autostart entry, unregister MCP server

Security boundaries:
  - Strictly allowlisted Rubylight hardware scripts and parameters only (no arbitrary shell execution).
  - HTTP mode binds strictly to loopback (127.0.0.1 / ::1 / localhost), checks Origin, and requires Bearer auth.
  - install-http refuses to run unless the caller is already on WinSta0\\Default (never escapes exebox).
  - Preserves exit code 3 (Rubylight AMD encoder hang sentinel — investigate, never retry).
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import datetime
import hmac
import http.server
import json
import math
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

PROTOCOL_VERSIONS = ('2025-03-26', '2024-11-05')
SERVER_INFO = {'name': 'rubylight-hw', 'version': '1.1.0'}
MCP_NAME = 'rubylight-hw'
TASK_NAME = 'RubylightHwMcp'
DEFAULT_PORT = 48765
EXIT_GPU_HANG = 3
MAX_HTTP_SESSIONS = 64

HOME_DIR = Path.home()
CLAUDE_DIR = HOME_DIR / '.claude'
GEMINI_MCP_CONFIG = HOME_DIR / '.gemini' / 'config' / 'mcp_config.json'
DEFAULT_TOKEN_FILE = CLAUDE_DIR / 'secrets' / 'hw-mcp-token'
DEFAULT_PID_FILE = CLAUDE_DIR / 'secrets' / 'hw-mcp.pid'
DEFAULT_REPO = Path(r'C:\src\rubylight-rel')
DEFAULT_WORK_ROOT = Path(r'C:\src\butterpollo-release')
DEFAULT_SERVERINFO = 'http://127.0.0.1:47989/serverinfo'
DEFAULT_LAPTOP_HOST = 'ramaz@192.168.4.12'
DEFAULT_ALLOWED_REPO_ROOTS = (
    Path(r'C:\src'),
    HOME_DIR / 'orca' / 'workspaces',
)

VERSION_RE = re.compile(r'^v?\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?$')
CASE_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
SAFE_VAR_KEYS = ('release', 'repo')
SHELL_META_RE = re.compile(r'[;|&$`"\'<>(){}\r\n\x00-\x1f]')
STREAM_CASES = (
    'h264', 'hevc', 'av1', 'hevc-vrr', 'hevc-recovery', 'hevc-behind',
    'hevc-120', 'hevc-warp', 'pyrowave', 'pyrowave-hdr-444',
)
SMOKE_CODECS = ('AV1', 'HEVC', 'H.264')
HW_BATCHES = ('desktop', 'laptop', 'laptop-host', 'nuc', 'retroid')
LOOPBACK_HOSTS = {'127.0.0.1', '::1', 'localhost'}


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _win_user_object_name(handle: Any) -> str:
    if not handle:
        return 'unknown'
    from ctypes import wintypes as w
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetUserObjectInformationW.argtypes = (
        w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD),
    )
    user32.GetUserObjectInformationW.restype = w.BOOL
    buf = ctypes.create_unicode_buffer(256)
    needed = w.DWORD(0)
    ok = user32.GetUserObjectInformationW(handle, 2, buf, ctypes.sizeof(buf), ctypes.byref(needed))
    return buf.value if ok and buf.value else 'unknown'


def current_window_station_name() -> str:
    """Return the process window station name (e.g. 'WinSta0')."""
    if os.name != 'nt':
        return 'non-windows'
    try:
        from ctypes import wintypes as w
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.GetProcessWindowStation.restype = w.HANDLE
        return _win_user_object_name(user32.GetProcessWindowStation())
    except Exception:
        return 'unknown'


def current_desktop_name() -> str:
    """Return the current thread's Windows desktop name (e.g. 'Default' or 'exebox-...')."""
    if os.name != 'nt':
        return 'non-windows'
    try:
        from ctypes import wintypes as w
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        kernel32.GetCurrentThreadId.restype = w.DWORD
        user32.GetThreadDesktop.argtypes = (w.DWORD,)
        user32.GetThreadDesktop.restype = w.HANDLE
        return _win_user_object_name(user32.GetThreadDesktop(kernel32.GetCurrentThreadId()))
    except Exception:
        return 'unknown'


def current_session_id() -> int:
    """Return the Windows session ID (0 = services/SSH, >= 1 = interactive console)."""
    if os.name != 'nt':
        return -1
    try:
        from ctypes import wintypes as w
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.ProcessIdToSessionId.argtypes = (w.DWORD, ctypes.POINTER(w.DWORD))
        kernel32.ProcessIdToSessionId.restype = w.BOOL
        sid = w.DWORD(0)
        return int(sid.value) if kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)) else -1
    except Exception:
        return -1


def desktop_info() -> dict[str, Any]:
    winsta = current_window_station_name()
    desktop = current_desktop_name()
    session_id = current_session_id()
    ok = (
        os.name == 'nt'
        and session_id > 0
        and winsta.lower() == 'winsta0'
        and desktop.lower() == 'default'
    )
    return {
        'window_station': winsta,
        'desktop': desktop,
        'session_id': session_id,
        'desktop_ok': ok,
        'pid': os.getpid(),
    }


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == 'nt':
        from ctypes import wintypes as w
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.OpenProcess.restype, k.OpenProcess.argtypes = w.HANDLE, (w.DWORD, w.BOOL, w.DWORD)
        k.GetExitCodeProcess.argtypes = (w.HANDLE, ctypes.POINTER(w.DWORD))
        k.CloseHandle.argtypes = (w.HANDLE,)
        h = k.OpenProcess(0x1000, False, pid)
        if not h:
            return ctypes.get_last_error() == 5
        try:
            code = w.DWORD()
            return bool(k.GetExitCodeProcess(h, ctypes.byref(code)) and code.value == 259)
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def process_start_time(pid: int) -> int | None:
    """Return Windows process CreationTime FILETIME (100ns ticks), or None if unavailable."""
    if os.name != 'nt' or pid <= 0:
        return None
    try:
        from ctypes import wintypes as w
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.OpenProcess.restype, k.OpenProcess.argtypes = w.HANDLE, (w.DWORD, w.BOOL, w.DWORD)
        k.GetProcessTimes.argtypes = (
            w.HANDLE, ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME),
            ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME),
        )
        k.GetProcessTimes.restype = w.BOOL
        k.CloseHandle.argtypes = (w.HANDLE,)
        h = k.OpenProcess(0x1000, False, pid)
        if not h:
            return None
        try:
            c, e, kt, ut = w.FILETIME(), w.FILETIME(), w.FILETIME(), w.FILETIME()
            if not k.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
                return None
            return (c.dwHighDateTime << 32) | c.dwLowDateTime
        finally:
            k.CloseHandle(h)
    except Exception:
        return None


def load_machine_config() -> dict[str, Any]:
    path = Path(os.environ.get('CLAUDE_MACHINE_FILE') or (CLAUDE_DIR / 'machine.json'))
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def ssh_and_scp_bins(cfg: dict[str, Any] | None = None) -> tuple[list[str], list[str]]:
    cfg = cfg if cfg is not None else load_machine_config()
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


def laptop_ssh_target(cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg if cfg is not None else load_machine_config()
    return str(((cfg.get('hosts') or {}).get('laptop') or {}).get('ssh') or DEFAULT_LAPTOP_HOST)


def preferred_python() -> str:
    py313 = HOME_DIR / 'AppData' / 'Local' / 'Programs' / 'Python' / 'Python313' / 'python.exe'
    if py313.is_file():
        return str(py313)
    return sys.executable


def probe_installed_host(url: str = DEFAULT_SERVERINFO) -> dict[str, Any]:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=3) as resp:
            body = resp.read().decode('utf-8', 'replace')
        root = ET.fromstring(body)
        state = (root.findtext('state') or '').strip()
        version = (root.findtext('RustHostVersion') or '').strip()
        return {'reachable': True, 'state': state, 'idle': state == 'SUNSHINE_SERVER_FREE', 'version': version}
    except Exception as exc:
        return {'reachable': False, 'state': 'UNREACHABLE', 'idle': False, 'version': '', 'error': str(exc)}


def _is_file_locked(path: Path) -> bool:
    try:
        if os.name == 'nt':
            import msvcrt
            with open(path, 'r+b') as fh:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        return False
    except OSError:
        return True


def probe_locks(work_root: Path = DEFAULT_WORK_ROOT, alive_fn=pid_alive) -> dict[str, Any]:
    locks: dict[str, Any] = {}
    for name in ('RELEASE-LOCK', 'GPU-LOCK', 'BUILD-LOCK', 'PRIORITY'):
        p = work_root / name
        if not p.exists():
            continue
        try:
            content = p.read_text(encoding='utf-8', errors='replace').strip()
        except OSError:
            content = '<unreadable>'
        if name == 'PRIORITY':
            m = re.search(r'\bpid=(\d+)\b', content)
            if m and not alive_fn(int(m.group(1))):
                continue
        locks[name] = content or '<present>'
    lanes_dir = work_root / 'lanes'
    held_lanes = []
    if lanes_dir.is_dir():
        for lf in sorted(lanes_dir.glob('*.lock')):
            if _is_file_locked(lf):
                held_lanes.append(lf.name)
    if held_lanes:
        locks['held_lanes'] = held_lanes
    return locks


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        st = os.lstat(path)
        return bool(getattr(st, 'st_file_attributes', 0) & 0x400)
    except OSError:
        return False


def validate_repo(
    repo_raw: str | None,
    required_relpath: str,
    default_repo: Path = DEFAULT_REPO,
    allowed_roots: tuple[Path, ...] = DEFAULT_ALLOWED_REPO_ROOTS,
) -> Path:
    if repo_raw is not None and not isinstance(repo_raw, str):
        raise ValueError('repo must be a string path')
    candidate = Path(repo_raw) if repo_raw else default_repo
    if any(ord(ch) < 32 for ch in str(candidate)):
        raise ValueError('repo path contains control characters')
    if _is_reparse_or_symlink(candidate):
        raise ValueError(f'repo path must not be a symlink or junction: {candidate}')
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise ValueError(f'repo directory does not exist: {resolved}')
    in_allowed = False
    for root in allowed_roots:
        try:
            root_res = root.resolve()
            if root_res in resolved.parents:
                in_allowed = True
                break
        except OSError:
            continue
    if not in_allowed:
        allowed_txt = ', '.join(str(r) for r in allowed_roots)
        raise ValueError(f'repo {resolved} must be a checkout inside allowed roots: {allowed_txt}')
    rel = Path(required_relpath)
    if rel.is_absolute() or '..' in rel.parts:
        raise ValueError(f'invalid relative script path: {required_relpath}')
    walk = resolved
    for part in rel.parts:
        walk = walk / part
        if _is_reparse_or_symlink(walk):
            raise ValueError(f'script path component must not be a symlink or junction: {walk}')
    target = walk.resolve()
    if resolved not in target.parents or not target.is_file():
        raise ValueError(f'required script missing or invalid: {resolved / required_relpath}')
    return resolved


def validate_version(version: str, required: bool = False) -> str:
    if not isinstance(version, str):
        raise ValueError('version must be a string')
    text = version.strip()
    if not text:
        if required:
            raise ValueError('version is required (e.g. v6.5.0 or 6.5.0)')
        return ''
    if not VERSION_RE.match(text):
        raise ValueError(f'invalid version tag: {text!r}')
    return text


def require_bool(value: Any, name: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f'{name} must be a boolean')
    return value


def require_int(value: Any, name: str, default: int, lo: int, hi: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'{name} must be an integer')
    if not (lo <= value <= hi):
        raise ValueError(f'{name} must be between {lo} and {hi}')
    return value


def require_str_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f'{name} must be an array of strings')
    return list(value)


def validate_hw_vars(
    vars_map: Any,
    default_repo: Path,
    allowed_roots: tuple[Path, ...],
) -> dict[str, str]:
    if vars_map is None:
        return {}
    if not isinstance(vars_map, dict):
        raise ValueError('vars must be an object mapping variable names to string values')
    cleaned: dict[str, str] = {}
    for k, v in vars_map.items():
        if not isinstance(k, str) or k not in SAFE_VAR_KEYS:
            raise ValueError(f'var {k!r} is not allowed; permitted vars: {", ".join(SAFE_VAR_KEYS)}')
        if not isinstance(v, str) or SHELL_META_RE.search(v):
            raise ValueError(f'invalid value for var {k!r}')
        if k == 'release':
            cleaned[k] = validate_version(v, required=True).lstrip('v')
        elif k == 'repo':
            cleaned[k] = str(validate_repo(v, 'tools/hw/run.py', default_repo, allowed_roots))
    return cleaned


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def ps_encoded_command(script: str) -> str:
    return base64.b64encode(script.encode('utf-16-le')).decode('ascii')


def terminate_tree(proc: subprocess.Popen[Any], grace_seconds: float = 3.0) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == 'nt':
            # Processes spawned with CREATE_NO_WINDOW have no shared console, so CTRL_BREAK_EVENT
            # is a no-op; terminate the tree directly and let runner() clean up job-owned locks.
            subprocess.run(
                ['taskkill', '/T', '/F', '/PID', str(proc.pid)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
            )
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        else:
            proc.terminate()
            try:
                proc.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def cleanup_cancelled_locks(work_root: Path, killed_pids: set[int], alive_fn=pid_alive) -> None:
    """Remove workstation lock files in work_root whose recorded PID belongs to a cancelled dead process."""
    if not killed_pids or not work_root.is_dir():
        return
    for name in ('RELEASE-LOCK', 'BUILD-LOCK', 'GPU-LOCK', 'PRIORITY'):
        lock_path = work_root / name
        if not lock_path.is_file():
            continue
        try:
            text = lock_path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        m = re.search(r'\bpid[= ](\d+)\b', text)
        if not m:
            continue
        owner_pid = int(m.group(1))
        if owner_pid not in killed_pids or alive_fn(owner_pid):
            continue
        for _ in range(10):
            try:
                lock_path.unlink(missing_ok=True)
                break
            except PermissionError:
                time.sleep(0.1)


def read_tail(path: Path, lines: int = 40) -> str:
    if lines <= 0 or not path.is_file():
        return ''
    try:
        content = path.read_text(encoding='utf-8', errors='replace').splitlines()
        return '\n'.join(content[-lines:])
    except OSError as exc:
        return f'<failed reading log: {exc}>'


def find_fresh_validation_json(work_root: Path, bare_version: str, started_wall: float) -> Path | None:
    cutoff = started_wall - 1.0
    if bare_version:
        p = work_root / bare_version / 'VALIDATION.json'
        try:
            if p.is_file() and p.stat().st_mtime >= cutoff:
                return p
        except OSError:
            return None
        return None
    newest: tuple[float, Path] | None = None
    if not work_root.is_dir():
        return None
    for child in work_root.iterdir():
        if not child.is_dir():
            continue
        cand = child / 'VALIDATION.json'
        try:
            if cand.is_file():
                mtime = cand.stat().st_mtime
                if mtime >= cutoff and (newest is None or mtime > newest[0]):
                    newest = (mtime, cand)
        except OSError:
            continue
    return newest[1] if newest else None


class Job:
    def __init__(self, job_id: str, kind: str, summary: str, job_dir: Path, log_path: Path):
        self.job_id = job_id
        self.kind = kind
        self.summary = summary
        self.job_dir = job_dir
        self.log_path = log_path
        self.status = 'running'
        self.exit_code: int | None = None
        self.started_at = utc_now_iso()
        self.started_mono = time.monotonic()
        self.finished_at: str | None = None
        self.error: str = ''
        self.artifacts: dict[str, Any] = {}
        self.done = threading.Event()
        self.cancel_requested = threading.Event()
        self._lock = threading.Lock()
        self._procs: list[subprocess.Popen[Any]] = []

    def spawn_tracked(self, argv: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
        """Atomically check cancel_requested and spawn/track a subprocess under _lock."""
        with self._lock:
            if self.cancel_requested.is_set():
                raise RuntimeError('job cancelled')
            if os.name == 'nt':
                flags = kwargs.get('creationflags', 0)
                kwargs['creationflags'] = (
                    flags
                    | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                    | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
                )
            proc = subprocess.Popen(argv, **kwargs)
            self._procs.append(proc)
            return proc

    def terminate_all(self) -> set[int]:
        with self._lock:
            procs = list(self._procs)
        pids: set[int] = set()
        for p in procs:
            pids.add(p.pid)
            terminate_tree(p)
        return pids

    def cancel(self, wait_timeout: float = 35.0) -> bool:
        if self.done.is_set():
            return False
        self.cancel_requested.set()
        self.terminate_all()
        # Wait for runner()'s finally block (including bounded remote cleanup) to finish and signal self.done.
        self.done.wait(timeout=wait_timeout)
        return True

    def finish(self, exit_code: int, error: str = '') -> None:
        with self._lock:
            if self.done.is_set():
                return
            self.exit_code = int(exit_code)
            self.error = error
            if self.cancel_requested.is_set():
                self.status = 'cancelled'
            elif exit_code == EXIT_GPU_HANG:
                self.status = 'gpu_hang'
            elif exit_code == 0 and not error:
                self.status = 'passed'
            else:
                self.status = 'failed'
            self.finished_at = utc_now_iso()
            self.done.set()

    def snapshot(self, tail_lines: int = 40) -> dict[str, Any]:
        elapsed = round(time.monotonic() - self.started_mono, 1)
        data: dict[str, Any] = {
            'job_id': self.job_id,
            'kind': self.kind,
            'summary': self.summary,
            'status': self.status,
            'exit_code': self.exit_code,
            'gpu_hang': self.exit_code == EXIT_GPU_HANG,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
            'elapsed_seconds': elapsed,
            'log_path': str(self.log_path),
            'log_tail': read_tail(self.log_path, tail_lines),
            'artifacts': self.artifacts,
        }
        if self.error:
            data['error'] = self.error
        if self.exit_code == EXIT_GPU_HANG:
            data['warning'] = 'AMD encoder hang detected (exit code 3) — investigate, NEVER retry.'
        return data


class JobManager:
    def __init__(
        self,
        work_root: Path = DEFAULT_WORK_ROOT,
        jobs_dir: Path | None = None,
        default_repo: Path = DEFAULT_REPO,
        allowed_repo_roots: tuple[Path, ...] = DEFAULT_ALLOWED_REPO_ROOTS,
        desktop_probe=desktop_info,
        host_probe=probe_installed_host,
        lock_probe=probe_locks,
        transport: str = 'stdio',
    ):
        self.work_root = work_root
        self.jobs_dir = jobs_dir or (work_root / 'hw-mcp-jobs')
        self.default_repo = default_repo
        self.allowed_repo_roots = allowed_repo_roots
        self.desktop_probe = desktop_probe
        self.host_probe = host_probe
        self.lock_probe = lock_probe
        self.transport = transport
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []

    def active_job(self) -> Job | None:
        with self._lock:
            for jid in reversed(self._order):
                if not self._jobs[jid].done.is_set():
                    return self._jobs[jid]
        return None

    def _new_job(self, kind: str, summary: str) -> Job:
        with self._lock:
            for jid in reversed(self._order):
                if not self._jobs[jid].done.is_set():
                    raise RuntimeError(f'another hardware job is already running: {jid} ({self._jobs[jid].summary})')
            stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
            job_id = f'{kind}-{stamp}-{secrets.token_hex(2)}'
            job_dir = self.jobs_dir / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            job = Job(job_id, kind, summary, job_dir, job_dir / 'output.log')
            self._jobs[job_id] = job
            self._order.append(job_id)
            return job

    def _require_default_desktop(self, action: str) -> dict[str, Any]:
        info = self.desktop_probe()
        if not info.get('desktop_ok'):
            raise RuntimeError(
                f"{action} requires WinSta0\\Default (session > 0), but this MCP server is running on "
                f"window_station={info.get('window_station')!r}, desktop={info.get('desktop')!r}, "
                f"session_id={info.get('session_id')}. "
                f"Run 'python ~/.claude/tools/hw_mcp.py install-http' once from a normal Windows PowerShell "
                f"to serve rubylight-hw from your interactive desktop session."
            )
        return info

    def status(self) -> dict[str, Any]:
        dinfo = self.desktop_probe()
        active = self.active_job()
        with self._lock:
            recent = [
                {'job_id': self._jobs[j].job_id, 'kind': self._jobs[j].kind, 'status': self._jobs[j].status,
                 'exit_code': self._jobs[j].exit_code}
                for j in self._order[-10:]
            ]
        return {
            **dinfo,
            'transport': self.transport,
            'settings_ps1_present': (self.work_root / 'settings.ps1').is_file(),
            'installed_host': self.host_probe(),
            'locks': self.lock_probe(self.work_root),
            'active_job': active.job_id if active else None,
            'recent_jobs': recent,
        }

    def start_release_check(
        self,
        version: str | None = None,
        no_install: bool = False,
        no_upload: bool = False,
        skip_streams: list[str] | None = None,
        repo: str | None = None,
    ) -> dict[str, Any]:
        self._require_default_desktop('start_release_check')
        ver = validate_version(version if version is not None else '', required=False)
        no_install = require_bool(no_install, 'no_install', False)
        no_upload = require_bool(no_upload, 'no_upload', False)
        skips = require_str_list(skip_streams, 'skip_streams')
        bad_skips = [s for s in skips if s not in STREAM_CASES]
        if bad_skips:
            raise ValueError(f'unknown skip_streams {bad_skips}; allowed: {", ".join(STREAM_CASES)}')
        repo_path = validate_repo(repo, 'rust/release/check.ps1', self.default_repo, self.allowed_repo_roots)
        script = repo_path / 'rust' / 'release' / 'check.ps1'

        ps_parts = ['&', ps_quote(str(script))]
        if ver:
            ps_parts += ['-Version', ps_quote(ver)]
        if self.work_root != DEFAULT_WORK_ROOT:
            ps_parts += ['-Work', ps_quote(str(self.work_root))]
        if no_install:
            ps_parts.append('-NoInstall')
        if no_upload:
            ps_parts.append('-NoUpload')
        if skips:
            arr = '@(' + ', '.join(ps_quote(s) for s in skips) + ')'
            ps_parts += ['-SkipStreams', arr]
        ps_cmd = ' '.join(ps_parts) + '; exit $LASTEXITCODE'
        argv = ['pwsh', '-NoProfile', '-OutputFormat', 'Text', '-EncodedCommand', ps_encoded_command(ps_cmd)]

        summary = f"check.ps1 {ver or 'latest'}" + (' -NoInstall' if no_install else '') + (' -NoUpload' if no_upload else '')
        job = self._new_job('release-check', summary)
        started_wall = time.time()

        def runner() -> None:
            rc = 1
            err = ''
            try:
                with open(job.log_path, 'w', encoding='utf-8', errors='replace') as lf:
                    proc = job.spawn_tracked(argv, cwd=str(repo_path), stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT)
                    rc = proc.wait()
                bare = ver.lstrip('v') if ver else ''
                val_path = find_fresh_validation_json(self.work_root, bare, started_wall)
                if val_path is not None:
                    job.artifacts['validation_json_path'] = str(val_path)
                    try:
                        job.artifacts['validation'] = json.loads(val_path.read_text(encoding='utf-8'))
                    except ValueError:
                        pass
            except Exception as exc:
                err = str(exc)
            finally:
                pids = job.terminate_all()
                if job.cancel_requested.is_set():
                    cleanup_cancelled_locks(self.work_root, pids)
                job.finish(rc, err)

        threading.Thread(target=runner, name=job.job_id, daemon=True).start()
        return {'job_id': job.job_id, 'status': job.status, 'summary': summary, 'log_path': str(job.log_path)}

    def start_real_client_smoke(
        self,
        version: str,
        codecs: list[str] | None = None,
        seconds: int = 60,
        hz: int = 240,
        repo: str | None = None,
    ) -> dict[str, Any]:
        self._require_default_desktop('start_real_client_smoke')
        ver = validate_version(version, required=True)
        tag = ver if ver.startswith('v') else f'v{ver}'
        codec_list = require_str_list(codecs, 'codecs') if codecs is not None else ['AV1', 'HEVC']
        bad_codecs = [c for c in codec_list if c not in SMOKE_CODECS]
        if bad_codecs or not codec_list:
            raise ValueError(f'invalid codecs {bad_codecs}; allowed: {", ".join(SMOKE_CODECS)}')
        seconds = require_int(seconds, 'seconds', 60, 15, 300)
        hz = require_int(hz, 'hz', 240, 60, 480)
        repo_path = validate_repo(repo, 'tools/hw/probe-on-new-display.ps1', self.default_repo, self.allowed_repo_roots)
        validate_repo(str(repo_path), 'rust/release/smoke.ps1', self.default_repo, self.allowed_repo_roots)

        probe_ps1 = repo_path / 'tools' / 'hw' / 'probe-on-new-display.ps1'
        smoke_ps1 = repo_path / 'rust' / 'release' / 'smoke.ps1'
        minutes = max(3, math.ceil((len(codec_list) * (seconds + 40) + 90) / 60))
        summary = f"smoke.ps1 {tag} ({','.join(codec_list)}, {seconds}s)"
        job = self._new_job('real-client-smoke', summary)

        def runner() -> None:
            probe_proc: subprocess.Popen[Any] | None = None
            probe_log = job.job_dir / 'probe.log'
            probe_out = job.job_dir / 'probe'
            cfg = load_machine_config()
            ssh_cmd, scp_cmd = ssh_and_scp_bins(cfg)
            target = laptop_ssh_target(cfg)
            remote_script_win = rf'C:\src\butterpollo-release\hw-mcp-smoke-{job.job_id}.ps1'
            remote_json_win = rf'C:\src\butterpollo-release\hw-mcp-smoke-{job.job_id}.json'
            remote_pid_win = rf'C:\src\butterpollo-release\hw-mcp-smoke-{job.job_id}.pid'
            remote_script_scp = remote_script_win.replace('\\', '/')
            remote_json_scp = remote_json_win.replace('\\', '/')
            local_json = job.job_dir / 'REAL-CLIENT.json'
            remote_touched = False
            rc = 1
            err = ''
            try:
                with open(probe_log, 'w', encoding='utf-8', errors='replace') as plf:
                    probe_proc = job.spawn_tracked(
                        ['pwsh', '-NoProfile', '-File', str(probe_ps1), '-Minutes', str(minutes), '-Out', str(probe_out), '-Hz', str(hz)],
                        cwd=str(repo_path), stdin=subprocess.DEVNULL, stdout=plf, stderr=subprocess.STDOUT,
                    )
                if job.cancel_requested.wait(timeout=2.5):
                    raise RuntimeError('cancelled before laptop smoke start')

                with open(job.log_path, 'w', encoding='utf-8', errors='replace') as lf:
                    lf.write(f'[{utc_now_iso()}] copying smoke.ps1 to {target}\n')
                    lf.flush()
                    remote_touched = True
                    mk_ps = "New-Item -ItemType Directory -Force 'C:\\src\\butterpollo-release' | Out-Null"
                    mk = job.spawn_tracked(
                        [*ssh_cmd, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', target,
                         'powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand', ps_encoded_command(mk_ps)],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    mk.wait(timeout=30)
                    cp = job.spawn_tracked(
                        [*scp_cmd, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-q',
                         str(smoke_ps1), f'{target}:{remote_script_scp}'],
                        stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
                    )
                    if cp.wait(timeout=45) != 0:
                        rc, err = cp.returncode or 1, f'scp smoke.ps1 to {target} failed'
                        return

                    codec_arr = '@(' + ', '.join(ps_quote(c) for c in codec_list) + ')'
                    inner_ps = (
                        f"$ErrorActionPreference = 'Stop'; "
                        f"Set-Content -LiteralPath {ps_quote(remote_pid_win)} -Value $PID -Encoding ascii; "
                        f"try {{ & {ps_quote(remote_script_win)} "
                        f"-Version {ps_quote(tag)} -Codecs {codec_arr} -Seconds {seconds} "
                        f"-Out {ps_quote(remote_json_win)}; exit $LASTEXITCODE }} "
                        f"finally {{ Remove-Item -LiteralPath {ps_quote(remote_script_win)}, {ps_quote(remote_pid_win)} -Force -ErrorAction SilentlyContinue }}"
                    )
                    # Wrap pwsh inside PowerShell 5.1 (OpenSSH DefaultShell) so exact $LASTEXITCODE (including 3) survives.
                    outer_ps = (
                        "$pwsh = (Get-Command pwsh -ErrorAction SilentlyContinue).Source; "
                        "if (-not $pwsh -and (Test-Path 'C:\\Program Files\\PowerShell\\7\\pwsh.exe')) { $pwsh = 'C:\\Program Files\\PowerShell\\7\\pwsh.exe' }; "
                        "if (-not $pwsh) { Write-Error 'pwsh.exe not found on laptop'; exit 1 }; "
                        f"& $pwsh -NoProfile -NonInteractive -OutputFormat Text -EncodedCommand {ps_quote(ps_encoded_command(inner_ps))}; "
                        "exit $LASTEXITCODE"
                    )
                    ssh_argv = [
                        *ssh_cmd, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', target,
                        'powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand', ps_encoded_command(outer_ps),
                    ]
                    lf.write(f'[{utc_now_iso()}] launching smoke.ps1 on {target}\n')
                    lf.flush()
                    ssh_proc = job.spawn_tracked(ssh_argv, stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT)
                    rc = ssh_proc.wait()

                    if not job.cancel_requested.is_set():
                        got = subprocess.run(
                            [*scp_cmd, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-q',
                             f'{target}:{remote_json_scp}', str(local_json)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                        )
                        job.artifacts['probe_log'] = str(probe_log)
                        if got.returncode == 0 and local_json.is_file():
                            job.artifacts['real_client_json_path'] = str(local_json)
                            try:
                                job.artifacts['real_client'] = json.loads(local_json.read_text(encoding='utf-8-sig'))
                            except ValueError:
                                pass
            except Exception as exc:
                err = str(exc)
            finally:
                job.terminate_all()
                if remote_touched:
                    # Clean only this job's remote files; on cancel stop only this job's recorded remote PID/task/lock.
                    cleanup_ps = ''
                    if job.cancel_requested.is_set():
                        cleanup_ps += (
                            f"$pf = {ps_quote(remote_pid_win)}; "
                            "if (Test-Path -LiteralPath $pf) { "
                            "$rpid = (Get-Content -LiteralPath $pf -Raw -ErrorAction SilentlyContinue).Trim(); "
                            "if ($rpid -match '^\\d+$') { "
                            "$tn = \"rubylight-smoke-$rpid\"; "
                            "Stop-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue; "
                            "Unregister-ScheduledTask -TaskName $tn -Confirm:$false -ErrorAction SilentlyContinue; "
                            f"Get-CimInstance Win32_Process -Filter \"Name='pwsh.exe'\" -ErrorAction SilentlyContinue | "
                            f"Where-Object {{ $_.CommandLine -like '*hw-mcp-smoke-{job.job_id}*' }} | "
                            "ForEach-Object { taskkill /T /F /PID $_.ProcessId 2>$null | Out-Null }; "
                            "taskkill /T /F /PID $rpid 2>$null | Out-Null; "
                            "$lf = 'C:\\src\\butterpollo-release\\LAPTOP-LOCK'; "
                            "if (Test-Path -LiteralPath $lf) { $t = Get-Content -LiteralPath $lf -Raw -ErrorAction SilentlyContinue; "
                            "if ($t -match ('^smoke\\b.*\\bpid=' + $rpid + '\\b') -and -not (Get-Process -Id ([int]$rpid) -ErrorAction SilentlyContinue)) { "
                            "Remove-Item -LiteralPath $lf -Force -ErrorAction SilentlyContinue } } } }; "
                        )
                    cleanup_ps += (
                        f"Remove-Item -LiteralPath {ps_quote(remote_script_win)}, {ps_quote(remote_json_win)}, {ps_quote(remote_pid_win)} "
                        "-Force -ErrorAction SilentlyContinue"
                    )
                    try:
                        subprocess.run(
                            [*ssh_cmd, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', target,
                             'powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand', ps_encoded_command(cleanup_ps)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25,
                        )
                    except Exception:
                        pass
                job.finish(rc, err)

        threading.Thread(target=runner, name=job.job_id, daemon=True).start()
        return {'job_id': job.job_id, 'status': job.status, 'summary': summary, 'log_path': str(job.log_path)}

    def start_hw_batch(
        self,
        batch: str,
        only: list[str] | None = None,
        vars_map: dict[str, str] | None = None,
        dry_run: bool = False,
        repo: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(batch, str) or batch not in HW_BATCHES:
            raise ValueError(f'invalid batch {batch!r}; allowed: {", ".join(HW_BATCHES)}')
        dry_run = require_bool(dry_run, 'dry_run', False)
        if batch == 'desktop' and not dry_run:
            self._require_default_desktop('start_hw_batch(desktop)')
        only_list = require_str_list(only, 'only')
        for cid in only_list:
            if not CASE_ID_RE.match(cid):
                raise ValueError(f'invalid case id: {cid!r}')
        var_items = validate_hw_vars(vars_map, self.default_repo, self.allowed_repo_roots)
        repo_path = validate_repo(repo, 'tools/hw/run.py', self.default_repo, self.allowed_repo_roots)
        run_py = repo_path / 'tools' / 'hw' / 'run.py'

        argv = [preferred_python(), str(run_py), '--batch', batch]
        if only_list:
            argv += ['--only', *only_list]
        for k, v in var_items.items():
            argv += ['--var', f'{k}={v}']
        if dry_run:
            argv.append('--dry-run')

        summary = f"hw batch {batch}" + (f" ({','.join(only_list)})" if only_list else '') + (' --dry-run' if dry_run else '')
        job = self._new_job('hw-batch', summary)

        def runner() -> None:
            rc = 1
            err = ''
            try:
                with open(job.log_path, 'w', encoding='utf-8', errors='replace') as lf:
                    proc = job.spawn_tracked(argv, cwd=str(repo_path), stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT)
                    rc = proc.wait()
            except Exception as exc:
                err = str(exc)
            finally:
                pids = job.terminate_all()
                if job.cancel_requested.is_set():
                    cleanup_cancelled_locks(self.work_root, pids)
                job.finish(rc, err)

        threading.Thread(target=runner, name=job.job_id, daemon=True).start()
        return {'job_id': job.job_id, 'status': job.status, 'summary': summary, 'log_path': str(job.log_path)}

    def job_status(self, job_id: str | None = None, wait_seconds: int = 0, tail_lines: int = 40) -> dict[str, Any]:
        if job_id is not None and not isinstance(job_id, str):
            raise ValueError('job_id must be a string')
        wait_s = require_int(wait_seconds, 'wait_seconds', 0, 0, 120)
        tail = require_int(tail_lines, 'tail_lines', 40, 0, 200)
        with self._lock:
            if not self._order:
                raise ValueError('no hardware jobs have been started yet')
            jid = job_id or self._order[-1]
            job = self._jobs.get(jid)
        if not job:
            raise ValueError(f'unknown job_id: {jid!r}')
        if wait_s > 0:
            job.done.wait(timeout=wait_s)
        return job.snapshot(tail_lines=tail)

    def job_cancel(self, job_id: str) -> dict[str, Any]:
        if not isinstance(job_id, str) or not job_id:
            raise ValueError('job_id is required')
        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            raise ValueError(f'unknown job_id: {job_id!r}')
        was_running = job.cancel()
        return {'job_id': job.job_id, 'cancelled': was_running, 'status': job.status}


TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        'name': 'hw_status',
        'description': (
            'Check desktop visibility (WinSta0\\Default vs exebox), installed Rubylight host idle/version, '
            'shared locks (RELEASE-LOCK, GPU-LOCK, BUILD-LOCK, live PRIORITY), and active/recent jobs.'
        ),
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'start_release_check',
        'description': (
            'Start rust/release/check.ps1 on WinSta0\\Default as an async job and return job_id immediately. '
            'Poll with job_status (supports wait_seconds up to 120). Preserves exit code 3 (AMD encoder hang).'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'version': {'type': 'string', 'description': 'Release tag or version (e.g. v6.5.0 or v6.5.0-rc.463). Omit for newest full release.'},
                'no_install': {'type': 'boolean', 'description': 'Pass -NoInstall (for pre-release RC validation).', 'default': False},
                'no_upload': {'type': 'boolean', 'description': 'Pass -NoUpload (skip uploading VALIDATION.json).', 'default': False},
                'skip_streams': {'type': 'array', 'items': {'type': 'string', 'enum': list(STREAM_CASES)}},
                'repo': {'type': 'string', 'description': 'Rubylight checkout path (default C:\\src\\rubylight-rel).'},
            },
            'additionalProperties': False,
        },
    },
    {
        'name': 'start_real_client_smoke',
        'description': (
            'Run probe-on-new-display.ps1 on WinSta0\\Default in lockstep with rust/release/smoke.ps1 on the '
            'measurement laptop, returning job_id immediately. Produces REAL-CLIENT.json.'
        ),
        'inputSchema': {
            'type': 'object',
            'required': ['version'],
            'properties': {
                'version': {'type': 'string', 'description': 'Installed host version tag (e.g. v6.5.0).'},
                'codecs': {'type': 'array', 'items': {'type': 'string', 'enum': list(SMOKE_CODECS)}},
                'seconds': {'type': 'integer', 'minimum': 15, 'maximum': 300, 'default': 60},
                'hz': {'type': 'integer', 'minimum': 60, 'maximum': 480, 'default': 240},
                'repo': {'type': 'string', 'description': 'Rubylight checkout path (default C:\\src\\rubylight-rel).'},
            },
            'additionalProperties': False,
        },
    },
    {
        'name': 'start_hw_batch',
        'description': 'Run tools/hw/run.py for a hardware test batch (desktop, laptop, laptop-host, nuc, retroid) as an async job.',
        'inputSchema': {
            'type': 'object',
            'required': ['batch'],
            'properties': {
                'batch': {'type': 'string', 'enum': list(HW_BATCHES)},
                'only': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Optional catalog case IDs.'},
                'vars': {
                    'type': 'object',
                    'additionalProperties': {'type': 'string'},
                    'description': 'Optional safe catalog overrides (permitted keys: release, repo).',
                },
                'dry_run': {'type': 'boolean', 'default': False},
                'repo': {'type': 'string', 'description': 'Rubylight checkout path (default C:\\src\\rubylight-rel).'},
            },
            'additionalProperties': False,
        },
    },
    {
        'name': 'job_status',
        'description': 'Check status, log tail, exit code, gpu_hang flag (exit code 3), and JSON artifacts of a hardware job.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'job_id': {'type': 'string', 'description': 'Job ID (defaults to most recent job).'},
                'wait_seconds': {'type': 'integer', 'minimum': 0, 'maximum': 120, 'default': 0},
                'tail_lines': {'type': 'integer', 'minimum': 0, 'maximum': 200, 'default': 40},
            },
            'additionalProperties': False,
        },
    },
    {
        'name': 'job_cancel',
        'description': 'Cancel a running hardware job and terminate its process tree.',
        'inputSchema': {
            'type': 'object',
            'required': ['job_id'],
            'properties': {'job_id': {'type': 'string'}},
            'additionalProperties': False,
        },
    },
]
ALLOWED_ARGS = {t['name']: set(t['inputSchema']['properties']) for t in TOOLS_SCHEMA}


class McpDispatcher:
    def __init__(self, manager: JobManager):
        self.manager = manager

    def handle_payload(self, payload: Any) -> Any:
        """Handle a single JSON-RPC object or a JSON-RPC 2.0 batch array."""
        if isinstance(payload, list):
            if not payload:
                return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid Request: empty batch'}}
            responses = []
            for item in payload:
                resp = self.handle_message(item)
                if resp is not None:
                    responses.append(resp)
            return responses if responses else None
        return self.handle_message(payload)

    def handle_message(self, msg: Any) -> dict[str, Any] | None:
        if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0' or not isinstance(msg.get('method'), str):
            return {'jsonrpc': '2.0', 'id': msg.get('id') if isinstance(msg, dict) else None,
                    'error': {'code': -32600, 'message': 'Invalid Request'}}
        method = msg['method']
        if 'id' not in msg:
            return None
        req_id = msg.get('id')
        params = msg.get('params') or {}

        if method == 'initialize':
            requested = params.get('protocolVersion') if isinstance(params, dict) else None
            version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return {
                'jsonrpc': '2.0',
                'id': req_id,
                'result': {
                    'protocolVersion': version,
                    'capabilities': {'tools': {'listChanged': False}},
                    'serverInfo': SERVER_INFO,
                },
            }
        if method == 'ping':
            return {'jsonrpc': '2.0', 'id': req_id, 'result': {}}
        if method == 'tools/list':
            return {'jsonrpc': '2.0', 'id': req_id, 'result': {'tools': TOOLS_SCHEMA}}
        if method == 'tools/call':
            if not isinstance(params, dict) or not isinstance(params.get('name'), str):
                return {'jsonrpc': '2.0', 'id': req_id, 'error': {'code': -32602, 'message': 'Missing tool name'}}
            args = params.get('arguments')
            if args is None:
                args = {}
            if not isinstance(args, dict):
                return {'jsonrpc': '2.0', 'id': req_id, 'error': {'code': -32602, 'message': 'Tool arguments must be an object'}}
            return {'jsonrpc': '2.0', 'id': req_id, 'result': self._call_tool(params['name'], args)}
        return {'jsonrpc': '2.0', 'id': req_id, 'error': {'code': -32601, 'message': f'Method not found: {method}'}}

    def _call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            if name not in ALLOWED_ARGS:
                raise ValueError(f'unknown tool: {name}')
            extra_keys = set(args) - ALLOWED_ARGS[name]
            if extra_keys:
                raise ValueError(f'unexpected argument(s) for {name}: {", ".join(sorted(extra_keys))}')
            if name == 'hw_status':
                out = self.manager.status()
            elif name == 'start_release_check':
                out = self.manager.start_release_check(
                    version=args.get('version'),
                    no_install=args.get('no_install', False),
                    no_upload=args.get('no_upload', False),
                    skip_streams=args.get('skip_streams'),
                    repo=args.get('repo'),
                )
            elif name == 'start_real_client_smoke':
                if 'version' not in args:
                    raise ValueError('version is required')
                out = self.manager.start_real_client_smoke(
                    version=args['version'],
                    codecs=args.get('codecs'),
                    seconds=args.get('seconds', 60),
                    hz=args.get('hz', 240),
                    repo=args.get('repo'),
                )
            elif name == 'start_hw_batch':
                if 'batch' not in args:
                    raise ValueError('batch is required')
                out = self.manager.start_hw_batch(
                    batch=args['batch'],
                    only=args.get('only'),
                    vars_map=args.get('vars'),
                    dry_run=args.get('dry_run', False),
                    repo=args.get('repo'),
                )
            elif name == 'job_status':
                out = self.manager.job_status(
                    job_id=args.get('job_id'),
                    wait_seconds=args.get('wait_seconds', 0),
                    tail_lines=args.get('tail_lines', 40),
                )
            elif name == 'job_cancel':
                out = self.manager.job_cancel(job_id=args.get('job_id'))
            else:
                raise ValueError(f'unknown tool: {name}')
            return {'content': [{'type': 'text', 'text': json.dumps(out, indent=2)}], 'isError': False}
        except Exception as exc:
            err = {'error': str(exc), 'tool': name}
            return {'content': [{'type': 'text', 'text': json.dumps(err, indent=2)}], 'isError': True}


def run_stdio(dispatcher: McpDispatcher, stdin=None, stdout=None) -> None:
    inp = stdin or sys.stdin
    out = stdout or sys.stdout
    while True:
        try:
            raw = inp.readline()
        except UnicodeDecodeError as exc:
            resp = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': f'Decode error: {exc}'}}
            out.write(json.dumps(resp) + '\n')
            out.flush()
            continue
        if not raw:
            break
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError as exc:
            resp = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': f'Parse error: {exc}'}}
        else:
            resp = dispatcher.handle_payload(payload)
        if resp is not None:
            out.write(json.dumps(resp) + '\n')
            out.flush()


class _ExclusiveThreadingHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self) -> None:
        if os.name == 'nt' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class _IPv6ThreadingHTTPServer(_ExclusiveThreadingHTTPServer):
    address_family = socket.AF_INET6


def _is_loopback_origin(origin: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(origin)
        return parsed.scheme in ('http', 'https') and (parsed.hostname or '').lower() in LOOPBACK_HOSTS
    except ValueError:
        return False


def make_http_server(
    dispatcher: McpDispatcher,
    host: str,
    port: int,
    token: str,
) -> http.server.ThreadingHTTPServer:
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f'refusing non-loopback bind host {host!r}; allowed: {sorted(LOOPBACK_HOSTS)}')
    if not token or len(token) < 16:
        raise ValueError('HTTP mode requires a bearer token of at least 16 characters')
    sessions: dict[str, float] = {}
    sessions_lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt: str, *args: Any) -> None:
            pass

        def _send_json(self, code: int, payload: Any, extra_headers: dict[str, str] | None = None) -> None:
            body = json.dumps(payload).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _check_origin(self) -> bool:
            origin = self.headers.get('Origin')
            if origin is not None and not _is_loopback_origin(origin):
                self._send_json(403, {'error': 'forbidden origin'})
                return False
            return True

        def _check_auth(self) -> bool:
            auth = self.headers.get('Authorization', '')
            if not auth.startswith('Bearer '):
                self._send_json(401, {'error': 'missing bearer token'})
                return False
            if not hmac.compare_digest(auth[7:].strip(), token):
                self._send_json(403, {'error': 'invalid bearer token'})
                return False
            return True

        def do_GET(self) -> None:
            if not self._check_origin():
                return
            path = self.path.split('?', 1)[0]
            if path == '/health':
                self._send_json(200, {'ok': True, **dispatcher.manager.desktop_probe()})
                return
            if path == '/mcp':
                self.send_response(405)
                self.send_header('Allow', 'POST, DELETE')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            self._send_json(404, {'error': 'not found'})

        def do_DELETE(self) -> None:
            if not self._check_origin():
                return
            path = self.path.split('?', 1)[0]
            if path != '/mcp':
                self._send_json(404, {'error': 'not found'})
                return
            if not self._check_auth():
                return
            sid = self.headers.get('Mcp-Session-Id')
            if sid:
                with sessions_lock:
                    sessions.pop(sid, None)
            self.send_response(204)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def do_POST(self) -> None:
            if not self._check_origin():
                return
            path = self.path.split('?', 1)[0]
            if path != '/mcp':
                self._send_json(404, {'error': 'not found'})
                return
            if not self._check_auth():
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                self._send_json(400, {'error': 'invalid Content-Length'})
                return
            if length <= 0 or length > 2_000_000:
                self._send_json(400, {'error': 'invalid body length'})
                return
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode('utf-8'))
            except ValueError as exc:
                self._send_json(400, {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': f'Parse error: {exc}'}})
                return

            is_init = isinstance(payload, dict) and payload.get('method') == 'initialize'
            req_sid = self.headers.get('Mcp-Session-Id')
            extra: dict[str, str] = {}
            if is_init:
                sid = secrets.token_hex(16)
                with sessions_lock:
                    if len(sessions) >= MAX_HTTP_SESSIONS:
                        oldest = min(sessions, key=sessions.get)
                        sessions.pop(oldest, None)
                    sessions[sid] = time.monotonic()
                extra['Mcp-Session-Id'] = sid
            elif req_sid:
                with sessions_lock:
                    if req_sid not in sessions:
                        self._send_json(404, {'error': 'unknown or expired Mcp-Session-Id'})
                        return
                    sessions[req_sid] = time.monotonic()
                extra['Mcp-Session-Id'] = req_sid

            resp = dispatcher.handle_payload(payload)
            if resp is None:
                self.send_response(202)
                for k, v in extra.items():
                    self.send_header(k, v)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            self._send_json(200, resp, extra_headers=extra)

    server_cls = _IPv6ThreadingHTTPServer if ':' in host else _ExclusiveThreadingHTTPServer
    server = server_cls((host, port), Handler)
    server.daemon_threads = True
    return server


def update_gemini_mcp_config(entry: dict[str, Any] | None, config_path: Path = GEMINI_MCP_CONFIG) -> None:
    """Merge or remove the rubylight-hw entry in ~/.gemini/config/mcp_config.json without touching other servers."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    cur: dict[str, Any] = {}
    if config_path.is_file():
        raw = config_path.read_text(encoding='utf-8-sig').strip()
        if raw:
            cur = json.loads(raw)
    servers = cur.setdefault('mcpServers', {})
    if not isinstance(servers, dict):
        servers = {}
        cur['mcpServers'] = servers
    if entry is None:
        servers.pop(MCP_NAME, None)
    else:
        servers[MCP_NAME] = entry
    config_path.write_text(json.dumps(cur, indent=2) + '\n', encoding='utf-8', newline='\n')


def restrict_file_acl(path: Path) -> None:
    if os.name != 'nt':
        os.chmod(path, 0o600)
        return
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip('\\')
    if not user:
        return
    subprocess.run(
        ['icacls', str(path), '/inheritance:r', '/grant:r', f'{user}:(R,W)', '*S-1-5-18:(F)'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def ensure_token(token_file: Path) -> str:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    if token_file.is_file():
        tok = token_file.read_text(encoding='utf-8').strip()
        if len(tok) >= 16:
            return tok
    tok = secrets.token_hex(32)
    token_file.write_text(tok + '\n', encoding='utf-8', newline='\n')
    restrict_file_acl(token_file)
    return tok


def write_pid_record(pid_file: Path) -> None:
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    rec = {'pid': os.getpid(), 'start_time': process_start_time(os.getpid())}
    pid_file.write_text(json.dumps(rec) + '\n', encoding='utf-8', newline='\n')


def stop_recorded_daemon(pid_file: Path) -> bool:
    """Stop the HTTP daemon recorded in pid_file only when PID and process CreationTime still match."""
    if not pid_file.is_file():
        return False
    try:
        raw = pid_file.read_text(encoding='utf-8').strip()
        rec = json.loads(raw)
        old_pid = int(rec.get('pid', 0))
        recorded_start = rec.get('start_time')
    except (OSError, ValueError, AttributeError):
        return False
    if old_pid <= 0:
        return False
    if os.name == 'nt':
        actual_start = process_start_time(old_pid)
        if actual_start is None or recorded_start is None or actual_start != recorded_start:
            return False
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(old_pid)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    return False


def install_stdio(script_path: Path | None = None, config_path: Path = GEMINI_MCP_CONFIG) -> None:
    target = (script_path or Path(__file__)).resolve()
    entry = {
        'command': preferred_python(),
        'args': [str(target), '--stdio'],
        'env': {'PYTHONUTF8': '1'},
    }
    update_gemini_mcp_config(entry, config_path=config_path)
    print(f'Registered {MCP_NAME} (stdio) in {config_path}')
    print(f'  command: {entry["command"]} {" ".join(entry["args"])}')


def install_http(
    port: int = DEFAULT_PORT,
    token_file: Path = DEFAULT_TOKEN_FILE,
    pid_file: Path = DEFAULT_PID_FILE,
    script_path: Path | None = None,
    config_path: Path = GEMINI_MCP_CONFIG,
) -> None:
    info = desktop_info()
    if not info['desktop_ok']:
        sys.exit(
            f"Refusing install-http from window_station={info['window_station']!r}, "
            f"desktop={info['desktop']!r} (session={info['session_id']}).\n"
            f"Run this once directly in a normal Windows PowerShell on your desktop (WinSta0\\Default):\n"
            f"  python \"{Path(__file__).resolve()}\" install-http"
        )
    target = (script_path or Path(__file__)).resolve()
    token = ensure_token(token_file)
    stop_recorded_daemon(pid_file)

    py = preferred_python()
    pyw = Path(py).with_name('pythonw.exe')
    daemon_exe = str(pyw) if pyw.is_file() else py
    daemon_args = [str(target), '--http', '--host', '127.0.0.1', '--port', str(port),
                   '--token-file', str(token_file), '--pid-file', str(pid_file)]

    import winreg
    arg_str = ' '.join(f'"{a}"' if ' ' in a else a for a in daemon_args)
    run_cmd = f'"{daemon_exe}" {arg_str}'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
        winreg.SetValueEx(key, TASK_NAME, 0, winreg.REG_SZ, run_cmd)

    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'DETACHED_PROCESS', 0)
    subprocess.Popen([daemon_exe, *daemon_args], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)

    health: dict[str, Any] | None = None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(25):
        try:
            with opener.open(f'http://127.0.0.1:{port}/health', timeout=1) as resp:
                health = json.loads(resp.read().decode('utf-8'))
                if health.get('ok'):
                    break
        except Exception:
            time.sleep(0.2)
    if not health or not health.get('desktop_ok'):
        sys.exit(f'Daemon did not report desktop_ok on http://127.0.0.1:{port}/health: {health}')

    entry = {
        'url': f'http://127.0.0.1:{port}/mcp',
        'headers': {'Authorization': f'Bearer {token}'},
        'timeoutSeconds': 300,
    }
    update_gemini_mcp_config(entry, config_path=config_path)
    restrict_file_acl(config_path)
    print(f'Installed {MCP_NAME} (Streamable HTTP) on http://127.0.0.1:{port}/mcp')
    print(f'  desktop: {health["window_station"]}\\{health["desktop"]} (session {health["session_id"]}, pid {health["pid"]})')
    print(f'  autostart: HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\{TASK_NAME}')
    print(f'  MCP config: {config_path} (token stored in {token_file}, never printed)')


def uninstall_http(pid_file: Path = DEFAULT_PID_FILE, config_path: Path = GEMINI_MCP_CONFIG) -> None:
    stop_recorded_daemon(pid_file)
    pid_file.unlink(missing_ok=True)
    if os.name == 'nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run', 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, TASK_NAME)
        except OSError:
            pass
    update_gemini_mcp_config(None, config_path=config_path)
    print(f'Removed {MCP_NAME} from {config_path} and deleted autostart entry {TASK_NAME}.')


def main(argv: list[str] | None = None) -> None:
    os.environ.setdefault('PYTHONUTF8', '1')
    for stream, kwargs in (
        (sys.stdin, {'encoding': 'utf-8', 'errors': 'replace'}),
        (sys.stdout, {'encoding': 'utf-8', 'newline': '\n'}),
    ):
        try:
            stream.reconfigure(**kwargs)
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--stdio', action='store_true', help='Run MCP server over stdin/stdout')
    mode.add_argument('--http', action='store_true', help='Run MCP server over Streamable HTTP (loopback only)')
    ap.add_argument('command', nargs='?', choices=('status', 'install-stdio', 'install-http', 'uninstall-http'),
                    help='Setup or diagnostic subcommand')
    ap.add_argument('--host', default='127.0.0.1', help='Loopback host for --http (default 127.0.0.1)')
    ap.add_argument('--port', type=int, default=DEFAULT_PORT, help=f'Loopback port for --http / install-http (default {DEFAULT_PORT})')
    ap.add_argument('--token-file', type=Path, default=DEFAULT_TOKEN_FILE, help='Bearer token file for --http')
    ap.add_argument('--pid-file', type=Path, default=DEFAULT_PID_FILE, help='PID file for --http daemon')
    args = ap.parse_args(argv)

    if args.stdio:
        run_stdio(McpDispatcher(JobManager(transport='stdio')))
        return

    if args.http:
        if not args.token_file.is_file():
            sys.exit(f'token file not found: {args.token_file}')
        token = args.token_file.read_text(encoding='utf-8').strip()
        manager = JobManager(transport=f'http://{args.host}:{args.port}/mcp')
        # Bind the socket BEFORE touching pid_file so a duplicate start never clobbers a live daemon's PID record.
        server = make_http_server(McpDispatcher(manager), args.host, args.port, token)
        write_pid_record(args.pid_file)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            args.pid_file.unlink(missing_ok=True)
        return

    if args.command == 'status':
        print(json.dumps(JobManager(transport='cli').status(), indent=2))
    elif args.command == 'install-stdio':
        install_stdio()
    elif args.command == 'install-http':
        install_http(port=args.port, token_file=args.token_file, pid_file=args.pid_file)
    elif args.command == 'uninstall-http':
        uninstall_http(pid_file=args.pid_file)
    else:
        ap.print_help()
        sys.exit(2)


if __name__ == '__main__':
    main()
