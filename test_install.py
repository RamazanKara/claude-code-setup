import base64
import contextlib
import datetime
import hashlib
import importlib.util
import io
import json
import shutil
import os
import subprocess
import sys
import tempfile
import time
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

    def test_a_users_hook_that_shares_a_group_with_ours_is_kept(self):
        config = {'hooks': {'PreToolUse': [{'matcher': 'Bash|PowerShell', 'hooks': [
            {'type': 'command', 'command': 'python', 'args': ['old/review_gate.py', 'push']}, {'type': 'command', 'command': 'mine'}]}]}}
        install.set_hooks(config)
        groups = config['hooks']['PreToolUse']
        self.assertEqual([[hook['command'] for hook in group['hooks']] for group in groups][0], ['mine'])
        self.assertEqual((len(groups), groups[1]['hooks'][0]['args'][1]), (2, 'push'))

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
            original = {'hooks': {'Stop': ['keep'], 'PostToolBatch': [stale, other]}, 'env': {'KEEP': '1'},
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
                self.assertFalse((home / 'tools').exists())
                first = install.Installer(False)
                first.run()
                settings = install.read_json(home / 'settings.json')
                self.assertEqual(settings['hooks']['Stop'], ['keep'])
                script = home / 'hooks' / 'read_streak.py'
                self.assertEqual(script.read_bytes(), (install.REPO / 'hooks' / script.name).read_bytes())
                batch = settings['hooks']['PostToolBatch']
                self.assertEqual(batch[0], other)
                self.assertEqual([g['hooks'][0]['args'] for g in batch[1:]], [[script.as_posix()]])
                self.assertEqual(settings['env'], {'KEEP': '1', 'CLAUDE_CODE_SUBAGENT_MODEL': 'opus'})
                self.assertEqual(settings['advisorModel'], 'fable')
                gate = (home / 'hooks' / 'review_gate.py').as_posix()
                self.assertEqual([(g['matcher'], g['hooks'][0]['args']) for g in settings['hooks']['PreToolUse']],
                                 [('Bash|PowerShell', [gate, 'push'])])
                self.assertEqual([(g['matcher'], g['hooks'][0]['args']) for g in settings['hooks']['SubagentStop']],
                                 [('reviewer', [gate, 'reviewed'])])
                self.assertTrue((agents / 'reviewer.md').exists())
                for tool in ('deepseek.py', 'memory_sync.py', 'task.py'):
                    self.assertEqual((home / 'tools' / tool).read_bytes(), (install.REPO / 'tools' / tool).read_bytes())
                self.assertEqual([g['hooks'][0]['args'] for g in settings['hooks']['SessionStart']],
                                 [[(home / 'tools' / 'task.py').as_posix(), 'daily'],
                                  [(home / 'hooks' / 'fresh_checkout.py').as_posix()]])
                self.assertEqual(install.read_json(home / 'machine.json'), install.read_json(install.REPO / 'machine.example.json'))
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

    def test_the_deepseek_tool_needs_opencode(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            home = Path(tmp) / 'claude'
            home.mkdir()
            with patch.multiple(install, HOME=home, OPENCODE=Path(tmp) / 'none', SHARED={}, RETIRED_AGENTS={}):
                install.Installer(False).run()
            self.assertTrue((home / 'hooks' / 'read_streak.py').exists())
            self.assertEqual(sorted(tool.name for tool in (home / 'tools').iterdir()), ['land.py', 'memory_sync.py', 'task.py'])


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
        self.assertNotIn('DeepSeek worker', note)
        self.assertIn('/tools/deepseek.py" read', note)

    def test_zero_turns_it_off_and_bad_input_stays_silent(self):
        self.env['CLAUDE_READ_STREAK'] = '0'
        self.assertEqual([self.turn(('Read', '')) for _ in range(4)], [None] * 4)
        done = subprocess.run([sys.executable, str(install.REPO / 'hooks' / 'read_streak.py')], input='not json',
                              capture_output=True, text=True, env=self.env)
        self.assertEqual((done.returncode, done.stdout), (0, ''))


class FreshCheckoutTests(unittest.TestCase):
    HOOK = install.REPO / 'hooks' / 'fresh_checkout.py'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
                    'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}
        self.env.pop('CLAUDE_FRESH_CHECKOUT', None)
        self.sessions = self.root / 'config' / 'sessions'
        self.sessions.mkdir(parents=True)
        self.env['CLAUDE_CONFIG_DIR'] = str(self.root / 'config')
        self.env['USERPROFILE'] = self.env['HOME'] = str(self.root)  # a terminal session's ~/.claude/sessions
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(self.root / 'origin.git')], check=True, env=self.env)
        self.other, self.repo = self.root / 'other', self.root / 'work'
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(self.other)], check=True, env=self.env)
        self.git(self.other, 'remote', 'add', 'origin', str(self.root / 'origin.git'))
        self.land('AGENTS.md', 'old rule\n')
        subprocess.run(['git', 'clone', '-q', str(self.root / 'origin.git'), str(self.repo)], check=True, env=self.env)

    def git(self, where, *args):
        return subprocess.run(['git', '-C', str(where), *args], check=True, env=self.env, capture_output=True, text=True).stdout

    def land(self, name, text):
        """A commit someone else pushes to main."""
        (self.other / name).write_text(text, encoding='utf-8')
        self.git(self.other, 'add', '-A')
        self.git(self.other, 'commit', '-q', '-m', name)
        self.git(self.other, 'push', '-q', 'origin', 'HEAD:main')

    def hook(self, env=None, **event):
        done = subprocess.run([sys.executable, str(self.HOOK)], input=json.dumps({'cwd': str(self.repo), 'source': 'startup', **event}),
                              capture_output=True, text=True, env={**self.env, **(env or {})})
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        return json.loads(done.stdout)['hookSpecificOutput']['additionalContext'] if done.stdout else None

    def rule(self):
        return (self.repo / 'AGENTS.md').read_text(encoding='utf-8')

    def session(self, pid, cwd, sid='other', folder=None, **more):
        folder = folder or self.sessions
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f'{pid}.json').write_text(json.dumps({'pid': pid, 'cwd': str(cwd), 'sessionId': sid, **more}), encoding='utf-8')

    def test_a_clean_main_behind_is_fast_forwarded_and_untracked_files_stay(self):
        self.assertIsNone(self.hook())  # up to date: silent
        self.land('AGENTS.md', 'new rule\n')
        self.land('code.rs', 'fn main() {}\n')
        (self.repo / 'drafts.md').write_text('mine\n', encoding='utf-8')
        said = self.hook(cwd=str(self.repo / '.git' / '..'))
        self.assertIn('2 commits behind origin/main and was fast-forwarded', said)
        self.assertIn('Changed with it: AGENTS.md', said)
        self.assertEqual(self.rule(), 'new rule\n')
        self.assertTrue((self.repo / 'drafts.md').exists())
        self.assertIsNone(self.hook())

    def test_own_commits_edits_or_a_rebase_under_way_are_left_and_named(self):
        self.land('AGENTS.md', 'new rule\n')
        (self.repo / 'AGENTS.md').write_text('my edit\n', encoding='utf-8')
        said = self.hook()
        self.assertIn('1 commits behind origin/main and was left as it is because it has edits to tracked files', said)
        self.assertIn('git grep <text> origin/main', said)
        self.assertEqual(self.rule(), 'my edit\n')
        self.git(self.repo, 'commit', '-q', '-am', 'mine')
        said = self.hook()
        self.assertIn('because it holds commits of its own', said)
        self.assertIn('Rule files changed upstream: AGENTS.md', said)
        self.git(self.repo, 'reset', '-q', '--hard', 'HEAD~1')
        (Path(self.git(self.repo, 'rev-parse', '--absolute-git-dir').strip()) / 'rebase-merge').mkdir()
        self.assertIn('because a merge, rebase or cherry-pick is under way', self.hook())
        self.assertEqual(self.rule(), 'old rule\n')

    def test_its_own_rule_edit_is_not_reported_as_upstreams(self):
        (self.repo / 'CLAUDE.md').write_text('my rule\n', encoding='utf-8')
        self.git(self.repo, 'add', 'CLAUDE.md')
        self.git(self.repo, 'commit', '-q', '-m', 'my rule')
        self.land('code.rs', 'fn main() {}\n')
        said = self.hook()
        self.assertIn('because it holds commits of its own', said)
        self.assertNotIn('Rule files changed upstream', said)

    def test_another_live_session_in_the_checkout_keeps_it_still_and_names_the_new_rules(self):
        self.land('AGENTS.md', 'new rule\n')
        gone = subprocess.Popen([sys.executable, '-c', 'pass'])
        gone.wait()
        self.session(gone.pid, self.repo)  # a session that ended does not count
        live = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        self.session(live.pid, self.repo / 'sub', 'mine')
        self.assertIn('fast-forwarded', self.hook(session_id='mine'))  # its own entry is no other session
        self.land('AGENTS.md', 'newer rule\n')
        self.session(live.pid, self.repo / 'sub')
        said = self.hook(session_id='mine')
        self.assertIn('was left as it is because 1 other live session works in it', said)
        self.assertIn('Rule files changed upstream: AGENTS.md', said)
        self.assertEqual(self.rule(), 'new rule\n')
        self.session(live.pid, self.root / 'elsewhere')  # a session in another folder does not count
        self.assertIn('fast-forwarded', self.hook(session_id='mine'))
        self.land('AGENTS.md', 'newest rule\n')
        terminal = self.root / '.claude' / 'sessions'
        self.session(live.pid, self.repo, folder=terminal)  # a plain terminal session counts too
        self.assertIn('because 1 other live session works in it', self.hook(session_id='mine'))
        shutil.rmtree(terminal)
        shutil.rmtree(self.sessions)
        self.assertIn('because live sessions could not be listed', self.hook())

    @unittest.skipUnless(os.name == 'nt', 'process start times are compared on Windows')
    def test_a_process_number_handed_on_after_a_crash_is_no_live_session(self):
        self.land('AGENTS.md', 'new rule\n')
        live = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        self.session(live.pid, self.repo, procStart='116444736000000000')  # 1970: another process had this number
        self.assertIn('fast-forwarded', self.hook(session_id='mine'))

    def test_an_ignored_file_upstream_now_tracks_is_never_overwritten(self):
        (self.repo / '.git' / 'info' / 'exclude').write_text('local.cfg\n', encoding='utf-8')
        (self.repo / 'local.cfg').write_text('MINE\n', encoding='utf-8')
        self.land('local.cfg', 'upstream\n')
        self.assertIn('the fast-forward failed.', self.hook())
        self.assertEqual((self.repo / 'local.cfg').read_text(encoding='utf-8'), 'MINE\n')

    def test_a_moved_submodule_is_named_and_does_not_stop_later_updates(self):
        allow = ('-c', 'protocol.file.allow=always')
        lib = self.root / 'lib'
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(lib)], check=True, env=self.env)
        for n in (1, 2):
            (lib / 'x.c').write_text(f'{n}\n', encoding='utf-8')
            self.git(lib, 'add', '-A')
            self.git(lib, 'commit', '-q', '-m', str(n))
        self.git(self.other, *allow, 'submodule', 'add', '-q', str(lib), 'lib')
        self.git(self.other, 'commit', '-q', '-m', 'lib')
        self.git(self.other, 'push', '-q', 'origin', 'HEAD:main')
        self.assertIn('Submodules moved and were not updated: lib', self.hook())
        self.git(self.repo, *allow, 'submodule', 'update', '-q', '--init')
        self.git(self.other / 'lib', 'checkout', '-q', 'HEAD~1')
        self.git(self.other, 'commit', '-q', '-am', 'lib back')
        self.git(self.other, 'push', '-q', 'origin', 'HEAD:main')
        self.assertIn('Submodules moved and were not updated: lib', self.hook())
        self.land('AGENTS.md', 'new rule\n')  # the submodule left at its old commit is no edit
        self.assertIn('fast-forwarded', self.hook())
        self.assertEqual(self.rule(), 'new rule\n')

    def test_other_branches_compaction_and_the_switch_stay_silent(self):
        self.land('AGENTS.md', 'new rule\n')
        self.assertIsNone(self.hook(source='compact'))
        self.assertIsNone(self.hook(env={'CLAUDE_FRESH_CHECKOUT': '0'}))
        self.git(self.repo, 'switch', '-q', '-c', 'topic')
        self.assertIsNone(self.hook())
        self.assertIsNone(self.hook(cwd=str(self.root)))  # no repository
        self.assertEqual(self.rule(), 'old rule\n')

    def test_installer_registers_it_with_room_for_the_fetch(self):
        config = {}
        install.set_hooks(config)
        entry = [g for g in config['hooks']['SessionStart'] if 'fresh_checkout.py' in json.dumps(g)]
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry[0]['hooks'][0]['timeout'], 60)


