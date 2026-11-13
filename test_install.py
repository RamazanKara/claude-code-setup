import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import install


class InstallerTests(unittest.TestCase):
    def test_jsonc_strings_comments_and_trailing_commas(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.jsonc'
            path.write_text('''{
                // comment
                "url": "https://example.com/*literal*/",
                "quoted": "escaped \\" // literal",
                "items": ["comma,}", /* block */ 1,],
            }''', encoding='utf-8')
            self.assertEqual(install.read_json(path), {
                'url': 'https://example.com/*literal*/',
                'quoted': 'escaped " // literal', 'items': ['comma,}', 1]})

    def test_permissions_preserve_allow_and_ask(self):
        config = {'permissions': {'allow': ['Read'], 'deny': ['Bash']},
                  'permission': {'shell': {'*': 'ask', 'blocked': 'deny'}}}
        install.clean_permissions(config)
        self.assertEqual(config, {'permissions': {'allow': ['Read']},
                                  'permission': {'shell': {'*': 'ask'}}})
        config = {'permission': 'deny'}
        install.clean_permissions(config)
        self.assertEqual(config, {})

    def test_opencode_preserves_custom_agent_properties(self):
        config = {'agents': {'explore': {'model': 'opencode-go/deepseek-v4.1-flash', 'custom': True},
                             'general': {'disabled': True, 'description': 'mine'},
                             'custom': {'model': 'user/model'}},
                  'permissions': [{'effect': 'deny', 'action': 'shell'},
                                  {'effect': 'allow', 'action': 'read'}]}
        install.clean_opencode(config)
        self.assertEqual(config['agents'], {'explore': {'custom': True},
                                           'general': {'description': 'mine'},
                                           'custom': {'model': 'user/model'}})
        self.assertEqual(config['permissions'], [{'effect': 'allow', 'action': 'read'}])

    def test_install_migration_backups_dry_run_and_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            home, opencode = root / 'claude', root / 'opencode'
            home.mkdir()
            opencode.mkdir()
            shared = {name: root / name / 'AGENTS.md' for name in ('codex', 'antigravity', 'other')}
            for dst in shared.values():
                dst.parent.mkdir()
            other = {'hooks': [{'type': 'command', 'command': 'other-hook'}]}
            stale = {'hooks': [{'type': 'command', 'command': 'old-python', 'args': ['old/read_streak.py']}]}
            original = {'hooks': {'Stop': ['keep'], 'PostToolBatch': [stale, other]},
                        'permissions': {'deny': ['blocked'], 'allow': ['Read']},
                        'skillOverrides': {'docs': 'off', 'custom': 'on'}}
            (home / 'settings.json').write_text(json.dumps(original), encoding='utf-8')
            (home / 'settings.local.json').write_text('{"permissions":{"deny":["local"]}}', encoding='utf-8')
            (opencode / 'opencode.jsonc').write_text(
                '// legacy\n{"agents":{"general":{"disabled":true}},'
                '"permissions":[{"effect":"deny","action":"shell"}],}', encoding='utf-8')
            agents = home / 'agents'
            agents.mkdir()
            (agents / 'old.md').write_text('retired\n', encoding='utf-8')
            (agents / 'custom.md').write_text('user replacement\n', encoding='utf-8')
            retired = dict.fromkeys(('claude/old.md', 'claude/custom.md'),
                                    hashlib.sha256(b'retired\n').hexdigest())
            with patch.multiple(install, HOME=home, OPENCODE=opencode, SHARED=shared, RETIRED_AGENTS=retired):
                dry = install.Installer(True)
                dry.run()
                self.assertEqual(install.read_json(home / 'settings.json'), original)
                self.assertTrue((agents / 'old.md').exists())
                self.assertFalse((home / 'CLAUDE.md').exists())
                self.assertFalse((agents / 'scout.md').exists())
                self.assertFalse((home / 'hooks').exists())
                first = install.Installer(False)
                first.run()
                settings = install.read_json(home / 'settings.json')
                self.assertEqual(settings['hooks']['Stop'], ['keep'])
                script = home / 'hooks' / 'read_streak.py'
                self.assertEqual(script.read_bytes(), (install.REPO / 'hooks' / script.name).read_bytes())
                batch = settings['hooks']['PostToolBatch']
                self.assertEqual(batch[0], other)
                self.assertEqual([g['hooks'][0]['args'] for g in batch[1:]], [[script.as_posix()]])
                self.assertEqual(settings['permissions'], {'allow': ['Read']})
                self.assertEqual(settings['skillOverrides'], {'custom': 'on'})
                self.assertEqual(install.read_json(first.backup / 'settings.json'), original)
                self.assertFalse((agents / 'old.md').exists())
                self.assertTrue((first.backup / 'claude-agents/old.md').exists())
                self.assertTrue((agents / 'custom.md').exists())
                for harness, directory, suffix in (
                    ('claude', home / 'agents', '.md'),
                    ('opencode', opencode / 'agents', '.md'),
                    ('codex', root / 'codex' / 'agents', '.toml'),
                    ('antigravity', root / 'antigravity' / 'config' / 'agents', '.md'),
                ):
                    for name in ('scout', 'runner', 'mechanic'):
                        dst = directory / (name + suffix)
                        self.assertEqual(dst.read_bytes(),
                                         (install.ROOT / harness / 'agents' / dst.name).read_bytes())
                        self.assertNotIn('effect: deny', dst.read_text(encoding='utf-8'))
                self.assertFalse((root / 'other' / 'agents').exists())
                self.assertEqual(install.read_json(home / 'settings.local.json'), {})
                self.assertNotIn('agents', install.read_json(opencode / 'opencode.jsonc'))
                self.assertNotIn('permissions', install.read_json(opencode / 'opencode.jsonc'))
                flash = install.read_json(opencode / 'opencode.jsonc')['providers']['opencode-go']['models']
                self.assertEqual(flash['deepseek-v4.1-flash']['body'], {'reasoning_effort': 'max'})
                self.assertEqual(next(iter(shared.values())).read_text(encoding='utf-8'),
                                 install.shared_rules(home / 'personal.md'))
                second = install.Installer(False)
                second.run()
                self.assertFalse(second.saved)
                self.assertFalse(second.backup.exists())


class ReadStreakHookTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env = {**os.environ, 'TMPDIR': tmp.name, 'TEMP': tmp.name, 'TMP': tmp.name}
        self.env.pop('CLAUDE_READ_STREAK', None)

    def turn(self, *calls, **event):
        event = {'session_id': 's1', 'hook_event_name': 'PostToolBatch', **event,
                 'tool_calls': [{'tool_name': name, 'tool_input': {'command': command}} for name, command in calls]}
        out = subprocess.run([sys.executable, str(install.REPO / 'hooks' / 'read_streak.py')], input=json.dumps(event),
                             capture_output=True, text=True, env=self.env, check=True).stdout
        return json.loads(out)['hookSpecificOutput']['additionalContext'] if out else None

    def test_nudges_at_the_third_single_read_turn_and_every_sixth_after(self):
        notes = [self.turn(('Read', '')) for _ in range(9)]
        self.assertEqual([i + 1 for i, note in enumerate(notes) if note], [3, 9])
        self.assertIn('3 read-only turns', notes[2])
        self.assertIn('DeepSeek', notes[2])

    def test_a_batched_or_acting_turn_resets_the_streak(self):
        resets = ((('Read', ''), ('Grep', '')), (('Edit', ''),), (('PowerShell', 'cargo test | tail -5'),))
        for n, reset in enumerate(resets):
            session = {'session_id': f'reset{n}'}
            self.assertIsNone(self.turn(('Grep', ''), **session))
            self.assertIsNone(self.turn(('PowerShell', 'Get-Content a.rs | Select-Object -First 5'), **session))
            self.assertIsNone(self.turn(*reset, **session))
            self.assertIsNone(self.turn(('Bash', 'git log --oneline -5'), **session))
            self.assertIsNone(self.turn(('Glob', ''), **session))
            self.assertIsNotNone(self.turn(('Read', ''), **session))

    def test_a_command_name_inside_a_path_is_not_a_call(self):
        reads = (r'Get-Content C:\Users\me\orca\workspaces\python\node\a.rs -TotalCount 5', r'cat ~/orca/gh/npm/a.rs',
                 'git log --oneline -5', 'get-childitem src | select-object -first 5')
        acts = ('orca orchestration worker-list', r'$o = python tools\x.py; $o | Select-Object -First 5',
                r'.\gradlew test | tail -5', 'git status; git push origin HEAD:main', r'tools\dev.ps1 check | tail -3',
                'Get-Content a.txt | Set-Content b.txt')
        for n, command in enumerate(reads + acts):
            session = {'session_id': f'path{n}'}
            notes = [self.turn(('PowerShell', command), **session) for _ in range(3)]
            self.assertEqual(bool(notes[2]), command in reads, command)

    def test_streaks_are_per_agent_and_helpers_are_skipped(self):
        for _ in range(2):
            self.assertIsNone(self.turn(('Read', '')))
            self.assertIsNone(self.turn(('Read', ''), agent_id='a1', agent_type='general-purpose'))
        for _ in range(4):
            self.assertIsNone(self.turn(('Read', ''), agent_id='a2', agent_type='scout'))
        note = self.turn(('Read', ''), agent_id='a1', agent_type='general-purpose')
        self.assertIn('`scout`', note)
        self.assertNotIn('DeepSeek', note)

    def test_zero_turns_it_off_and_bad_input_stays_silent(self):
        self.env['CLAUDE_READ_STREAK'] = '0'
        self.assertEqual([self.turn(('Read', '')) for _ in range(4)], [None] * 4)
        done = subprocess.run([sys.executable, str(install.REPO / 'hooks' / 'read_streak.py')], input='not json',
                              capture_output=True, text=True, env=self.env)
        self.assertEqual((done.returncode, done.stdout), (0, ''))


if __name__ == '__main__':
    unittest.main()
