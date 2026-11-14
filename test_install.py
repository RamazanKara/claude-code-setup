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
                for tool in ('deepseek.py', 'task.py'):
                    self.assertEqual((home / 'tools' / tool).read_bytes(), (install.REPO / 'tools' / tool).read_bytes())
                self.assertEqual([g['hooks'][0]['args'] for g in settings['hooks']['SessionStart']],
                                 [[(home / 'tools' / 'task.py').as_posix(), 'daily']])
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
            self.assertEqual([tool.name for tool in (home / 'tools').iterdir()], ['task.py'])


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


if __name__ == '__main__':
    unittest.main()