class LandToolTests(unittest.TestCase):
    TOOL = install.REPO / 'tools' / 'land.py'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = {**os.environ, 'TMPDIR': tmp.name, 'TEMP': tmp.name, 'TMP': tmp.name, 'CLAUDECODE': '1',
                    'CLAUDE_REVIEW_LOG': str(self.root / 'gate.log'), 'GIT_CONFIG_GLOBAL': os.devnull,
                    'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
                    'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}
        self.env.pop('CLAUDE_REVIEW_MIN_LINES', None)
        self.origin = self.root / 'origin.git'
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(self.origin)], check=True, env=self.env)
        self.other, self.repo = self.root / 'other', self.root / 'work'
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(self.other)], check=True, env=self.env)
        self.git(self.other, 'remote', 'add', 'origin', str(self.origin))
        self.put(self.other, 'a.rs', 'fn a() {}\n')
        self.git(self.other, 'push', '-q', 'origin', 'HEAD:main')
        subprocess.run(['git', 'clone', '-q', str(self.origin), str(self.repo)], check=True, env=self.env)

    def git(self, where, *args):
        return subprocess.run(['git', '-C', str(where), *args], check=True, env=self.env, capture_output=True, text=True).stdout.strip()

    def put(self, where, name, text, message=None):
        path = Path(where) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        self.git(where, 'add', '-A')
        self.git(where, 'commit', '-q', '-m', message or name)

    def upstream(self, name, text):
        self.git(self.other, 'pull', '-q', '--rebase', 'origin', 'main')
        self.put(self.other, name, text)
        self.git(self.other, 'push', '-q', 'origin', 'HEAD:main')

    def land(self, *args, env=None, tool=None):
        done = subprocess.run([sys.executable, str(tool or self.TOOL), '--repo', str(self.repo), *args], capture_output=True,
                              text=True, env={**self.env, **(env or {})}, encoding='utf-8')
        return done.returncode, done.stdout + done.stderr

    def main_sha(self):
        return self.git(self.origin, 'rev-parse', 'main')

    def decisions(self):
        log = self.root / 'gate.log'
        return [json.loads(line)['decision'] for line in log.read_text(encoding='utf-8').splitlines()] if log.exists() else []

    def test_nothing_to_land_and_a_small_change_lands(self):
        self.assertEqual(self.land()[0], 0)
        self.put(self.repo, 'b.rs', 'fn b() {}\n')
        code, out = self.land()
        self.assertEqual(code, 0, out)
        self.assertIn('landed', out)
        self.assertEqual(self.main_sha(), self.git(self.repo, 'rev-parse', 'HEAD'))

    def test_a_moved_main_is_rebased_and_only_then_checked(self):
        mark = self.root / 'checked'
        check = 'python -c "import os; open(os.environ[\'MARK\'], \'a\').write(\'x\')"'
        self.put(self.repo, 'b.rs', 'fn b() {}\n')
        self.assertEqual(self.land('--check', check, env={'MARK': str(mark)})[0], 0)
        self.assertFalse(mark.exists())  # main had not moved: no check
        self.put(self.repo, 'c.rs', 'fn c() {}\n')
        self.upstream('d.rs', 'fn d() {}\n')
        code, out = self.land('--check', check, env={'MARK': str(mark)})
        self.assertEqual(code, 0, out)
        self.assertIn('rebased 1 commit', out)
        self.assertTrue(mark.exists())
        self.assertEqual(self.git(self.origin, 'log', '--format=%s', '-2', 'main').splitlines(), ['c.rs', 'd.rs'])

    def test_a_failed_check_or_a_conflict_pushes_nothing_and_leaves_the_checkout(self):
        self.put(self.repo, 'b.rs', 'fn mine() {}\n')
        self.upstream('e.rs', 'fn e() {}\n')
        before = self.main_sha()
        code, out = self.land('--check', 'exit 7')
        self.assertEqual(code, 3, out)
        self.assertEqual(self.main_sha(), before)
        self.upstream('b.rs', 'fn theirs() {}\n')
        head = self.git(self.repo, 'rev-parse', 'HEAD')
        code, out = self.land()
        self.assertEqual(code, 2, out)
        self.assertIn('b.rs', out)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), head)
        self.assertEqual(self.git(self.repo, 'status', '--porcelain'), '')

    def test_uncommitted_edits_are_refused_and_untracked_files_are_not(self):
        self.put(self.repo, 'b.rs', 'fn b() {}\n')
        (self.repo / 'b.rs').write_text('fn edited() {}\n', encoding='utf-8')
        code, out = self.land()
        self.assertEqual(code, 1, out)
        self.assertIn('never stashes', out)
        self.git(self.repo, 'checkout', '--', 'b.rs')
        (self.repo / 'notes.txt').write_text('mine\n', encoding='utf-8')
        self.assertEqual(self.land()[0], 0)
        self.assertTrue((self.repo / 'notes.txt').exists())

    def test_a_large_unreviewed_change_is_held_and_a_skip_passes_and_is_logged(self):
        self.put(self.repo, 'big.rs', ''.join(f'let v{n} = {n};\n' for n in range(300)))
        before = self.main_sha()
        code, out = self.land()
        self.assertEqual(code, 4, out)
        self.assertIn('land.py again', out)
        self.assertNotIn('REVIEW_SKIP=<reason>` at the end', out)
        self.assertEqual(self.main_sha(), before)
        self.assertEqual(self.decisions(), ['held'])
        code, out = self.land('--skip', 'user asked for speed')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.decisions(), ['held', 'skipped'])

    def test_outside_claude_code_the_gate_is_not_asked_and_a_missing_gate_holds(self):
        self.put(self.repo, 'big.rs', ''.join(f'let v{n} = {n};\n' for n in range(300)))
        lone = self.root / 'lone' / 'tools'
        lone.mkdir(parents=True)
        shutil.copy(self.TOOL, lone / 'land.py')
        code, out = self.land(tool=lone / 'land.py')
        self.assertEqual(code, 4, out)
        self.assertIn('review gate is missing', out)
        code, out = self.land(env={'CLAUDECODE': ''})
        self.assertEqual(code, 0, out)
        self.assertIn('review gate not asked', out)

    def test_a_detached_worktree_given_as_a_git_bash_path_lands(self):
        wt = self.root / 'wt'
        self.git(self.repo, 'worktree', 'add', '-q', '--detach', str(wt), 'origin/main')
        self.put(wt, 'f.rs', 'fn f() {}\n')
        path = str(wt)
        if os.name == 'nt':
            path = '/' + path[0].lower() + path[2:].replace('\\', '/')
        done = subprocess.run([sys.executable, str(self.TOOL), '--repo', path], capture_output=True, text=True, env=self.env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.main_sha(), self.git(wt, 'rev-parse', 'HEAD'))

    def test_a_failed_check_is_owed_until_one_passes(self):
        self.put(self.repo, 'b.rs', 'fn b() {}\n')
        self.upstream('e.rs', 'fn e() {}\n')
        before = self.main_sha()
        self.assertEqual(self.land('--check', 'exit 7')[0], 3)
        code, out = self.land()  # main has not moved since, but the failed check is still owed
        self.assertEqual(code, 3, out)
        self.assertIn('a check is owed', out)
        code, out = self.land('--check', 'exit 7')
        self.assertEqual(code, 3, out)
        self.assertEqual(self.main_sha(), before)
        code, out = self.land('--check', 'exit 0')
        self.assertEqual(code, 0, out)
        self.assertIn('running the check', out)
        self.put(self.repo, 'c.rs', 'fn c() {}\n')
        self.assertNotIn('running the check', self.land('--check', 'exit 7')[1])  # paid: no check until main moves again

    def test_a_rebase_without_a_check_lands_and_a_helper_is_told_to_report_a_hold(self):
        self.put(self.repo, 'b.rs', 'fn b() {}\n')
        self.upstream('e.rs', 'fn e() {}\n')
        self.assertEqual(self.land()[0], 0)
        self.put(self.repo, 'big.rs', ''.join(f'let v{n} = {n};\n' for n in range(300)))
        code, out = self.land('--agent', 'runner')
        self.assertEqual(code, 4, out)
        self.assertIn('report this to the agent that sent you', out)

    def test_a_submodule_moved_by_the_rebase_follows_it(self):
        self.env.update({'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'protocol.file.allow', 'GIT_CONFIG_VALUE_0': 'always'})
        lib = self.root / 'lib'
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(lib)], check=True, env=self.env)
        self.put(lib, 'x.txt', 'one\n')
        self.git(self.other, 'pull', '-q', '--rebase', 'origin', 'main')
        self.git(self.other, 'submodule', 'add', '-q', str(lib), 'lib')
        self.git(self.other, 'commit', '-q', '-m', 'add lib')
        self.git(self.other, 'push', '-q', 'origin', 'HEAD:main')
        self.git(self.repo, 'pull', '-q', '--rebase', 'origin', 'main')
        self.git(self.repo, 'submodule', 'update', '-q', '--init')
        self.put(self.repo, 'b.rs', 'fn b() {}\n')
        self.put(lib, 'x.txt', 'two\n')
        self.git(self.other / 'lib', 'pull', '-q', 'origin', 'main')
        self.git(self.other, 'commit', '-q', '-am', 'bump lib')
        self.git(self.other, 'push', '-q', 'origin', 'HEAD:main')
        code, out = self.land()
        self.assertEqual(code, 0, out)
        self.assertIn('submodule lib follows the rebase', out)
        self.assertEqual(self.git(self.repo / 'lib', 'rev-parse', 'HEAD'), self.git(lib, 'rev-parse', 'HEAD'))
        self.assertEqual(self.git(self.repo, 'status', '--porcelain'), '')

    def test_main_moving_during_the_push_is_fetched_rebased_and_pushed_again(self):
        hook = self.repo / '.git' / 'hooks' / 'pre-push'
        once = self.root / 'raced'
        hook.write_text('#!/bin/sh\nunset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX\n'
                        f'if [ ! -f "{once.as_posix()}" ]; then touch "{once.as_posix()}"; '
                        f'git -C "{self.other.as_posix()}" commit -q --allow-empty -m race && '
                        f'git -C "{self.other.as_posix()}" push -q origin HEAD:main; fi\nexit 0\n', encoding='utf-8', newline='\n')
        hook.chmod(0o755)
        self.put(self.repo, 'g.rs', 'fn g() {}\n')
        code, out = self.land()
        self.assertEqual(code, 0, out)
        self.assertIn('main moved during the push', out)
        self.assertEqual(self.git(self.origin, 'log', '--format=%s', '-2', 'main').splitlines(), ['g.rs', 'race'])


