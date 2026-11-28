import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'claude' / 'tools'))
import hw_mcp


def _fake_repo(tmp: Path) -> Path:
    repo = tmp / 'rubylight-fake'
    (repo / 'rust' / 'release').mkdir(parents=True)
    (repo / 'tools' / 'hw').mkdir(parents=True)
    (repo / 'rust' / 'release' / 'check.ps1').write_text(
        'param([string]$Version = "6.5.0", [string]$Work, [switch]$NoInstall, [switch]$NoUpload, [string[]]$SkipStreams)\n'
        '$bare = $Version -replace "^v", ""\n'
        '$dir = Join-Path $Work $bare\n'
        'New-Item -ItemType Directory -Force $dir | Out-Null\n'
        'if ($SkipStreams -contains "h264") { exit 1 }\n'
        '@{ version = $bare; passed = $true } | ConvertTo-Json | Set-Content (Join-Path $dir "VALIDATION.json") -Encoding utf8\n'
        'exit 0\n',
        encoding='utf-8',
    )
    (repo / 'rust' / 'release' / 'smoke.ps1').write_text('# stub\n', encoding='utf-8')
    (repo / 'tools' / 'hw' / 'probe-on-new-display.ps1').write_text('# stub\n', encoding='utf-8')
    (repo / 'tools' / 'hw' / 'run.py').write_text(
        'import sys, time\n'
        'if "sleep" in sys.argv:\n'
        '    time.sleep(30)\n'
        'print("stub hw batch", " ".join(sys.argv[1:]))\n'
        'sys.exit(3 if "hang" in sys.argv else 0)\n',
        encoding='utf-8',
    )
    return repo


def _default_desktop():
    return {'window_station': 'WinSta0', 'desktop': 'Default', 'session_id': 1, 'desktop_ok': True, 'pid': 42}


