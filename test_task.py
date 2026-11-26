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
        spec = importlib.util.spec_from_file_location('task_caches', TOOL)
        task = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(task)
        remove = task.remove

        def locked(path):
            if Path(path) == first / 'debug':
                raise PermissionError('in use')
            remove(path)
        out = io.StringIO()
        with patch.dict(os.environ, self.env), patch.object(task, 'remove', locked), contextlib.redirect_stdout(out):
            task.caches(task.settings(), 3, True, False)
        self.assertIn('1 failed', out.getvalue())
        self.assertFalse(second.exists())
        self.assertTrue((first / 'CACHEDIR.TAG').exists())


if __name__ == '__main__':
    unittest.main()