class ReviewGateTests(unittest.TestCase):
    GATE = install.REPO / 'hooks' / 'review_gate.py'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / 'work'
        self.env = {**os.environ, 'TMPDIR': tmp.name, 'TEMP': tmp.name, 'TMP': tmp.name,
                    'CLAUDE_REVIEW_LOG': str(self.root / 'gate.log'), 'GIT_CONFIG_GLOBAL': os.devnull,
                    'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
                    'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}
        self.env.pop('CLAUDE_REVIEW_MIN_LINES', None)
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(self.root / 'origin.git')], check=True, env=self.env)
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(self.repo)], check=True, env=self.env)
        self.git('remote', 'add', 'origin', str(self.root / 'origin.git'))
        self.write('a.rs', 1)
        self.commit('start')
        self.git('push', '-q', 'origin', 'HEAD:main')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True, env=self.env, capture_output=True, text=True).stdout

    def write(self, name, lines, mark='x'):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(f'{mark} line {n}\n' for n in range(lines)), encoding='utf-8')

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-q', '-m', message)

    def hook(self, mode, **event):
        done = subprocess.run([sys.executable, str(self.GATE), mode], input=json.dumps({'cwd': str(self.repo), **event}),
                              capture_output=True, text=True, env=self.env)
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        return json.loads(done.stdout)['hookSpecificOutput']['permissionDecisionReason'] if done.stdout else None

    def push(self, command='git push origin HEAD:main', **event):
        return self.hook('push', tool_name='PowerShell', tool_input={'command': command}, **event)

    def decisions(self):
        log = self.root / 'gate.log'
        return [json.loads(line)['decision'] for line in log.read_text(encoding='utf-8').splitlines()] if log.exists() else []

    def test_small_changes_prose_and_other_branches_pass(self):
        self.write('a.rs', 40)
        self.write('docs/guide.md', 900)
        self.write('app/src/main/res/values-de/strings.xml', 400)
        self.commit('small')
        self.assertIsNone(self.push())
        self.write('b.rs', 300)
        self.commit('large')
        self.assertIsNone(self.push('git push origin HEAD:refs/heads/topic'))
        self.assertIsNone(self.push('git push --dry-run origin HEAD:main'))
        self.assertIsNone(self.push('git status; echo push main'))
        self.assertEqual(self.decisions(), [])

    def test_a_large_unreviewed_push_is_held_until_the_reviewer_ran(self):
        self.write('b.rs', 300)
        self.commit('large')
        held = self.push()
        self.assertIn('300 changed code lines in 1 files', held)
        self.assertIn('`reviewer` agent', held)
        self.assertIn('# REVIEW_SKIP=<reason>', held)
        self.assertIn('report this to the agent that sent you', self.push(agent_type='runner'))
        self.assertIsNone(self.hook('reviewed', agent_type='reviewer', last_assistant_message='Clean.\nReviewed: ' + str(self.repo)))
        self.assertIsNone(self.push())
        self.write('b.rs', 340)  # fixes after the review: 40 lines more in b.rs and a new file of 100
        self.write('c.rs', 100)
        self.commit('fixes')
        self.assertIsNone(self.push())
        self.write('d.rs', 200)
        self.commit('more')
        self.assertIn('came after the last review', self.push())
        self.assertEqual(self.decisions(), ['held', 'held', 'review ran', 'reviewed', 'reviewed', 'held'])

    def test_a_review_of_uncommitted_work_counts_and_does_not_cover_the_next_task(self):
        self.write('b.rs', 200)
        self.write('new/c.rs', 90)  # not yet added to git
        self.assertIsNone(self.hook('reviewed', agent_type='reviewer', last_assistant_message=f'Low: a.\n\nReviewed: `{self.repo}`\n'))
        self.commit('reviewed work')
        self.assertIsNone(self.push())
        self.git('push', '-q', 'origin', 'HEAD:main')
        self.write('b.rs', 200, mark='z')
        self.commit('the next task, in the same file')
        self.assertIn('no independent review has seen', self.push())

    def test_a_review_that_did_not_finish_or_saw_little_unlocks_nothing(self):
        self.write('b.rs', 100)
        self.commit('large')
        self.assertIsNone(self.hook('reviewed', agent_type='reviewer', last_assistant_message='Interrupted before the report.'))
        self.assertIn('no independent review has seen', self.push())
        self.git('reset', '-q', '--hard', 'origin/main')
        self.assertIsNone(self.hook('reviewed', agent_type='reviewer', last_assistant_message=f'Reviewed: {self.repo}'))  # a clean tree
        self.write('b.rs', 100)
        self.commit('written after the review')
        self.assertIn('no independent review has seen', self.push())
        self.git('reset', '-q', '--hard', 'origin/main')
        self.write('b.rs', 30)
        self.write('c.rs', 20)
        self.assertIsNone(self.hook('reviewed', agent_type='reviewer', last_assistant_message=f'Reviewed: {self.repo}'))  # 50 lines seen
        self.write('c.rs', 90)
        self.commit('more than was reviewed')
        self.assertIn('70 changed code lines came after the last review', self.push())
        self.assertEqual(self.decisions(), ['review not counted', 'held', 'review ran', 'held', 'review ran', 'held'])

    def test_a_review_covers_the_content_it_saw_not_a_line_count(self):
        self.write('b.rs', 300)
        self.commit('large')
        self.assertIsNone(self.hook('reviewed', agent_type='reviewer', last_assistant_message=f'Reviewed: {self.repo}'))
        self.write('b.rs', 300, mark='y')  # as many lines, all of them different
        self.commit('rewritten')
        self.assertIn('no independent review has seen', self.push())

    def test_a_commit_and_push_in_one_command_is_judged_by_the_whole_checkout(self):
        self.write('a.rs', 200)
        self.write('b.rs', 100)  # not tracked yet
        one = "git add -A; git commit -F - <<'EOF'\nfeat: b\nEOF\ngit push origin HEAD:main"
        held = self.push(one)
        self.assertIn('299 changed code lines in 2 files', held)
        self.assertIn('push in a command of its own', held)
        self.assertIn('`reviewer` agent', held)
        self.assertIn('the working tree against', held)
        helper = self.push(one, agent_type='runner')
        self.assertIn('push in a command of its own', helper)
        self.assertIn('report this to the agent that sent you', helper)
        self.assertNotIn('`reviewer` agent', helper)
        self.assertIsNone(self.push())  # nothing is committed, so a bare push carries nothing
        self.assertIsNone(self.hook('reviewed', agent_type='reviewer', last_assistant_message=f'Reviewed: {self.repo}'))
        self.write('b.rs', 120)
        self.assertIsNone(self.push(one))
        self.commit('feat: b')
        self.assertIsNone(self.push())
        self.git('push', '-q', 'origin', 'HEAD:main')
        self.write('a.rs', 200, mark='z')
        self.assertIn('no independent review has seen', self.push(one))
        self.assertEqual(self.decisions(), ['held', 'held', 'review ran', 'reviewed', 'reviewed', 'held'])

    def test_what_a_commit_in_the_command_takes_is_not_guessed(self):
        self.write('a.rs', 200)  # someone's unfinished work in a tracked file
        self.write('sub/n.rs', 200)  # and in a new one
        self.write('docs/guide.md', 50)
        for command in ('git add docs/guide.md; git commit -m "docs: guide"; git push origin HEAD:main',  # takes prose only
                        'git commit -am "fix: a; b"; git push origin HEAD:main',
                        'git commit -am "fix: a (b) c" > /dev/null; git push origin HEAD:main',
                        "git commit -am 'don''t break'; git push origin HEAD:main",
                        'git commit -a --message="a b"; git push origin HEAD:main',
                        'git commit -am "$(cat msg.txt)"; git push origin HEAD:main',
                        'git add -A > $null; git commit -m x; git push origin HEAD:main',
                        'git -C sub add n.rs; git commit -m x; git push origin HEAD:main',
                        'cd sub; git add n.rs; cd ..; git commit -m x; git push origin HEAD:main',
                        'git stage -A; git commit -m x; git push origin HEAD:main',
                        '$f = "a.rs"; git add $f; git commit -m x; git push origin HEAD:main',
                        'git commit -m x docs/guide.md; git push origin HEAD:main'):
            self.assertIn('399 changed code lines in 2 files', self.push(command), command)
        self.git('add', 'sub/n.rs')
        (self.repo / 'sub/n.rs').unlink()  # staged, then gone from the tree: a plain commit still takes it
        self.assertIn('399 changed code lines in 2 files', self.push('git commit -m x; git push origin HEAD:main'))
        self.git('add', 'a.rs')
        self.write('a.rs', 5)  # staged, then cut down in the tree: the commit takes the 200 lines
        for command in ('git commit -m x; git push origin HEAD:main', 'git switch -c fix; git commit -m x; git push origin fix:main'):
            self.assertIn('399 changed code lines in 2 files', self.push(command), command)
        self.git('reset', '-q')
        self.git('add', 'docs/guide.md')  # made apart, the commit of the guide alone goes out
        self.git('commit', '-q', '-m', 'docs: guide')
        self.assertIsNone(self.push())
        self.assertEqual(set(self.decisions()), {'held'})

    def test_the_push_is_judged_in_the_checkout_the_command_names(self):
        self.write('b.rs', 300)
        self.commit('large')
        elsewhere = {'cwd': str(self.root)}
        for command in (f'Set-Location {self.repo}; git add -A; git push origin HEAD:main', f'cd "{self.repo}" && git push origin main',
                        f'git -C "{self.repo}" push origin HEAD:main', f"git -C '{self.repo}' push"):
            self.assertIsNotNone(self.push(command, **elsewhere), command)
        self.assertIsNone(self.push(**elsewhere))
        for command in (f'(cd "{self.repo}" && git push origin HEAD:main)', f'Push-Location {self.repo}; git push origin HEAD:main; Pop-Location',
                        f'& {{ Set-Location "{self.repo}"; git push origin HEAD:main}}'):
            self.assertIsNotNone(self.push(command, **elsewhere), command)
        self.assertIsNotNone(self.push('cd work && git push origin HEAD:main', **elsewhere))
        self.assertIsNotNone(self.push('Set-Location $unknown; git push origin HEAD:main'))
        if os.name == 'nt':  # Git Bash writes C:\x as /c/x
            bash = '/' + self.repo.drive[0].lower() + self.repo.as_posix()[2:]
            self.assertIsNotNone(self.push(f'cd {bash} && git push origin HEAD:main', **elsewhere))
            self.assertIsNotNone(self.push(f'git -C {bash} push origin HEAD:main', **elsewhere))
            self.git('reset', '-q', '--hard', 'origin/main')
            self.write('b.rs', 300)
            self.assertIsNone(self.hook('reviewed', agent_type='reviewer', last_assistant_message='Reviewed: ' + bash, **elsewhere))
            self.commit('large')
            self.assertIsNone(self.push())

    def test_a_rebase_onto_other_edits_of_the_same_file_needs_no_second_review(self):
        self.write('a.rs', 50)
        self.commit('a file both sides will edit')
        self.git('push', '-q', 'origin', 'HEAD:main')
        self.write('a.rs', 150)  # ours: 100 lines at the end
        self.commit('ours')
        self.assertIsNone(self.hook('reviewed', agent_type='reviewer', last_assistant_message=f'Reviewed: {self.repo}'))
        self.git('checkout', '-q', '-b', 'theirs', 'origin/main')
        (self.repo / 'a.rs').write_text(''.join(f'u line {n}\n' for n in range(80)) + (self.repo / 'a.rs').read_text(encoding='utf-8'),
                                        encoding='utf-8')  # theirs: 80 lines at the top
        self.commit('theirs')
        self.git('push', '-q', 'origin', 'theirs:main')
        self.git('checkout', '-q', 'main')
        self.git('fetch', '-q', 'origin')
        self.git('rebase', '-q', 'origin/main')
        self.assertEqual(len((self.repo / 'a.rs').read_text(encoding='utf-8').splitlines()), 230)
        self.assertIsNone(self.push())
        self.assertEqual(self.decisions(), ['review ran', 'reviewed'])

    def test_a_push_of_another_branch_or_of_commits_the_command_brings_in(self):
        self.git('checkout', '-q', '-b', 'feat')
        self.write('b.rs', 300)
        self.commit('large')
        self.git('checkout', '-q', 'main')
        self.assertIn('300 changed code lines', self.push('git push origin feat:main'))
        self.assertIsNone(self.push('git push origin HEAD:main'))  # main itself carries nothing
        self.assertIsNone(self.push('git fetch origin; git rebase origin/main; git push origin HEAD:main'))
        for command in ('git merge --ff-only feat; git push origin main', 'git cherry-pick feat && git push origin HEAD:main',
                        'git revert --no-edit HEAD~3; git push origin HEAD:main'):
            self.assertIn('a command of its own', self.push(command), command)
        self.assertIsNone(self.push('git merge --ff-only feat; git push origin main  # REVIEW_SKIP=merge train'))
        self.assertEqual(self.decisions(), ['held', 'held', 'held', 'held', 'skipped'])

    def test_what_counts_as_a_push_to_main(self):
        self.write('b.rs', 300)
        self.commit('large')
        for command in ('git --no-pager push origin HEAD:main', 'git push origin HEAD', 'git push origin main --tags',
                        'git push -u origin +HEAD:refs/heads/main 2>&1', 'GIT_TRACE=1 git push origin HEAD:main',
                        'if true; then git push origin HEAD:main; fi', 'git add -A; if ($?) { git push origin "HEAD:main" }'):
            self.assertIsNotNone(self.push(command), command)
        for command in ('git push origin HEAD:feature/main', 'git push origin main:topic', 'git push --tags', 'git push origin v1.2.0',
                        "git commit -F - <<'EOF'\nfix: x\n\nNext: run git push origin HEAD:main once CI is green.\nEOF",
                        'git commit -m "docs: say when to git push origin HEAD:main"',
                        'git push origin HEAD:main', ):
            if command == 'git push origin HEAD:main':
                self.hook('reviewed', agent_type='scout', last_assistant_message=f'Reviewed: {self.repo}')  # not the reviewer
                self.assertIsNotNone(self.push(command))
            else:
                self.assertIsNone(self.push(command), command)

    def test_a_skip_is_logged_and_zero_turns_the_gate_off(self):
        self.write('b.rs', 300)
        self.commit('large')
        self.assertIsNone(self.push("$env:REVIEW_SKIP='user wants it out now'; git push origin HEAD:main"))
        entry = json.loads((self.root / 'gate.log').read_text(encoding='utf-8'))
        self.assertEqual((entry['decision'], entry['reason'], entry['lines']), ('skipped', 'user wants it out now', 300))
        self.assertIsNone(self.push('git push origin HEAD:main  # REVIEW_SKIP=release fix, user said ship'))
        self.assertEqual(json.loads((self.root / 'gate.log').read_text(encoding='utf-8').splitlines()[1])['reason'],
                         'release fix, user said ship')
        self.env['CLAUDE_REVIEW_MIN_LINES'] = '0'
        self.assertIsNone(self.push())
        done = subprocess.run([sys.executable, str(self.GATE), 'push'], input='not json', capture_output=True, text=True, env=self.env)
        self.assertEqual((done.returncode, done.stdout), (0, ''))


