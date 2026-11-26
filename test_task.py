import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import install

TOOL = install.REPO / 'tools' / 'task.py'
TAG = 'Signature: 8a477f597d28d172789f06886806bc55\n# This file is a cache directory tag created by cargo.\n'


class CachesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.artifacts, self.tests = self.root / 'artifacts', self.root / 'tests'
        (self.root / 'machine.json').write_text(json.dumps(
            {'artifacts': str(self.artifacts), 'roots': [str(self.tests)], 'keep': ['held']}), encoding='utf-8')
        self.env = {**os.environ, 'CLAUDE_MACHINE_FILE': str(self.root / 'machine.json')}

    def target(self, path, days=10, tagged=True):
        (path / 'debug' / '.fingerprint').mkdir(parents=True)
        (path / 'debug' / 'app.exe').write_bytes(b'x' * 1000)
        if tagged:
            (path / 'CACHEDIR.TAG').write_text(TAG, encoding='utf-8')
            (path / '.rustc_info.json').write_text('{}', encoding='utf-8')
        self.age(path, days)
        return path

    def age(self, path, days):
        for item in (path, *path.rglob('*')):
            os.utime(item, (time.time() - days * 86400,) * 2)

    def tool(self, *args):
        done = subprocess.run([sys.executable, str(TOOL), 'caches', *args], capture_output=True, text=True, env=self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def listed(self, *args):
        return sorted(Path(row['path']).relative_to(self.root).as_posix() for row in json.loads(self.tool('--json', *args))['targets'])

    def test_it_finds_targets_and_leaves_out_what_must_stay(self):
        self.target(self.artifacts / 'a' / 'target')
        self.target(self.artifacts / 'b' / 'x' / 'y' / 'custom', tagged=False)
        self.target(self.tests / 'c-target')
        self.target(self.artifacts / 'held' / 'target')
        self.target(self.artifacts / 'rl-target-pool' / '1')
        self.target(self.artifacts / 'gate' / 'slot')
        (self.artifacts / 'gate' / 'slot.lock').write_text('', encoding='utf-8')
        self.target(self.artifacts / 'new' / 'target', days=1)
        (self.artifacts / 'other' / 'debug').mkdir(parents=True)  # no Cargo output
        self.assertEqual(self.listed(), ['artifacts/a/target', 'artifacts/b/x/y/custom', 'tests/c-target'])
        self.assertIn('artifacts/new/target', self.listed('--days', '0.5'))
        self.assertEqual(self.listed('--days', '30'), [])

    def test_a_folder_that_also_holds_evidence_is_no_target(self):
        mixed = self.target(self.artifacts / 'ab', tagged=False)
        (mixed / '.rustc_info.json').write_text('{}', encoding='utf-8')
        (mixed / 'build.log').write_text('evidence', encoding='utf-8')
        self.age(mixed, 10)
        self.assertEqual(self.listed(), [])
        (mixed / 'build.log').unlink()
        (mixed / 'runs').mkdir()
        self.age(mixed, 10)
        self.assertEqual(self.listed(), [])

    def skipped(self, *args):
        return {Path(row['path']).relative_to(self.root).as_posix(): row['why'] for row in json.loads(self.tool('--json', *args))['skipped']}

    def module(self):
        spec = importlib.util.spec_from_file_location('task_caches', TOOL)
        task = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(task)
        return task

    def test_a_tagged_folder_with_other_content_or_a_checkout_is_skipped(self):
        logs, tree = self.target(self.artifacts / 'a' / 'target'), self.target(self.artifacts / 'b' / 'target')
        (logs / 'build.log').write_text('evidence', encoding='utf-8')
        (tree / 'debug' / 'build' / 'dep' / '.git').mkdir(parents=True)
        whole = self.target(self.artifacts / 'c' / 'target')
        self.target(whole / 'x86_64-pc-windows-msvc')
        self.target(whole / 'service')
        for name in ('doc', 'tmp', 'flycheck0', 'nextest'):
            (whole / name).mkdir()
        for path in (logs, tree, whole):
            self.age(path, 10)
        self.assertEqual(self.listed(), ['artifacts/c/target'])
        self.assertEqual(self.skipped(), {'artifacts/a/target': 'mixed', 'artifacts/b/target': 'holds a checkout'})
        self.tool('--apply')
        self.assertTrue((logs / 'build.log').exists() and (tree / 'debug' / 'app.exe').exists())
        self.assertFalse(whole.exists())

    def test_untagged_doc_package_and_tmp_must_look_like_cargos(self):
        target = self.target(self.artifacts / 'a', tagged=False)
        for name in ('doc/static.files', 'doc/app', 'package/app-1.2.3', 'tmp'):
            (target / name).mkdir(parents=True)
        (target / 'doc' / 'app' / 'index.html').write_text('', encoding='utf-8')
        (target / 'package' / 'app-1.2.3.crate').write_text('', encoding='utf-8')
        self.age(target, 10)
        self.assertEqual(self.listed(), ['artifacts/a'])
        (target / 'doc' / 'app' / 'sub').mkdir()
        (target / 'doc' / 'app' / 'sub' / 'page.html').write_text('', encoding='utf-8')
        self.age(target, 10)
        self.assertEqual(self.listed(), ['artifacts/a'])
        for name in ('doc/notes.md', 'package/setup.exe', 'tmp/run.log', 'doc/app/sub/trace.log'):
            (target / name).write_text('evidence', encoding='utf-8')
            self.age(target, 10)
            self.assertEqual(self.listed(), [], name)
            (target / name).unlink()

    def test_a_target_in_use_is_not_deleted(self):
        task = self.module()
        target = self.target(self.artifacts / 'a' / 'target')
        (target / 'debug' / '.cargo-lock').write_text('', encoding='utf-8')
        self.age(target, 10)
        held = task.lock(target / 'debug' / '.cargo-lock')
        try:
            self.assertIn('is locked', self.tool('--apply'))
        finally:
            held.close()
        self.assertTrue((target / 'debug' / 'app.exe').exists())
        with patch.dict(os.environ, self.env):
            row = task.targets(task.settings(), 3)[0][0]
        with patch.object(task, 'running', lambda path: True):
            self.assertIn('running', task.delete(row, 3))
        (target / 'debug' / 'examples').mkdir()
        (target / 'debug' / 'examples' / 'probe.exe').write_text('', encoding='utf-8')
        self.age(target, 10)
        with patch.object(task, 'running', lambda path: path.endswith('probe.exe')):
            self.assertIn('probe.exe', task.delete(row, 3))
        os.utime(target / 'debug')
        self.assertIn('no longer idle', task.delete(row, 3))
        self.assertTrue((target / 'debug' / 'app.exe').exists())
        self.age(target, 10)
        self.tool('--apply')
        self.assertFalse(target.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows refuses to open a running program')
    def test_a_running_program_is_recognised(self):
        task = self.module()
        self.assertTrue(task.running(sys.executable))
        idle = self.root / 'idle.exe'
        idle.write_bytes(b'x')
        self.assertFalse(task.running(str(idle)))

    def test_a_target_beside_a_checkout_in_use_stays(self):
        target = self.target(self.artifacts / 'work' / 'target')
        tree = self.artifacts / 'work' / 'src'
        (tree / '.git').mkdir(parents=True)
        self.assertEqual(self.listed(), [])
        self.age(tree, 10)
        self.assertEqual(self.listed(), [target.relative_to(self.root).as_posix()])

    def test_only_apply_deletes_and_only_the_targets(self):
        target = self.target(self.artifacts / 'a' / 'target')
        log = self.artifacts / 'a' / 'build.log'
        log.write_text('evidence', encoding='utf-8')
        said = self.tool()
        self.assertIn('dry run', said)
        self.assertIn('--apply', said)
        self.assertTrue((target / 'debug' / 'app.exe').exists())
        self.assertIn('0 failed', self.tool('--apply'))
        self.assertFalse(target.exists())
        self.assertEqual(log.read_text(encoding='utf-8'), 'evidence')

    def test_a_locked_file_does_not_stop_the_run_and_the_target_stays_findable(self):
        first, second = self.target(self.artifacts / 'a' / 'target'), self.target(self.artifacts / 'b' / 'target')
        task = self.module()
        remove = task.remove

        def locked(path):
            if Path(path) == first / 'debug' / 'app.exe':
                raise PermissionError('in use')
            remove(path)
        out = io.StringIO()
        with patch.dict(os.environ, self.env), patch.object(task, 'remove', locked), contextlib.redirect_stdout(out):
            task.caches(task.settings(), 3, True, False)
        self.assertIn('1 failed', out.getvalue())
        self.assertFalse(second.exists())
        self.assertTrue((first / 'CACHEDIR.TAG').exists() and (first / 'debug' / '.fingerprint').is_dir())
        self.assertIn('artifacts/a/target', self.listed('--days', '0'))


if __name__ == '__main__':
    unittest.main()