class HwMcpTests(unittest.TestCase):
    def test_validation_rejects_bad_versions_repos_vars_only_and_types(self):
        with self.assertRaises(ValueError):
            hw_mcp.validate_version('v6.5.0; whoami')
        with self.assertRaises(ValueError):
            hw_mcp.validate_version('', required=True)
        self.assertEqual(hw_mcp.validate_version('v6.5.0-rc.463'), 'v6.5.0-rc.463')
        self.assertEqual(hw_mcp.validate_version('6.5.0'), '6.5.0')

        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            repo = _fake_repo(tmp)
            self.assertEqual(
                hw_mcp.validate_repo(str(repo), 'tools/hw/run.py', default_repo=repo, allowed_roots=(tmp,)),
                repo.resolve(),
            )
            # Root itself (e.g. C:\src) must be rejected; only subdirectories inside allowed roots pass.
            with self.assertRaises(ValueError):
                hw_mcp.validate_repo(str(tmp), 'tools/hw/run.py', default_repo=repo, allowed_roots=(tmp,))
            with self.assertRaises(ValueError):
                hw_mcp.validate_repo(str(repo), 'tools/hw/run.py', default_repo=repo, allowed_roots=(tmp / 'other',))
            with self.assertRaises(ValueError):
                hw_mcp.validate_repo(str(repo), '../../etc/passwd', default_repo=repo, allowed_roots=(tmp,))

            # Symlinked repo or script must be rejected before .resolve() dereferences it.
            link_repo = tmp / 'linked-repo'
            try:
                os.symlink(repo, link_repo, target_is_directory=True)
            except OSError:
                pass
            else:
                with self.assertRaises(ValueError):
                    hw_mcp.validate_repo(str(link_repo), 'tools/hw/run.py', default_repo=repo, allowed_roots=(tmp,))

            # vars allowlist and injection defense.
            self.assertEqual(
                hw_mcp.validate_hw_vars({'release': 'v6.5.0', 'repo': str(repo)}, repo, (tmp,)),
                {'release': '6.5.0', 'repo': str(repo.resolve())},
            )
            for bad_vars in (
                {'python': 'calc.exe; #'},
                {'catalog': 'evil.yaml'},
                {'release': '1.0.0; whoami'},
                {'release': 'not-a-version'},
                'not-a-dict',
            ):
                with self.assertRaises(ValueError):
                    hw_mcp.validate_hw_vars(bad_vars, repo, (tmp,))

            # Strict MCP type checks & leading-dash / bare-string `only` rejection.
            mgr = hw_mcp.JobManager(
                work_root=tmp / 'work', default_repo=repo, allowed_repo_roots=(tmp,),
                desktop_probe=_default_desktop,
                host_probe=lambda: {'reachable': True, 'idle': True, 'version': '6.5.0', 'state': 'SUNSHINE_SERVER_FREE'},
                lock_probe=lambda _w: {},
            )
            disp = hw_mcp.McpDispatcher(mgr)
            for bad_call in (
                {'name': 'start_hw_batch', 'arguments': {'batch': 'desktop', 'only': ['--catalog=evil.yaml'], 'repo': str(repo)}},
                {'name': 'start_hw_batch', 'arguments': {'batch': 'desktop', 'only': 'e2e-hevc', 'repo': str(repo)}},
                {'name': 'start_hw_batch', 'arguments': {'batch': 'desktop', 'dry_run': 'false', 'repo': str(repo)}},
                {'name': 'start_release_check', 'arguments': {'no_install': 'false', 'repo': str(repo)}},
                {'name': 'job_status', 'arguments': {'wait_seconds': True}},
                {'name': 'hw_status', 'arguments': {'unexpected': 1}},
            ):
                res = disp.handle_message({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': bad_call})
                self.assertTrue(res['result']['isError'], f'expected error for {bad_call}')

    def test_exebox_and_non_winsta0_refuse_desktop_jobs(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            repo = _fake_repo(tmp)
            for bad_desktop in (
                {'window_station': 'WinSta0', 'desktop': 'exebox-abc123', 'session_id': 1, 'desktop_ok': False, 'pid': 99},
                {'window_station': 'SandboxWinSta', 'desktop': 'Default', 'session_id': 1, 'desktop_ok': False, 'pid': 99},
            ):
                mgr = hw_mcp.JobManager(
                    work_root=tmp / 'work', default_repo=repo, allowed_repo_roots=(tmp,),
                    desktop_probe=lambda bd=bad_desktop: bd,
                    host_probe=lambda: {'reachable': True, 'idle': True, 'version': '6.5.0', 'state': 'SUNSHINE_SERVER_FREE'},
                    lock_probe=lambda _w: {},
                )
                disp = hw_mcp.McpDispatcher(mgr)
                res = disp.handle_message({
                    'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                    'params': {'name': 'start_release_check', 'arguments': {'version': 'v6.5.0', 'repo': str(repo)}},
                })
                self.assertTrue(res['result']['isError'])
                payload = json.loads(res['result']['content'][0]['text'])
                self.assertIn('install-http', payload['error'])

    def test_stdio_protocol_batch_utf8_cancel_and_exit_code_3(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            repo = _fake_repo(tmp)
            mgr = hw_mcp.JobManager(
                work_root=tmp / 'work', default_repo=repo, allowed_repo_roots=(tmp,),
                desktop_probe=_default_desktop,
                host_probe=lambda: {'reachable': True, 'idle': True, 'version': '6.5.0', 'state': 'SUNSHINE_SERVER_FREE'},
                lock_probe=lambda _w: {'GPU-LOCK': 'test'},
            )
            disp = hw_mcp.McpDispatcher(mgr)

            requests_in = '\n'.join([
                json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}}),
                json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}),
                # JSON-RPC 2.0 batch with UTF-8 non-ASCII characters (Ü, —) + notification.
                json.dumps([
                    {'jsonrpc': '2.0', 'id': 'b1', 'method': 'ping'},
                    {'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'reason': 'Übung — ok'}},
                    {'jsonrpc': '2.0', 'id': 'b2', 'method': 'tools/list'},
                ]),
                json.dumps({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'hw_status', 'arguments': {}}}),
                json.dumps({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
                            'params': {'name': 'start_hw_batch', 'arguments': {'batch': 'desktop', 'only': ['ok-case'], 'repo': str(repo)}}}),
            ]) + '\n'
            out = io.StringIO()
            hw_mcp.run_stdio(disp, stdin=io.StringIO(requests_in), stdout=out)
            lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
            self.assertEqual(len(lines), 4)
            self.assertEqual(lines[0]['result']['protocolVersion'], '2025-03-26')
            self.assertIsInstance(lines[1], list)
            self.assertEqual([item['id'] for item in lines[1]], ['b1', 'b2'])

            started = json.loads(lines[3]['result']['content'][0]['text'])
            done = mgr.job_status(started['job_id'], wait_seconds=15)
            self.assertEqual(done['status'], 'passed')
            self.assertEqual(done['exit_code'], 0)

            # Exit code 3 must be flagged as gpu_hang.
            hang_start = mgr.start_hw_batch(batch='desktop', only=['hang'], repo=str(repo))
            hang_done = mgr.job_status(hang_start['job_id'], wait_seconds=15)
            self.assertEqual(hang_done['status'], 'gpu_hang')
            self.assertEqual(hang_done['exit_code'], 3)
            self.assertTrue(hang_done['gpu_hang'])
            self.assertIn('NEVER retry', hang_done['warning'])

            # Cancellation must terminate process and wait until runner marks job cancelled before allowing another job.
            slp = mgr.start_hw_batch(batch='desktop', only=['sleep'], repo=str(repo))
            time.sleep(0.2)
            cancelled = mgr.job_cancel(slp['job_id'])
            self.assertTrue(cancelled['cancelled'])
            self.assertEqual(cancelled['status'], 'cancelled')
            # A subsequent job must succeed immediately after cancel completes.
            next_job = mgr.start_hw_batch(batch='desktop', only=['ok-after-cancel'], repo=str(repo))
            next_done = mgr.job_status(next_job['job_id'], wait_seconds=15)
            self.assertEqual(next_done['status'], 'passed')

    def test_release_check_validation_json_fresh_omitted_and_stale(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            repo = _fake_repo(tmp)
            work = tmp / 'work'
            stale_dir = work / '6.5.0'
            stale_dir.mkdir(parents=True)
            stale_val = stale_dir / 'VALIDATION.json'
            stale_val.write_text(json.dumps({'version': '6.5.0', 'stale': True}), encoding='utf-8')
            old_ts = time.time() - 120
            os.utime(stale_val, (old_ts, old_ts))

            mgr = hw_mcp.JobManager(
                work_root=work, default_repo=repo, allowed_repo_roots=(tmp,),
                desktop_probe=_default_desktop,
                host_probe=lambda: {'reachable': True, 'idle': True, 'version': '6.5.0', 'state': 'SUNSHINE_SERVER_FREE'},
                lock_probe=lambda _w: {},
            )
            # Early failure must NOT attach the stale VALIDATION.json.
            early = mgr.start_release_check(version='v6.5.0', skip_streams=['h264'], repo=str(repo))
            early_done = mgr.job_status(early['job_id'], wait_seconds=20)
            self.assertEqual(early_done['status'], 'failed')
            self.assertNotIn('validation', early_done['artifacts'])

            # Omitting version must still discover and attach the freshly written VALIDATION.json.
            fresh = mgr.start_release_check(version=None, no_install=True, no_upload=True, repo=str(repo))
            fresh_done = mgr.job_status(fresh['job_id'], wait_seconds=20)
            self.assertEqual(fresh_done['status'], 'passed')
            self.assertEqual(fresh_done['artifacts']['validation']['version'], '6.5.0')

    def test_streamable_http_ipv4_ipv6_origin_session_and_batch(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            repo = _fake_repo(tmp)
            mgr = hw_mcp.JobManager(
                work_root=tmp / 'work', default_repo=repo, allowed_repo_roots=(tmp,),
                desktop_probe=_default_desktop,
                host_probe=lambda: {'reachable': True, 'idle': True, 'version': '6.5.0', 'state': 'SUNSHINE_SERVER_FREE'},
                lock_probe=lambda _w: {},
                transport='http',
            )
            disp = hw_mcp.McpDispatcher(mgr)
            token = 'a' * 32

            with self.assertRaises(ValueError):
                hw_mcp.make_http_server(disp, '0.0.0.0', 0, token)

            # Verify IPv6 ::1 binding works without gaierror.
            srv6 = hw_mcp.make_http_server(disp, '::1', 0, token)
            srv6.server_close()

            srv = hw_mcp.make_http_server(disp, '127.0.0.1', 0, token)
            port = srv.server_address[1]
            # Duplicate bind on the same loopback port must fail (SO_EXCLUSIVEADDRUSE on Windows).
            with self.assertRaises(OSError):
                dup = hw_mcp.make_http_server(disp, '127.0.0.1', port, token)
                dup.server_close()
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                # Non-loopback Origin must be rejected with 403 even on /health.
                bad_origin = urllib.request.Request(f'http://127.0.0.1:{port}/health', headers={'Origin': 'https://evil.example'})
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    opener.open(bad_origin, timeout=3)
                self.assertEqual(ctx.exception.code, 403)

                # Missing token -> 401.
                no_auth = urllib.request.Request(
                    f'http://127.0.0.1:{port}/mcp',
                    data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
                    headers={'Content-Type': 'application/json'}, method='POST',
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    opener.open(no_auth, timeout=3)
                self.assertEqual(ctx.exception.code, 401)

                # Initialize -> returns Mcp-Session-Id.
                init_req = urllib.request.Request(
                    f'http://127.0.0.1:{port}/mcp',
                    data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}}).encode('utf-8'),
                    headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'}, method='POST',
                )
                with opener.open(init_req, timeout=3) as r:
                    self.assertEqual(r.status, 200)
                    sid = r.headers.get('Mcp-Session-Id')
                    self.assertTrue(sid)

                # DELETE /mcp invalidates the session -> subsequent use returns 404.
                del_req = urllib.request.Request(
                    f'http://127.0.0.1:{port}/mcp',
                    headers={'Authorization': f'Bearer {token}', 'Mcp-Session-Id': sid}, method='DELETE',
                )
                with opener.open(del_req, timeout=3) as r:
                    self.assertEqual(r.status, 204)

                dead_req = urllib.request.Request(
                    f'http://127.0.0.1:{port}/mcp',
                    data=json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'ping'}).encode('utf-8'),
                    headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}', 'Mcp-Session-Id': sid},
                    method='POST',
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    opener.open(dead_req, timeout=3)
                self.assertEqual(ctx.exception.code, 404)

                # Batch notification-only -> 202 Accepted.
                batch_notif = urllib.request.Request(
                    f'http://127.0.0.1:{port}/mcp',
                    data=json.dumps([{'jsonrpc': '2.0', 'method': 'notifications/initialized'}]).encode('utf-8'),
                    headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'}, method='POST',
                )
                with opener.open(batch_notif, timeout=3) as r:
                    self.assertEqual(r.status, 202)
            finally:
                srv.shutdown()
                srv.server_close()

    def test_stale_priority_ignored_and_mcp_config_merge(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            (tmp / 'PRIORITY').write_text('release check.ps1 pid=999999\n', encoding='utf-8')
            self.assertNotIn('PRIORITY', hw_mcp.probe_locks(tmp, alive_fn=lambda _p: False))
            self.assertIn('PRIORITY', hw_mcp.probe_locks(tmp, alive_fn=lambda _p: True))

            # cleanup_cancelled_locks removes only locks owned by killed dead PIDs, never unrelated locks.
            (tmp / 'GPU-LOCK').write_text('run.py pid=12345\n', encoding='utf-8')
            (tmp / 'RELEASE-LOCK').write_text('check.ps1 pid=77777\n', encoding='utf-8')
            hw_mcp.cleanup_cancelled_locks(tmp, {12345}, alive_fn=lambda _p: False)
            self.assertFalse((tmp / 'GPU-LOCK').exists())
            self.assertTrue((tmp / 'RELEASE-LOCK').exists())

            cfg = tmp / 'mcp_config.json'
            cfg.write_text(json.dumps({'mcpServers': {'other': {'command': 'echo'}}}), encoding='utf-8')
            hw_mcp.install_stdio(script_path=tmp / 'hw_mcp.py', config_path=cfg)
            data = json.loads(cfg.read_text(encoding='utf-8'))
            self.assertIn('other', data['mcpServers'])
            self.assertEqual(data['mcpServers']['rubylight-hw']['env'], {'PYTHONUTF8': '1'})
            hw_mcp.update_gemini_mcp_config(None, config_path=cfg)
            data2 = json.loads(cfg.read_text(encoding='utf-8'))
            self.assertIn('other', data2['mcpServers'])
            self.assertNotIn('rubylight-hw', data2['mcpServers'])


if __name__ == '__main__':
    unittest.main()