class CostReportTests(unittest.TestCase):
    def test_advisor_tokens_are_listed_apart_from_the_total(self):
        usage = {'input_tokens': 1_000_000, 'iterations': [
            {'type': 'message', 'input_tokens': 1_000_000},
            {'type': 'advisor_message', 'model': 'claude-fable-5-2', 'input_tokens': 100_000, 'output_tokens': 10_000}]}
        entry = {'type': 'assistant', 'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 'message': {'id': 'm1', 'model': 'claude-opus-5-5', 'usage': usage, 'content': []}}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'project').mkdir()
            (Path(tmp) / 'project' / 'session.jsonl').write_text(json.dumps(entry) + '\n', encoding='utf-8')
            out = subprocess.run([sys.executable, str(install.ROOT / 'tools' / 'cost_report.py'), '--root', tmp],
                                 capture_output=True, text=True, check=True).stdout
        self.assertIn('last 7 days: $4.00', out)
        self.assertIn('Advisor calls: 1, $1.20 at list prices', out)  # Fable 5.2: $8 in, $40 out
        self.assertIn('main chat on opus 1', out)


FAKE_OPENCODE = '''
import json, sys, time
brief = sys.stdin.read().strip()
def say(kind, **part):
    print(json.dumps({'type': kind, 'part': part}), flush=True)
if 'say nothing' in brief:
    sys.exit(0)
say('step_start')
say('text', text='looking')
say('tool_use', tool='read')
if 'fail' in brief:
    print(json.dumps({'type': 'error', 'error': {'message': 'quota reached'}}), flush=True)
    sys.exit(0)
say('step_start')
say('text', text='report: ' + brief.splitlines()[0][:24] + ' | ' + ' '.join(sys.argv[1:4]) + ' | ' + brief.splitlines()[-1])
if 'sleep' in brief:
    time.sleep(60)
'''


class DeepSeekToolTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bin = Path(tmp.name)
        fake = self.bin / 'fake.py'
        fake.write_text(FAKE_OPENCODE, encoding='utf-8')
        if os.name == 'nt':  # a shim with a child process, as npm installs it
            (self.bin / 'opencode.cmd').write_text(f'@"{sys.executable}" "{fake}" %*\n', encoding='utf-8')
        else:
            shim = self.bin / 'opencode'
            shim.write_text(f'#!/bin/sh\n"{sys.executable}" "{fake}" "$@"\n', encoding='utf-8')
            shim.chmod(0o755)
        self.env = {**os.environ, 'PATH': str(self.bin)}

    def tool(self, *args, **kw):
        return subprocess.run([sys.executable, str(install.REPO / 'tools' / 'deepseek.py'), *args],
                              capture_output=True, text=True, env=self.env, **kw)

    def test_prints_only_the_last_report_and_sends_the_mode_and_model(self):
        done = self.tool('read', 'find the thing')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(),
                         'report: This is a read-only task | run -m opencode-go/deepseek-v4.1-flash | find the thing')
        done = self.tool('write', '-', input='make "a b" & c\n')
        self.assertEqual(done.stdout.strip(),
                         'report: This is a scaffolding ta | run -m opencode-go/deepseek-v4.1-flash | make "a b" & c')

    def test_a_timeout_ends_the_run_and_keeps_what_was_said(self):
        start = time.monotonic()
        done = self.tool('read', 'sleep', '--timeout', '5')
        self.assertEqual(done.returncode, 124)
        self.assertIn('timed out after 5 s', done.stderr)
        self.assertIn('report: ', done.stdout)
        self.assertLess(time.monotonic() - start, 30)

    def test_fails_on_an_error_no_report_an_empty_brief_or_no_opencode(self):
        failed = self.tool('read', 'fail')
        self.assertEqual(failed.returncode, 1)
        self.assertIn('quota reached', failed.stderr)
        silent = self.tool('read', 'say nothing')
        self.assertEqual((silent.returncode, silent.stdout), (1, ''))
        self.assertIn('no report came back', silent.stderr)
        self.assertEqual(self.tool('read', ' ').returncode, 1)
        self.env['PATH'] = str(self.bin / 'none')
        self.assertIn('opencode is not on PATH', self.tool('read', 'x').stderr)


FAKE_SSH = '''import json, sys
with open(sys.argv[1], 'a', encoding='utf-8') as f:
    f.write(json.dumps(sys.argv[2:]) + '\\n')
sys.exit(1 if sys.argv[-2] == 'broken' else 0)
'''


class TaskToolTests(unittest.TestCase):
    BIRTH = os.name == 'nt' or hasattr(os.stat('.'), 'st_birthtime')  # Linux keeps no creation time: the folder's name decides there

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.artifacts, self.tests, self.temp = self.root / 'artifacts', self.root / 'tests', self.root / 'temp'
        for folder in (self.artifacts / 'reference' / 'sdk', self.tests / 'paired' / 'certs', self.temp):
            folder.mkdir(parents=True)
        (self.root / 'fake_ssh.py').write_text(FAKE_SSH, encoding='utf-8')
        self.machine = {'artifacts': str(self.artifacts), 'roots': [str(self.tests)], 'keep': ['reference', str(self.tests / 'paired' / 'certs')],
                        'hosts': {'nuc': {'ssh': 'nuc', 'shell': 'sh', 'roots': ['/mnt/hdd2']},
                                  'laptop': {'ssh': 'me@laptop', 'shell': 'powershell', 'roots': ['C:\\src\\Artifacts']},
                                  'off': {'ssh': 'broken', 'shell': 'sh', 'roots': ['/data']}},
                        'ssh': [sys.executable, str(self.root / 'fake_ssh.py'), str(self.root / 'ssh.log')]}
        self.configure()
        self.env = {**os.environ, 'CLAUDE_MACHINE_FILE': str(self.root / 'machine.json'), 'CLAUDE_TASK_STAMP': str(self.root / 'stamp'),
                    'TMP': str(self.temp), 'TEMP': str(self.temp), 'TMPDIR': str(self.temp)}

    def configure(self, **changes):
        self.machine.update(changes)
        (self.root / 'machine.json').write_text(json.dumps(self.machine), encoding='utf-8')

    def tool(self, *args):
        return subprocess.run([sys.executable, str(install.REPO / 'tools' / 'task.py'), *map(str, args)], capture_output=True, text=True,
                              env=self.env, cwd=self.root)

    def refused(self, why, *args):
        done = self.tool(*args)
        self.assertEqual(done.returncode, 1, args)
        self.assertIn(why, done.stderr, args)
        self.assertNotIn('Traceback', done.stderr)

    def ledger(self, task_name, **changes):
        path = self.artifacts / task_name / 'task.json'
        task = json.loads(path.read_text(encoding='utf-8'))
        if changes:
            task.update(changes)
            path.write_text(json.dumps(task), encoding='utf-8')
        return task

    def link(self, at, to):
        if os.name == 'nt':  # a junction needs no privilege
            import _winapi
            _winapi.CreateJunction(str(to), str(at))
        else:
            os.symlink(to, at)

    def python(self, code):
        return ('& ' if os.name == 'nt' else '') + f'"{sys.executable}" -c "{code}"'

    def sleeper(self, **kw):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], **kw)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child

    def test_open_makes_one_folder_and_takes_over_nothing(self):
        done = self.tool('open', 'fix-a')
        self.assertEqual((done.returncode, Path(done.stdout.strip())), (0, self.artifacts / 'fix-a'))
        task = self.ledger('fix-a')
        self.assertEqual((task['name'], task['entries'], task['worktree']), ('fix-a', [], None))
        self.assertEqual(self.tool('open', 'fix-a').returncode, 0)
        self.assertEqual(self.ledger('fix-a')['opened'], task['opened'])
        (self.artifacts / 'old').mkdir()
        (self.artifacts / 'old' / 'run.log').write_text('x', encoding='utf-8')
        self.link(self.artifacts / 'elsewhere', self.temp)
        for name, why in (('old', 'was not opened by this tool'), ('elsewhere', 'was not opened by this tool'), ('reference', 'keep list'),
                          ('../x', 'is no task name'), ('.closed', 'is no task name')):
            self.refused(why, 'open', name)
        for name in ('old', 'reference', 'elsewhere'):
            self.refused('open the task first', 'close', name)
        self.assertTrue((self.artifacts / 'old' / 'run.log').exists())
        self.assertTrue((self.artifacts / 'reference' / 'sdk').exists())
        self.assertEqual([p.name for p in self.artifacts.iterdir() if p.name.startswith('.')], [])

    def test_a_folder_elsewhere_goes_only_if_the_task_made_it(self):
        (self.tests / 'before').mkdir()
        (self.root / 'elsewhere').mkdir()
        self.tool('open', 'fix-a')
        opened, own = self.ledger('fix-a')['opened'], self.tests / 'fix-a'
        for path in (own / 'profile', own / 'late', own / 'build' / 'deps' / 'clone' / '.git', self.tests / 'repo' / '.git',
                     self.temp / 'fix-a' / 'deep', self.artifacts / 'other'):
            path.mkdir(parents=True)
        self.link(own / 'link', self.root / 'elsewhere')
        for path, why in ((self.root / 'elsewhere', 'outside the temp folder and the roots'), (self.tests, 'is, or holds'), (self.root, 'is, or holds'),
                          (self.tests / 'paired', 'keep list'), (self.tests / 'paired' / 'certs', 'keep list'),
                          (self.tests / 'repo', 'holds a checkout'), (own / 'build', 'holds a checkout'), (own, 'holds a checkout'),
                          (own / 'link', 'it is a link'), (self.artifacts / 'other', 'belong to their own tasks'),
                          (self.artifacts, 'is, or holds'), (self.temp, 'is, or holds'), (self.tests / 'missing', 'is no folder')):
            self.refused(why, 'add', 'fix-a', 'dir', path)
        if self.BIRTH:
            self.ledger('fix-a', opened=opened + 60)  # as if these had been there before the task
            for path in (self.tests / 'before', own / 'late'):
                self.refused('was there before the task was opened', 'add', 'fix-a', 'dir', path)
            self.ledger('fix-a', opened=opened)
        else:
            self.refused('keeps no creation time', 'add', 'fix-a', 'dir', self.tests / 'before')
        for path in (own / 'profile', self.temp / 'fix-a', own / 'late', own / 'profile'):
            self.assertEqual(self.tool('add', 'fix-a', 'dir', path).returncode, 0, path)
        self.assertEqual(len(self.ledger('fix-a')['entries']), 3)  # the same folder twice is one entry
        self.configure(keep=self.machine['keep'] + [str(own / 'late')])  # kept since: close must leave it
        done = self.tool('close', 'fix-a')
        self.assertEqual(done.returncode, 1)
        self.assertIn('LEFT dir', done.stdout)
        self.assertIn('1 of this task', done.stderr)
        self.assertEqual([Path(e['path']).name for e in self.ledger('fix-a')['entries']], ['late'])
        self.assertEqual([(own / 'profile').exists(), (self.temp / 'fix-a').exists(), (own / 'late').exists()], [False, False, True])
        self.configure(keep=self.machine['keep'][:2])
        done = self.tool('close', 'fix-a')
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        self.assertIn('closed fix-a', done.stdout)
        self.assertEqual(sorted(p.name for p in own.iterdir()), ['build', 'link'])  # never recorded, so they stay
        self.assertEqual(sorted(p.name for p in self.tests.iterdir()), ['before', 'fix-a', 'paired', 'repo'])
        self.assertTrue((self.root / 'elsewhere').is_dir())
        self.assertEqual(sorted(p.name for p in self.artifacts.iterdir()), ['.closed', 'other', 'reference'])
        self.assertEqual(list((self.artifacts / '.closed').iterdir()), [])

    def test_a_changed_record_cannot_reach_what_the_task_did_not_make(self):
        (self.tests / 'before').mkdir()
        time.sleep(2.5)  # older than the slack a clock is allowed
        self.tool('open', 'fix-a')
        self.ledger('fix-a', name='hdd2', opened=0, entries=[
            {'type': 'dir', 'path': str(self.tests / 'before')}, {'type': 'remote', 'host': 'nuc', 'path': '/mnt/hdd2/everything'},
            'junk', {'type': 'command', 'shell': 'zsh', 'text': 'x'}, {'type': 'process'}, {'type': 'dir', 'path': str(self.tests / 'paired')}])
        done = self.tool('close', 'fix-a')
        self.assertEqual((done.returncode, done.stdout.count('LEFT')), (1, 6), done.stdout + done.stderr)
        self.assertNotIn('Traceback', done.stderr)
        self.assertTrue((self.tests / 'before').is_dir())
        self.assertTrue((self.tests / 'paired' / 'certs').is_dir())
        self.assertFalse((self.root / 'ssh.log').exists())
        self.assertEqual(len(self.ledger('fix-a')['entries']), 6)
        self.tool('open', 'hdd2')  # an honest name that is also part of a root
        self.refused('must be <root>/hdd2', 'add', 'hdd2', 'remote', 'nuc', '/mnt/hdd2/paired')

    def test_the_task_folder_stays_while_it_holds_a_checkout_or_is_in_use(self):
        self.tool('open', 'fix-a')
        folder = self.artifacts / 'fix-a'
        (folder / 'clone' / '.git').mkdir(parents=True)
        (folder / 'clone' / 'work.txt').write_text('not committed', encoding='utf-8')
        marker = self.root / 'undone'
        self.tool('add', 'fix-a', 'command', self.python(f"open('{marker.as_posix()}', 'w').close()"))
        self.refused('is a checkout inside the task folder', 'close', 'fix-a')
        self.assertTrue((folder / 'clone' / 'work.txt').exists())
        self.assertFalse(marker.exists())  # nothing was undone either
        shutil.rmtree(folder / 'clone')
        if os.name == 'nt':  # a program standing in the folder: Windows cannot delete it, and must find out before deleting anything
            (folder / 'out.log').write_text('x', encoding='utf-8')
            busy = self.sleeper(cwd=folder)
            self.refused('is in use', 'close', 'fix-a')
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ['out.log', 'task.json'])
            self.assertTrue(marker.exists())  # what was recorded is undone first; only the folder waits
            busy.kill()
            busy.wait(timeout=15)
        (folder / 'target').mkdir()
        (folder / 'target' / 'read-only.bin').write_text('x', encoding='utf-8')
        os.chmod(folder / 'target' / 'read-only.bin', 0o444)
        self.link(folder / 'link', self.tests / 'paired')
        os.rename(folder, self.artifacts / '.closing-fix-a')  # as a close leaves it when it dies half-way
        self.refused('did not finish', 'open', 'fix-a')
        self.assertIn('fix-a: a close did not finish, run it again', self.tool('sweep').stdout)
        done = self.tool('close', 'fix-a')
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        self.assertEqual(sorted(p.name for p in self.artifacts.iterdir()), ['.closed', 'reference'])
        self.assertTrue((self.tests / 'paired' / 'certs').is_dir())  # the link went, what it pointed to stays
        self.assertTrue(marker.exists())

    def test_a_task_named_like_a_shared_folder_cannot_delete_it(self):
        disk = self.root / 'disk'  # a root in a root, and the artifact folder in a folder of that root
        self.artifacts = disk / 'claude' / 'artifacts'
        self.configure(artifacts=str(self.artifacts), roots=[str(disk), str(disk / 'tests')], keep=[],
                       hosts={'nuc': {'ssh': 'nuc', 'shell': 'sh', 'roots': ['/mnt/hdd2', '/mnt/hdd2/tests/']}})
        for name in ('claude', 'tests', 'disk'):
            self.assertEqual(self.tool('open', name).returncode, 0)
        for path in (disk / 'tests' / 'paired', disk / 'tests' / 'tests' / 'out', disk / 'tests' / 'other'):
            path.mkdir(parents=True)  # all made after the tasks were opened
        for name, path in (('claude', disk / 'claude'), ('tests', disk / 'tests'), ('disk', disk), ('tests', disk)):
            self.refused('is, or holds', 'add', name, 'dir', path)
        if not self.BIRTH:  # by name, and the name counts below the nearest root
            self.refused('keeps no creation time', 'add', 'tests', 'dir', disk / 'tests' / 'other')
        self.assertEqual(self.tool('add', 'tests', 'dir', disk / 'tests' / 'tests').returncode, 0)
        for path, why in (('/mnt/hdd2/tests', 'not below the roots'), ('/mnt/hdd2/tests/', 'not below the roots'), ('/mnt', 'not below the roots'),
                          ('/mnt/hdd2/tests/other', 'must be <root>/tests'), ('/mnt/hdd2/tests/paired/tests', 'must be <root>/tests')):
            self.refused(why, 'add', 'tests', 'remote', 'nuc', path)
        self.assertEqual(self.tool('add', 'tests', 'remote', 'nuc', '/mnt/hdd2/tests/tests/out').returncode, 0)
        self.ledger('tests', entries=self.ledger('tests')['entries'] + [
            {'type': 'dir', 'path': str(disk / 'tests')}, {'type': 'dir', 'path': str(disk / 'claude')},
            {'type': 'remote', 'host': 'nuc', 'path': '/mnt/hdd2/tests'}])
        done = self.tool('close', 'tests')
        self.assertEqual((done.returncode, done.stdout.count('LEFT'), done.stdout.count('ok  ')), (1, 3, 2), done.stdout + done.stderr)
        self.assertNotIn('Traceback', done.stderr)
        self.assertEqual(sorted(p.name for p in (disk / 'tests').iterdir()), ['other', 'paired'])
        self.assertEqual(sorted(p.name for p in self.artifacts.iterdir()), ['claude', 'disk', 'tests'])
        calls = [json.loads(line)[5] for line in (self.root / 'ssh.log').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(calls, ["rm -rf -- /mnt/hdd2/tests/tests/out"])

    def test_a_link_in_the_artifact_folder_is_no_task_folder(self):
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        self.tool('open', 'fix-a')
        shutil.copy(self.artifacts / 'fix-a' / 'task.json', elsewhere / 'task.json')
        (elsewhere / 'data.bin').write_text('x', encoding='utf-8')
        for name in ('linked', '.closing-fix-c'):
            self.link(self.artifacts / name, elsewhere)
        self.refused('is a link to another place', 'close', 'linked')
        self.refused('is a link to another place', 'close', 'fix-c')
        self.refused('is a link to another place', 'add', 'linked', 'command', 'x')
        self.refused('was not opened by this tool', 'open', 'linked')
        self.assertEqual(sorted(p.name for p in elsewhere.iterdir()), ['data.bin', 'task.json'])
        self.assertEqual(json.loads((elsewhere / 'task.json').read_text(encoding='utf-8'))['entries'], [])

    def test_a_close_that_stopped_at_the_emptied_folder_is_finished_by_the_next(self):
        (self.artifacts / '.closing-fix-b').mkdir()  # task.json went, the folder could not
        self.refused('did not finish', 'open', 'fix-b')
        done = self.tool('close', 'fix-b')
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        self.assertEqual(self.tool('open', 'fix-b').returncode, 0)
        (self.artifacts / '.closing-fix-d' / 'mine').mkdir(parents=True)  # not left by a close: it stays
        self.refused('could not be deleted', 'close', 'fix-d')
        self.assertTrue((self.artifacts / '.closing-fix-d' / 'mine').is_dir())

    def test_a_lock_is_given_up_only_when_its_owner_is_gone(self):
        spec = importlib.util.spec_from_file_location('task_tool', install.REPO / 'tools' / 'task.py')
        task = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(task)
        lock, me = self.root / 'lock', task.process(os.getpid())
        gone = subprocess.Popen([sys.executable, '-c', 'pass'])
        gone.wait()
        for text, age, given_up in ((f'{os.getpid()} {me[0]}', 5000, False),      # its owner runs, however long it takes
                                    (f'{os.getpid()} {me[0] - 60}', 0, True),     # the number went to another process
                                    (f'{gone.pid} {me[0]}', 0, True), ('', 0, False), ('0 0', 0, False), ('0 0', 1000, True), ('x', 1000, True)):
            lock.write_text(text, encoding='utf-8')
            os.utime(lock, (time.time() - age,) * 2)
            self.assertEqual(task.abandoned(str(lock)), given_up, text)
        (self.artifacts / '.lock-fix-a').write_text(f'{gone.pid} {me[0]}', encoding='utf-8')
        self.assertEqual(self.tool('open', 'fix-a').returncode, 0)
        self.assertFalse((self.artifacts / '.lock-fix-a').exists())
        if os.name != 'nt':  # an abandoned lock that cannot be removed ends the wait by name, not in a loop
            (self.artifacts / '.lock-fix-a').mkdir()
            os.utime(self.artifacts / '.lock-fix-a', (time.time() - 1000,) * 2)
            self.refused('cannot be removed', 'close', 'fix-a')

    def test_a_process_is_ended_only_if_it_is_the_one_recorded(self):
        self.tool('open', 'fix-a')
        opened = self.ledger('fix-a')['opened']
        first = self.sleeper()
        self.ledger('fix-a', opened=opened + 60)
        self.refused('was running before the task was opened', 'add', 'fix-a', 'process', first.pid)
        self.ledger('fix-a', opened=opened)
        self.refused('is no process number', 'add', 'fix-a', 'process', 'python')
        self.assertEqual(self.tool('add', 'fix-a', 'process', first.pid).returncode, 0)
        entry = self.ledger('fix-a')['entries'][0]
        self.assertEqual(Path(entry['program']).stem.lower()[:6], 'python')
        self.ledger('fix-a', entries=[{**entry, 'started': entry['started'] - 100}])  # as if the number had gone to another process
        done = self.tool('close', 'fix-a')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('belongs to another process', done.stdout)
        self.assertIsNone(first.poll())
        self.tool('open', 'fix-b')
        second = self.sleeper()
        self.assertEqual(self.tool('add', 'fix-b', 'process', second.pid).returncode, 0)
        done = self.tool('close', 'fix-b')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('ended', done.stdout)
        second.wait(timeout=15)
        first.kill()
        first.wait(timeout=15)
        self.tool('open', 'fix-c')
        self.refused('is not running', 'add', 'fix-c', 'process', first.pid)

    @unittest.skipIf(os.name == 'nt', 'a program replacing itself is a POSIX matter')
    def test_a_wrapper_that_replaced_itself_goes_with_what_it_started(self):
        self.tool('open', 'fix-a')
        wrapper = subprocess.Popen(['sh', '-c', 'sleep 0.2; sh -c "sleep 12077" & exec sleep 12078'])
        self.addCleanup(wrapper.wait)
        self.addCleanup(wrapper.kill)
        self.assertEqual(self.tool('add', 'fix-a', 'process', wrapper.pid).returncode, 0)
        time.sleep(1)  # by now it is `sleep`, not `sh`, under the same number
        done = self.tool('close', 'fix-a')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('ended', done.stdout)
        wrapper.wait(timeout=15)
        time.sleep(0.5)
        self.assertEqual(subprocess.run(['pgrep', '-f', 'sleep 1207[78]'], capture_output=True).returncode, 1)

    def test_a_command_puts_back_the_rest_and_a_failure_keeps_the_folder(self):
        self.tool('open', 'fix-a')
        flag, marker = (self.root / 'flag').as_posix(), self.root / 'restored'
        failing = self.python(f"import os, sys; sys.exit(0 if os.path.exists('{flag}') else 7)")
        self.assertEqual(self.tool('add', 'fix-a', 'command', failing, '--why', 'device setting').returncode, 0)
        self.assertEqual(self.tool('add', 'fix-a', 'command', self.python(f"open('{marker.as_posix()}', 'w').close()")).returncode, 0)
        done = self.tool('close', 'fix-a')
        self.assertEqual(done.returncode, 1)
        self.assertIn('LEFT command device setting: exit ', done.stdout)  # Windows PowerShell reports any failure as 1
        self.assertTrue(marker.exists())
        self.assertEqual([e.get('why') for e in self.ledger('fix-a')['entries']], ['device setting'])
        Path(flag).write_text('', encoding='utf-8')
        self.assertEqual(self.tool('close', 'fix-a').returncode, 0)
        self.assertFalse((self.artifacts / 'fix-a').exists())

    def test_a_folder_on_another_machine_must_be_the_tasks_own(self):
        self.tool('open', 'fix-a')
        for host, path, why in (('tv', '/mnt/hdd2/fix-a', 'no host "tv"'), ('nuc', '/home/x/fix-a', 'not below the roots'),
                                ('nuc', '/mnt/hdd2', 'not below the roots'), ('nuc', '/mnt/hdd2x/fix-a', 'not below the roots'),
                                ('nuc', '/mnt/hdd2/other', 'must be <root>/fix-a'), ('nuc', '/mnt/hdd2/x/fix-a', 'must be <root>/fix-a'),
                                ('nuc', '/mnt/hdd2/fix-a/../other', 'no relative parts'), ('nuc', '/mnt/hdd2/fix-a//x', 'no relative parts'),
                                ('laptop', 'D:\\x\\fix-a', 'not below the roots')):
            self.refused(why, 'add', 'fix-a', 'remote', host, path)
        self.refused('usage: add NAME remote HOST PATH', 'add', 'fix-a', 'remote', 'nuc')
        self.assertEqual(self.tool('add', 'fix-a', 'remote', 'nuc', '/mnt/hdd2/fix-a/target dir/').returncode, 0)
        self.assertEqual(self.tool('add', 'fix-a', 'remote', 'laptop', 'c:\\src\\artifacts\\FIX-A\\it\u2019s').returncode, 0)
        self.assertEqual(self.tool('close', 'fix-a').returncode, 0)
        calls = [json.loads(line) for line in (self.root / 'ssh.log').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([call[:5] for call in calls], [['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'me@laptop'],
                                                        ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'nuc']])
        self.assertEqual(calls[1][5], "rm -rf -- '/mnt/hdd2/fix-a/target dir'")
        script = base64.b64decode(calls[0][5].rsplit(' ', 1)[1]).decode('utf-16-le')
        self.assertIn("Remove-Item -LiteralPath 'c:/src/artifacts/FIX-A/it\u2019\u2019s' -Recurse -Force", script)
        self.tool('open', 'fix-b')
        self.assertEqual(self.tool('add', 'fix-b', 'remote', 'off', '/data/fix-b').returncode, 0)
        done = self.tool('close', 'fix-b')
        self.assertEqual(done.returncode, 1)
        self.assertIn('LEFT remote off:/data/fix-b', done.stdout)
        self.configure(hosts={})  # the host is gone from the machine file: still no crash, and nothing deleted
        self.assertIn('no host "off"', self.tool('close', 'fix-b').stdout)
        self.assertTrue((self.artifacts / 'fix-b').exists())

    def test_sweep_lists_what_looks_finished_and_deletes_nothing(self):
        (self.artifacts / 'legacy').mkdir()
        for name in ('fix-a', 'fix-b', 'fix-c'):
            self.tool('open', name)
        self.ledger('fix-a', worktree=str(self.root / 'gone'))
        self.ledger('fix-c', worktree=str(self.tests))  # a folder that stays when the task is closed
        self.assertEqual(self.tool('close', 'fix-c').returncode, 0)
        done = self.tool('sweep')
        self.assertEqual(done.returncode, 0, done.stderr)
        report = done.stdout
        self.assertIn('Look finished (worktree gone, or untouched for 3 days): 1\n  fix-a: idle 0.0 days, worktree gone', report)
        self.assertIn('Open: 1\n  fix-b:', report)
        self.assertIn(f'still there (remove it through Orca or git once no session lives in it): 1\n  fix-c: {self.tests}', report)
        self.assertIn('  legacy: idle 0.0 days\n', report)
        self.assertIn('  reference: idle 0.0 days, on the keep list', report)
        listed = {row['name']: row for row in json.loads(self.tool('sweep', '--json').stdout)}
        self.assertEqual({name: row['state'] for name, row in listed.items()},
                         {'fix-a': 'open', 'fix-b': 'open', 'fix-c': 'closed', 'legacy': 'unlisted', 'reference': 'unlisted'})
        self.assertEqual(sorted(p.name for p in self.artifacts.iterdir()), ['.closed', 'fix-a', 'fix-b', 'legacy', 'reference'])
        note = json.loads(self.tool('daily').stdout)['hookSpecificOutput']
        self.assertEqual(note['hookEventName'], 'SessionStart')
        self.assertIn('1 task folders', note['additionalContext'])
        self.assertIn('1 closed tasks still have a worktree', note['additionalContext'])
        again = self.tool('daily')
        self.assertEqual((again.returncode, again.stdout, again.stderr), (0, '', ''))
        (self.root / 'stamp').unlink()
        self.env['CLAUDE_MACHINE_FILE'] = str(self.root / 'none.json')
        broken = self.tool('daily')
        self.assertEqual((broken.returncode, broken.stdout, broken.stderr), (0, '', ''))
        self.refused('cannot read', 'sweep')


FAKE_REMOTE = """import json, os, shutil, subprocess, sys
mode, log, args = sys.argv[1], sys.argv[2], sys.argv[3:]
with open(log, 'a', encoding='utf-8') as f:
    f.write(json.dumps([mode, *args]) + '\\n')
rest, flags, at = [], [], 0
while at < len(args):
    if args[at] == '-o':
        at += 1
    elif mode == 'scp' and args[at] in ('-r', '-p', '-q'):
        flags.append(args[at])
    else:
        rest.append(args[at])
    at += 1
if any(arg.startswith('broken') for arg in rest):
    sys.exit('ssh: connect to host broken: timed out')
# What a test asks to happen during a run: [{"at": "ssh 2" | "again", "write": {path: text}, "fail": true}]
what = 'again' if '-r' in flags and os.path.basename(rest[-1]) == 'again' else ''
if mode == 'ssh':
    what = 'ssh %d' % sum(1 for line in open(log, encoding='utf-8') if json.loads(line)[0] == 'ssh')
events, fail = log + '.events.json', False
if os.path.exists(events):
    for event in json.load(open(events, encoding='utf-8')):
        if event['at'] == what:
            for path, text in event.get('write', {}).items():
                open(path, 'w', encoding='utf-8', newline='').write(text)
            fail = event.get('fail', False)
if mode == 'ssh':  # the other machine is this one: run what it was asked to
    code = subprocess.run(rest[1], shell=True).returncode
    sys.exit('ssh: connection lost' if fail else code)
paths = [arg[len('me@box:'):] if arg.startswith('me@box:') else arg for arg in rest]
if '-r' in flags:
    shutil.copytree(paths[0], paths[1])
else:
    for source in paths[:-1]:
        shutil.copy(source, paths[-1])
"""


def pointers_in(text):
    return [line for line in text.splitlines() if line.startswith('- [')]


class MemorySyncTests(unittest.TestCase):
    ROWS = ['- one\n', '- two\n', '- three\n', '- four\n', '- five\n', '- six\n', '- seven\n']

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.here, self.there, self.state, self.log = self.root / 'here' / 'memory', self.root / 'there' / 'memory', self.root / 'state', self.root / 'calls.log'
        self.here.mkdir(parents=True)
        self.there.mkdir(parents=True)
        (self.root / 'fake.py').write_text(FAKE_REMOTE, encoding='utf-8')
        fake = [sys.executable, str(self.root / 'fake.py')]
        machine = {'artifacts': str(self.root / 'artifacts'), 'ssh': [*fake, 'ssh', str(self.log)], 'scp': [*fake, 'scp', str(self.log)],
                   'hosts': {'box': {'ssh': 'me@box', 'shell': 'powershell' if os.name == 'nt' else 'sh'}, 'off': {'ssh': 'broken', 'shell': 'sh'}},
                   'memory': {'proj': {'local': str(self.here), 'host': 'box', 'remote': self.there.as_posix(), 'exclude': ['own.md']},
                              'away': {'local': str(self.here), 'host': 'off', 'remote': '/data/memory'}}}
        (self.root / 'machine.json').write_text(json.dumps(machine), encoding='utf-8')
        self.env = {**os.environ, 'CLAUDE_MACHINE_FILE': str(self.root / 'machine.json'), 'CLAUDE_MEMORY_SYNC_DIR': str(self.state)}

    def tool(self, *args, code=0):
        done = subprocess.run([sys.executable, str(install.REPO / 'tools' / 'memory_sync.py'), *map(str, args)], capture_output=True, text=True, env=self.env)
        self.assertEqual(done.returncode, code, done.stdout + done.stderr)
        self.assertNotIn('Traceback', done.stderr)
        return done.stdout + done.stderr

    def during(self, *events):
        """In the next run: at its n-th ssh call (1 prepares the push, 2 moves the files in) or its second
        fetch ('again'), write files and, for ssh, lose the connection after the command ran."""
        calls = sum(1 for line in self.log.read_text(encoding='utf-8').splitlines() if json.loads(line)[0] == 'ssh') if self.log.exists() else 0
        planned = [{'at': at if at == 'again' else f'ssh {calls + at}', 'write': {str(path): text for path, text in write.items()}, 'fail': fail} for at, write, fail in events]
        Path(str(self.log) + '.events.json').write_text(json.dumps(planned), encoding='utf-8')
        self.addCleanup(lambda: Path(str(self.log) + '.events.json').unlink(missing_ok=True))

    def calm(self):
        Path(str(self.log) + '.events.json').unlink(missing_ok=True)

    @staticmethod
    def memo(body, saved='2026-01-01T00:00:00.000Z', by='s1'):
        return f'---\nname: note\ndescription: a note\nmetadata:\n  type: feedback\n  originSessionId: {by}\n  modified: {saved}\n---\n\n{body}'

    def put(self, name, text, *sides):
        for side in sides or (self.here, self.there):
            (side / name).write_text(text, encoding='utf-8', newline='')

    def read(self, side, name):
        return (side / name).read_bytes().decode('utf-8')

    def names(self, side):
        return sorted(p.name for p in side.iterdir())

    def test_the_first_run_copies_what_one_machine_lacks_and_names_what_differs(self):
        self.put('a.md', self.memo('same on both\n'))
        self.put('only-here.md', self.memo('from here\n'), self.here)
        self.put('only-there.md', self.memo('from there\n'), self.there)
        self.put('differ.md', self.memo('one version\n'), self.here)
        self.put('differ.md', self.memo('another version\n'), self.there)
        self.put('own.md', 'this machine\n', self.here)
        self.put('own.md', 'that machine\n', self.there)
        self.put('odd name.md', 'x\n', self.here)
        self.put('.hidden.md', 'x\n', self.here)
        self.put('MEMORY.md', '- [A](a.md) — a\n- [Here](only-here.md) — h\n- [Own](own.md) — o\n', self.here)
        self.put('MEMORY.md', '- [A](a.md) — a\n- [There](only-there.md) — t\n- [Own](own.md) — o\n', self.there)
        said = self.tool('proj', '--dry', code=1)
        self.assertIn('dry run: nothing written', said)
        self.assertEqual((self.names(self.there), self.state.joinpath('proj', 'base').exists(), self.state.joinpath('proj', 'backup').exists()),
                         (['MEMORY.md', 'a.md', 'differ.md', 'only-there.md', 'own.md'], False, False))
        said = self.tool('proj', code=1)
        self.assertIn('needs a hand: differ.md differs and was never synced', said)
        self.assertIn('left alone, a name this tool does not pass on: .hidden.md, odd name.md', said)
        self.assertEqual(self.names(self.there), ['MEMORY.md', 'a.md', 'differ.md', 'only-here.md', 'only-there.md', 'own.md'])
        self.assertEqual(self.names(self.here), ['.hidden.md', 'MEMORY.md', 'a.md', 'differ.md', 'odd name.md', 'only-here.md', 'only-there.md', 'own.md'])
        self.assertEqual((self.read(self.here, 'differ.md'), self.read(self.there, 'differ.md')), (self.memo('one version\n'), self.memo('another version\n')))
        self.assertEqual(self.state.joinpath('proj', 'theirs', 'differ.md').read_bytes().decode('utf-8'), self.memo('another version\n'))
        self.assertEqual((self.read(self.here, 'own.md'), self.read(self.there, 'own.md')), ('this machine\n', 'that machine\n'))  # excluded: each its own
        whole = '- [A](a.md) — a\n- [Here](only-here.md) — h\n- [There](only-there.md) — t\n- [Own](own.md) — o\n'
        self.assertEqual((self.read(self.here, 'MEMORY.md'), self.read(self.there, 'MEMORY.md')), (whole, whole))
        self.assertEqual(len(list(self.state.joinpath('proj', 'backup').iterdir())), 1)
        self.assertEqual(self.names(self.root / 'there'), ['memory'])  # nothing left beside it
        self.assertIn('to this machine: differ.md', self.tool('proj', '--theirs', 'differ.md'))
        self.assertEqual(self.read(self.here, 'differ.md'), self.memo('another version\n'))
        self.assertIn('in step', self.tool(code=2))  # every pair: this one in step, the other one's machine away
        self.assertFalse(self.state.joinpath('proj', 'theirs').exists())

    def test_an_edit_on_one_machine_goes_to_the_other_and_a_save_alone_is_no_edit(self):
        for name in ('a.md', 'b.md', 'c.md', 'd.md'):
            self.put(name, self.memo('first\nsecond\n'))
        self.tool('proj')
        self.put('a.md', self.memo('edited here\n', '2026-02-01T00:00:00.000Z', 's2'), self.here)
        self.put('b.md', self.memo('edited there\n', '2026-02-02T00:00:00.000Z', 's3'), self.there)
        self.put('c.md', self.memo('first\nsecond\n', '2026-02-03T00:00:00.000Z', 's4'), self.there)  # saved again, nothing changed
        self.put('d.md', self.memo('first\nsecond\n').replace('\n', '\r\n'), self.here)  # other line endings, nothing changed
        said = self.tool('proj')
        self.assertIn('to box: a.md\n', said)
        self.assertIn('to this machine: b.md\n', said)
        self.assertEqual(self.read(self.there, 'a.md'), self.memo('edited here\n', '2026-02-01T00:00:00.000Z', 's2'))
        self.assertEqual(self.read(self.here, 'b.md'), self.memo('edited there\n', '2026-02-02T00:00:00.000Z', 's3'))
        self.assertEqual((self.read(self.here, 'c.md'), self.read(self.there, 'd.md')), (self.memo('first\nsecond\n'),) * 2)
        self.put('d.md', self.memo('first\nsecond, edited\n'), self.there)  # an edit there still arrives, whatever the line endings here
        self.assertIn('to this machine: d.md', self.tool('proj'))
        self.assertIn('in step', self.tool('proj'))

    def test_edits_on_both_machines_merge_when_they_lie_apart_and_stay_when_they_do_not(self):
        rows = self.ROWS
        self.put('m.md', self.memo(''.join(rows)))
        self.tool('proj')
        self.put('m.md', self.memo(''.join(['- ONE\n'] + rows[1:]), '2026-02-01T00:00:00.000Z', 's2'), self.here)
        self.put('m.md', self.memo(''.join(rows[:6] + ['- SEVEN\n']), '2026-02-02T00:00:00.000Z', 's3'), self.there)
        self.assertIn('merged on both: m.md', self.tool('proj'))
        both = self.memo(''.join(['- ONE\n'] + rows[1:6] + ['- SEVEN\n']), '2026-02-02T00:00:00.000Z', 's3')  # the later save's who and when
        self.assertEqual((self.read(self.here, 'm.md'), self.read(self.there, 'm.md')), (both, both))
        mine, theirs = both.replace('- two', '- two, here'), both.replace('- two', '- two, there')
        self.put('m.md', mine, self.here)
        self.put('m.md', theirs, self.there)
        said = self.tool('proj', code=1)
        self.assertIn('needs a hand: m.md was changed on both machines in the same lines', said)
        self.assertEqual((self.read(self.here, 'm.md'), self.read(self.there, 'm.md')), (mine, theirs))  # no merge markers, nothing lost
        self.assertEqual(self.state.joinpath('proj', 'theirs', 'm.md').read_bytes().decode('utf-8'), theirs)
        self.put('m.md', both.replace('- two', '- two, here and there'), self.here)
        self.assertIn('to box: m.md', self.tool('proj', '--mine', 'm.md'))
        self.assertEqual(self.read(self.there, 'm.md'), both.replace('- two', '- two, here and there'))
        self.tool('proj', '--mine', 'nothing.md', code=2)
        self.tool('proj', '--mine', 'm.md', '--theirs', 'm.md', code=2)
        self.tool('--mine', 'm.md', code=2)  # two pairs: which one?

    def test_a_deleted_file_is_neither_copied_back_nor_deleted_and_keeps_its_pointer_where_it_still_is(self):
        for name in ('a.md', 'b.md', 'wrong.md'):
            self.put(name, self.memo(name + '\n'))
        self.put('MEMORY.md', 'Rules\n- [A](a.md) — a\n- [B](b.md) — b\n- [Wrong](wrong.md) — w\n')
        self.tool('proj')
        (self.here / 'wrong.md').unlink()  # forgotten here, with its pointer
        self.put('MEMORY.md', 'Rules\n- [A](a.md) — a\n- [B](b.md) — b\n', self.here)
        (self.there / 'b.md').unlink()  # dropped there, the pointer left behind
        for run in range(2):
            said = self.tool('proj', code=1)
            self.assertIn('wrong.md was deleted here and is still on the other machine', said)
            self.assertIn('b.md was deleted on the other machine and is still here', said)
            self.assertEqual((self.names(self.here), self.names(self.there)), (['MEMORY.md', 'a.md', 'b.md'], ['MEMORY.md', 'a.md', 'wrong.md']))
            self.assertEqual(self.read(self.here, 'MEMORY.md'), 'Rules\n- [A](a.md) — a\n- [B](b.md) — b\n')  # b.md is still here: so is its pointer
            self.assertEqual(self.read(self.there, 'MEMORY.md'), 'Rules\n- [A](a.md) — a\n- [Wrong](wrong.md) — w\n')  # and none to a file it lacks
        self.assertIn('to this machine: MEMORY.md, wrong.md', self.tool('proj', '--theirs', 'wrong.md', code=1))  # back, with its pointer
        self.assertEqual(self.read(self.here, 'MEMORY.md'), 'Rules\n- [A](a.md) — a\n- [B](b.md) — b\n- [Wrong](wrong.md) — w\n')
        (self.here / 'wrong.md').unlink()
        (self.there / 'wrong.md').unlink()  # by hand on both, as the report asks; the pointers follow
        self.assertIn('to box: MEMORY.md, b.md', self.tool('proj', '--mine', 'b.md'))
        self.assertIn('in step', self.tool('proj'))
        self.assertEqual((self.read(self.here, 'MEMORY.md'), self.read(self.there, 'MEMORY.md')), ('Rules\n- [A](a.md) — a\n- [B](b.md) — b\n',) * 2)
        (self.there / 'MEMORY.md').unlink()
        self.assertIn('MEMORY.md was deleted on one machine', self.tool('proj', code=1))
        self.assertEqual(self.read(self.here, 'MEMORY.md'), 'Rules\n- [A](a.md) — a\n- [B](b.md) — b\n')
        self.assertIn('to box: MEMORY.md', self.tool('proj', '--mine', 'MEMORY.md'))

    def test_the_index_is_merged_by_the_file_a_line_points_to(self):
        for name in ('a.md', 'b.md', 'c.md', 'Build-Env.md'):
            self.put(name, self.memo(name + '\n'))
        first = 'Rules\n- [A](a.md) — one\n- [B](b.md) — two\n\nState\n- [C](c.md) — three\n- [Env](build-env.md) — in another case\n'
        self.put('MEMORY.md', first)
        self.tool('proj')
        self.put('d.md', self.memo('d\n'), self.here)
        self.put('MEMORY.md', first.replace('— one', '— one, said better').replace('- [B](b.md) — two\n', '- [B](b.md) — two\n- [D](d.md) — four\n'), self.here)
        self.put('e.md', self.memo('e\n'), self.there)
        self.put('MEMORY.md', first.replace('— two', '— two, said better').replace('- [C](c.md) — three\n', '- [C](c.md) — three\n- [E](e.md) — five\n'), self.there)
        self.tool('proj')
        whole = ('Rules\n- [A](a.md) — one, said better\n- [B](b.md) — two, said better\n- [D](d.md) — four\n\nState\n- [C](c.md) — three\n- [E](e.md) — five\n'
                 '- [Env](build-env.md) — in another case\n')
        self.assertEqual((self.read(self.here, 'MEMORY.md'), self.read(self.there, 'MEMORY.md')), (whole, whole))
        self.assertEqual(self.names(self.here), self.names(self.there))
        self.put('MEMORY.md', whole.replace('State\n', 'Project state\n'), self.there)  # a heading follows the machine that changed it
        self.put('MEMORY.md', whole.replace('- [B](b.md) — two, said better\n', ''), self.here)  # a pointer taken out while the file stays
        self.tool('proj')
        whole = whole.replace('State\n', 'Project state\n').replace('- [B](b.md) — two, said better\n', '')
        self.assertEqual((self.read(self.here, 'MEMORY.md'), self.read(self.there, 'MEMORY.md')), (whole, whole))
        mine, theirs = whole.replace('— three', '— three, from here').replace('Rules', 'The rules'), whole.replace('— three', '— three, from there')
        self.put('MEMORY.md', mine, self.here)
        self.put('MEMORY.md', theirs, self.there)
        self.assertIn('needs a hand: MEMORY.md was changed on both machines in the same lines', self.tool('proj', code=1))
        self.assertEqual((self.read(self.here, 'MEMORY.md'), self.read(self.there, 'MEMORY.md')), (mine, theirs))  # neither edit is dropped
        self.assertIn('to box: MEMORY.md', self.tool('proj', '--mine', 'MEMORY.md'))
        self.assertEqual(self.read(self.there, 'MEMORY.md'), mine)
        self.put('MEMORY.md', '', self.there)  # emptied by a mistake, not edited
        self.assertIn('MEMORY.md on box has 0 pointers where the last run left 5', self.tool('proj', code=2))
        self.assertEqual(self.read(self.here, 'MEMORY.md'), mine)

    def test_it_stops_and_writes_nothing_when_the_other_machine_is_away_or_a_folder_looks_emptied(self):
        for name in 'abcdef':
            self.put(name + '.md', self.memo(name + '\n'))
        self.assertIn('not synced', self.tool('away', code=2))
        self.assertFalse(self.state.joinpath('away', 'base').exists())
        self.assertIn('names no memory pair "nowhere"', self.tool('nowhere', code=2))
        self.tool('proj')
        self.put('a.md', self.memo('edited\n'), self.here)
        kept = self.root / 'kept'
        kept.mkdir()
        for name in 'bcdef':
            os.rename(self.there / (name + '.md'), kept / (name + '.md'))
        self.assertIn('box has 1 memory files where the last run left 6', self.tool('proj', code=2))
        self.assertEqual((self.names(self.here), self.read(self.there, 'a.md')), ([n + '.md' for n in 'abcdef'], self.memo('a\n')))
        self.assertEqual(self.tool('proj', 'away', code=2).count('not synced'), 2)
        (self.state / 'proj' / 'lock').write_text('', encoding='utf-8')
        self.assertIn('another sync of it is running', self.tool('proj', code=2))

    def test_a_file_changed_while_it_runs_is_not_overwritten_and_waits_for_the_next_run(self):
        rows = self.ROWS
        for name in ('m.md', 'n.md', 'p.md', 'q.md'):
            self.put(name, self.memo(''.join(rows)))
        self.put('MEMORY.md', '- [M](m.md) — m\n')
        self.tool('proj')
        edit = lambda row, text, saved='2026-02-01T00:00:00.000Z': self.memo(''.join(rows[:row] + [text] + rows[row + 1:]), saved)
        self.put('m.md', edit(0, '- ONE\n'), self.here)  # for the other machine, which edits it before the second look
        self.put('n.md', edit(0, '- ONE\n'), self.here)  # for the other machine, undisturbed
        self.put('p.md', edit(0, '- ONE\n'), self.here)  # for the other machine, which edits it during the push
        self.put('q.md', edit(6, '- SEVEN\n'), self.there)  # for this machine, where it is edited during the push
        self.put('x.md', self.memo('new there\n'), self.there)  # new there with its pointer, and edited before the second look
        self.put('MEMORY.md', '- [M](m.md) — m\n- [X](x.md) — x\n', self.there)
        late = '2026-02-02T00:00:00.000Z'
        self.during(('again', {self.there / 'm.md': edit(6, '- SEVEN\n', late), self.there / 'x.md': self.memo('new there, edited\n', late)}, False),
                    (1, {self.there / 'p.md': edit(6, '- SEVEN\n', late), self.here / 'q.md': edit(0, '- ONE\n', late)}, False))
        said = self.tool('proj', code=1)
        self.assertIn('changed while this ran, left for the next run: m.md, p.md, q.md, x.md', said)
        self.assertIn('to box: n.md\n', said)
        self.assertEqual([self.read(self.there, 'm.md'), self.read(self.there, 'p.md'), self.read(self.here, 'q.md'), self.read(self.there, 'n.md')],
                         [edit(6, '- SEVEN\n', late), edit(6, '- SEVEN\n', late), edit(0, '- ONE\n', late), edit(0, '- ONE\n')])
        self.assertEqual((self.read(self.here, 'MEMORY.md'), (self.here / 'x.md').exists()), ('- [M](m.md) — m\n', False))  # no pointer before its file
        self.calm()
        said = self.tool('proj')
        self.assertIn('merged on both: m.md, p.md, q.md', said)
        self.assertIn('to this machine: MEMORY.md, x.md', said)
        both = self.memo(''.join(['- ONE\n'] + rows[1:6] + ['- SEVEN\n']), late)
        for name in ('m.md', 'p.md', 'q.md'):
            self.assertEqual((self.read(self.here, name), self.read(self.there, name)), (both, both), name)
        self.assertIn('in step', self.tool('proj'))

    def test_a_run_that_stops_half_way_is_finished_by_the_next_one(self):
        for name in ('a.md', 'b.md', 'c.md', 'gone.md'):
            self.put(name, self.memo(''.join(self.ROWS)))
        self.put('MEMORY.md', 'Rules\n- [A](a.md) — a\n- [B](b.md) — b\n- [C](c.md) — c\n- [Gone](gone.md) — g\n')
        self.tool('proj')
        self.put('new.md', self.memo('new here\n'), self.here)
        self.put('edit.md', self.memo('new here too\n'), self.here)
        (self.there / 'gone.md').unlink()  # the pointer left behind there: this run takes it out of that machine's index
        self.during((2, {}, True))  # the files are moved in, then the connection is lost
        self.assertIn('did not finish', self.tool('proj', code=2))
        self.assertEqual(self.names(self.there), ['MEMORY.md', 'a.md', 'b.md', 'c.md', 'edit.md', 'new.md'])
        self.calm()
        (self.here / 'new.md').unlink()  # forgotten here since: it must not come back from the other machine
        self.put('edit.md', self.memo('new here too, edited\n', '2026-02-01T00:00:00.000Z'), self.here)
        said = self.tool('proj', code=1)
        self.assertIn('new.md was deleted here and is still on the other machine', said)
        self.assertIn('to box: edit.md', said)
        self.assertNotIn('never synced', said)
        self.assertFalse((self.here / 'new.md').exists())
        self.assertEqual(self.read(self.there, 'edit.md'), self.memo('new here too, edited\n', '2026-02-01T00:00:00.000Z'))
        self.assertIn('- [Gone](gone.md) — g\n', self.read(self.here, 'MEMORY.md'))  # gone.md is still here: so is its pointer
        self.assertNotIn('gone.md', self.read(self.there, 'MEMORY.md'))
        self.assertFalse(self.state.joinpath('proj', 'pending').exists())

    def test_two_runs_cut_off_in_a_row_lose_no_record(self):
        for name in ('a.md', 'b.md'):
            self.put(name, self.memo(''.join(self.ROWS)))
        self.put('MEMORY.md', 'Rules\n- [A](a.md) \u2014 a\n- [B](b.md) \u2014 b\n')
        self.tool('proj')
        self.put('n.md', self.memo('new here\n'), self.here)
        self.put('MEMORY.md', 'Rules\n- [A](a.md) \u2014 a, said better\n- [B](b.md) \u2014 b\n- [N](n.md) \u2014 n\n', self.here)
        self.during((2, {}, True))  # run 1: moved in on the other machine, then the connection is lost
        self.tool('proj', code=2)
        self.assertEqual(self.names(self.there), ['MEMORY.md', 'a.md', 'b.md', 'n.md'])
        self.put('a.md', self.memo('edited here\n', '2026-02-01T00:00:00.000Z'), self.here)
        self.during((1, {}, True))  # run 2: cut off before anything is copied
        self.tool('proj', code=2)
        self.calm()
        (self.here / 'n.md').unlink()  # forgotten here, with its pointer; and the other pointer edited once more
        self.put('MEMORY.md', 'Rules\n- [A](a.md) \u2014 a, said better still\n- [B](b.md) \u2014 b\n', self.here)
        said = self.tool('proj', code=1)
        self.assertIn('n.md was deleted here and is still on the other machine', said)
        self.assertNotIn('MEMORY.md was changed on both', said)  # only this machine edited it
        self.assertEqual(self.names(self.here), ['MEMORY.md', 'a.md', 'b.md'])
        self.assertEqual(self.read(self.here, 'MEMORY.md'), 'Rules\n- [A](a.md) \u2014 a, said better still\n- [B](b.md) \u2014 b\n')
        self.assertEqual(self.read(self.there, 'MEMORY.md'), 'Rules\n- [A](a.md) \u2014 a, said better still\n- [B](b.md) \u2014 b\n- [N](n.md) \u2014 n\n')
        self.assertEqual(self.read(self.there, 'a.md'), self.memo('edited here\n', '2026-02-01T00:00:00.000Z'))

    def test_many_files_go_in_one_push_and_the_index_last(self):
        names = [f'note-{n:03}.md' for n in range(60)]
        for name in names:
            self.put(name, self.memo('first\n'))
        self.put('MEMORY.md', ''.join(f'- [N](%s) \u2014 x\n' % name for name in names))
        self.tool('proj')
        for name in names:
            self.put(name, self.memo('edited here\n', '2026-02-01T00:00:00.000Z'), self.here)
        self.put('MEMORY.md', ''.join(f'- [N](%s) \u2014 y\n' % name for name in names), self.here)
        self.assertIn('to box: MEMORY.md, note-000.md', self.tool('proj'))
        self.assertEqual([self.read(self.there, name) for name in names], [self.memo('edited here\n', '2026-02-01T00:00:00.000Z')] * 60)
        self.assertEqual(self.read(self.there, 'MEMORY.md'), self.read(self.here, 'MEMORY.md'))
        self.assertEqual(self.names(self.root / 'there'), ['memory'])
        self.put('MEMORY.md', '', self.there)  # an emptied index stops the run, however few pointers it had
        self.assertIn('--meant', self.tool('proj', code=2))

    def test_most_memories_forgotten_on_purpose_stay_forgotten(self):
        names = [f'{letter}.md' for letter in 'abcdef']
        for name in names:
            self.put(name, self.memo('kept\n'))
        self.put('MEMORY.md', ''.join('- [N](%s) \u2014 x\n' % name for name in names))
        self.tool('proj')
        for name in names[2:]:
            (self.here / name).unlink()
        self.put('MEMORY.md', ''.join('- [N](%s) \u2014 x\n' % name for name in names[:2]), self.here)
        self.assertIn('--meant', self.tool('proj', code=2))
        said = self.tool('proj', '--meant', code=1)
        self.assertIn('c.md was deleted here and is still on the other machine', said)
        self.assertEqual(self.names(self.here), ['MEMORY.md', 'a.md', 'b.md'])  # nothing came back
        self.assertEqual(len(pointers_in(self.read(self.here, 'MEMORY.md'))), 2)
        self.assertEqual(self.names(self.there), ['MEMORY.md', *names])  # and nothing was deleted there
        self.assertEqual(len(pointers_in(self.read(self.there, 'MEMORY.md'))), 6)
        for name in names[2:]:  # deleted there too: the two are in step again, with no flag
            (self.there / name).unlink()
        self.put('MEMORY.md', self.read(self.here, 'MEMORY.md'), self.there)
        self.tool('proj', '--meant')
        self.assertIn('in step', self.tool('proj'))


if __name__ == '__main__':
    unittest.main()
