#!/usr/bin/env python3
"""Install this dual-harness workflow setup into ~/.claude (Claude Code) and ~/.gemini/config (Antigravity).

Copies CLAUDE.md, native agents (`scout`, `coder`, `reviewer`), dual-harness hooks, shared tools
(`worker.py`, `laptop.py`, `land.py`, `task.py`, `memory_sync.py`, `update_harnesses.py`), and skills
into ~/.claude, merges settings.json (preserving Orca hooks and marking Orca telemetry async), and
creates ~/.claude/personal.md and machine.json from examples once.

When ~/.gemini/config exists, also installs Antigravity's lean GEMINI.md (with personal.md inlined),
merges a safety-only `"workflow-guards"` hook group (`pre_bash.py --pool-only` on `run_command`,
removing any stale `"orca-status"` hooks while preserving custom user hook groups), and copies the
Antigravity-relevant shared skills (`merge-train`, `public-copy`) into ~/.gemini/config/skills/.
Retired Codex files, retired tools (`deepseek.py`, `oc.py`), and retired legacy agents are cleaned up.
Everything replaced is backed up to ~/.claude/backups/claude-code-setup-<time>/ first.

Orca's Claude profiles set CLAUDE_CONFIG_DIR and import ~/.claude/CLAUDE.md,
so the installer ignores that variable when it points into an Orca profile.

  python install.py            # install, or update after `git pull` or editing personal.md
  python install.py --dry-run  # show what would change
"""
import argparse, datetime, filecmp, hashlib, json, os, re, shutil, stat, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT / 'claude'
AGY_REPO = ROOT / 'antigravity'
_cfg = os.environ.get('CLAUDE_CONFIG_DIR')
HOME = Path(_cfg) if _cfg and 'claude-profiles' not in Path(_cfg).parts else Path.home() / '.claude'
GEMINI = Path.home() / '.gemini' / 'config'
GEMINI_HOOK_GROUP = 'workflow-guards'
GEMINI_SKILLS = ('merge-train', 'public-copy')
GEMINI_RETIRED_SKILLS = ('issue-batch', 'orca-cli', 'orca-computer-use', 'orca-per-workspace-env', 'orchestration')
# Codex and OpenCode get no workflow: the rules and agents this setup once wrote there go (backed up).
RETIRED_HARNESSES = {
    'codex': Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex'),
    'opencode': Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'opencode',
}
RETIRED_FILES = {  # 'rules': a file that starts with this setup's heading
    'codex': {'AGENTS.md': 'rules',
              'agents/mechanic.toml': 'c61b24599ef9f5b2378ec324bd3d7ae060c9cd6435d349eed44086db6f96beed',
              'agents/runner.toml': '066984ee78aff19a6e47258ad64900efac438503a4398288e0edcbf22ed22927',
              'agents/scout.toml': '2a32f78009a691074643711cff086a5cc7075be94259c3759713eff8e1016267'},
    'opencode': {'AGENTS.md': 'rules',
                 'agents/coder.md': '51c5b1e24a147b8d3b3755ebbc774f73168a058ebdbbb6b663fe40913d24f7d4',
                 'agents/mechanic.md': '865722c53554ecc312707ca97c813d13d14c852e472a75d32f34d484d1d0d912',
                 'agents/reviewer.md': 'c64380847fe2dc07797fe5c7e9c4dc6bede168b6fb0d174cb5bd2ebe76891bb2',
                 'agents/runner.md': '7127eb61a688605ceab0a2ad6911882614c14b18a77c28be741405f988d4ddfb',
                 'agents/scout.md': '996f4d63c53010df035128dc629fd3713039f8e8f3a63745a607504a25d3005f'},
}
# Only remove legacy definitions shipped by this setup; preserve user replacements and current agents.
RETIRED_AGENTS = {
    'claude/architect.md': '9542ffe407ed1894aeddf7549a4f741616ea7fa3076d70a12e42f622ee9218a2',
    'claude/designer.md': 'c77acd7c49f9dc8b2ab05262dc7ec15e2b5e13f3247719703a17796dd16da269',
    'claude/implementer.md': 'ef1e6edbf9a7fc10760df32522ec101751ba0aa21bd3f6d7d024b6e3b66b3479',
    'claude/reviewer.md': 'f95a910b92760caf14010455f749de6936ace734bd5ae268fb7448520d1d37a3',
    'claude/scout.md': '48e91b389171cca16095503d6385a28e96a28a435adf76f94b3b0e46afa978c8',
    'claude/runner.md': '015956d20c1885c9a07a50d93ffce8d0b8c98deaf3908743627054c32eece8a5',
    'claude/mechanic.md': '80b8de644704b025bc6c22f67b8b969ddbd6a2abea1868c31585470342ba68d5',
    'antigravity/mechanic.md': '9ddc7bdd0daebd0f35291c3ee4994a2d79f20b5a50961c3db63ac7b82422fd85',
    'antigravity/runner.md': '970bcf763cf979cfa3ccf77a926d4f846f34bf2864858f5e7d3e03813263d4b0',
    'antigravity/scout.md': '05de66d138ed42f3b415ca551282cb5a5e1964580327aaa798768438b5fb9a7a',
}


def read_json(path):
    """Read JSON or JSONC without treating comment markers inside strings as comments."""
    text = path.read_text(encoding='utf-8-sig')
    quoted = r'"(?:\\.|[^"\\])*"'
    text = re.sub(quoted + r'|//[^\r\n]*|/\*.*?\*/',
                  lambda m: m[0] if m[0].startswith('"') else '', text, flags=re.S)
    text = re.sub(quoted + r'|,(?=\s*[}\]])',
                  lambda m: '' if m[0] == ',' else m[0], text)
    return json.loads(text)


def without_denies(value):
    if isinstance(value, dict):
        return {k: without_denies(v) for k, v in value.items() if k != 'deny' and v != 'deny'}
    if isinstance(value, list):
        return [without_denies(v) for v in value
                if v != 'deny' and not (isinstance(v, dict) and v.get('effect') == 'deny')]
    return value


def tool_denies():
    """The template's deny rules, which hide tools from the model; the installer removes every other deny rule."""
    return tuple(read_json(REPO / 'settings.json').get('permissions', {}).get('deny', []))


def clean_permissions(config, keep=()):
    for key in ('permission', 'permissions'):
        if key in config:
            if config[key] == 'deny':
                del config[key]
                continue
            denies = config[key].get('deny') if isinstance(config[key], dict) else None
            kept = [rule for rule in denies if rule in keep] if isinstance(denies, list) else []
            config[key] = without_denies(config[key])
            if kept:
                config[key]['deny'] = list(dict.fromkeys(kept))
            if not config[key]:
                del config[key]


ASYNC_ORCA_EVENTS = ('PostToolUse', 'PostToolUseFailure', 'PostToolBatch', 'SubagentStart',
                     'SubagentStop', 'Stop', 'TeammateIdle', 'SessionEnd')


def clean_claude(config):
    clean_permissions(config, tool_denies())
    overrides = config.get('skillOverrides', {})
    for skill in ('built-in-browser', 'chrome-browser', 'computer-use', 'docs'):
        if overrides.get(skill) == 'off':
            del overrides[skill]
    if not overrides:
        config.pop('skillOverrides', None)
    config.pop('advisorModel', None)
    env = config.get('env')
    if isinstance(env, dict) and env.get('CLAUDE_CODE_SUBAGENT_MODEL') == 'opus':
        del env['CLAUDE_CODE_SUBAGENT_MODEL']
        if not env:
            config.pop('env', None)
    hooks = config.get('hooks')
    if isinstance(hooks, dict):
        for event in ASYNC_ORCA_EVENTS:
            for group in hooks.get(event) or ():
                if isinstance(group, dict) and isinstance(group.get('hooks'), list):
                    for hook in group['hooks']:
                        if isinstance(hook, dict) and 'orca-agent-hook' in str(hook.get('command', '')):
                            hook['async'] = True


# event, matcher, script, its arguments
HOOKS = (('PostToolBatch', None, 'read_streak.py', ()),
         ('PreToolUse', 'Bash|PowerShell', 'pre_bash.py', ()),
         ('SubagentStop', 'reviewer|wave-reviewer', 'review_gate.py', ('reviewed',)),
         ('SessionStart', None, 'fresh_checkout.py', ()),
         ('SessionStart', None, 'tools/task.py', ('daily',)))
RETIRED_TOOLS = ('deepseek.py', 'oc.py')  # replaced by worker.py (Claude Code <-> Antigravity)
RETIRED_HOOKS = ('advisor_guard.py',)
# Superseded PreToolUse entries merged into pre_bash.py (review_gate.py stays registered on SubagentStop).
SUPERSEDED_PRE_TOOL = ('review_gate.py', 'cargo_pool.py')
TIMEOUTS = {'pre_bash.py': 20, 'fresh_checkout.py': 60, 'tools/task.py': 5}


def set_hooks(config):
    """Add this setup's Claude Code hooks to settings, replacing earlier entries for the same scripts."""
    for event, groups in config.get('hooks', {}).items():
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get('hooks'), list):
                drop = (*RETIRED_HOOKS, *(SUPERSEDED_PRE_TOOL if event == 'PreToolUse' else ()))
                group['hooks'] = [hook for hook in group['hooks'] if not any(name in json.dumps(hook) for name in drop)]
        groups[:] = [g for g in groups if not (isinstance(g, dict) and g.get('hooks') == [])]
    config['hooks'] = {event: groups for event, groups in config.get('hooks', {}).items() if groups}
    for event, matcher, name, args in HOOKS:
        script = HOME / (name if '/' in name else 'hooks/' + name)
        entry = {'hooks': [{'type': 'command', 'command': Path(sys.executable).as_posix(),
                            'args': [script.as_posix(), *args],
                            'timeout': TIMEOUTS.get(name, 20 if args else 10)}]}
        if matcher:
            entry = {'matcher': matcher, **entry}
        groups = config.setdefault('hooks', {}).setdefault(event, [])
        for group in groups:  # our earlier entry goes; a user's hook that shares its group stays
            if isinstance(group.get('hooks'), list):
                group['hooks'] = [hook for hook in group['hooks'] if name not in json.dumps(hook)]
        groups[:] = [g for g in groups if g.get('hooks') != []] + [entry]


def set_gemini_hooks(config):
    """Install Antigravity's safety-only PreToolUse hook (`pre_bash.py --pool-only`), removing stale `orca-status` and keeping custom user groups."""
    py = Path(sys.executable).as_posix()
    pre_bash = (HOME / 'hooks' / 'pre_bash.py').as_posix()
    config.pop('orca-status', None)
    config[GEMINI_HOOK_GROUP] = {
        'PreToolUse': [
            {
                'matcher': 'run_command',
                'hooks': [
                    {'type': 'command', 'command': f'"{py}" "{pre_bash}" --pool-only', 'timeout': 20},
                ],
            }
        ],
    }


def merge(base, extra):
    """Deep-merge extra into base. Lists are unioned, scalars from extra win."""
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            merge(base[k], v)
        elif isinstance(v, list) and isinstance(base.get(k), list):
            base[k] += [x for x in v if x not in base[k]]
        else:
            base[k] = v
    return base


def label(dst):
    try:
        return str(dst.relative_to(HOME))
    except ValueError:
        return str(dst)


def antigravity_rules(personal):
    """Render Antigravity's GEMINI.md with personal.md inlined."""
    text = (AGY_REPO / 'GEMINI.md').read_text(encoding='utf-8').rstrip() + '\n'
    if personal.exists():
        text += '\n' + personal.read_text(encoding='utf-8').strip() + '\n'
    return text


def _is_junction(path):
    fn = getattr(os.path, 'isjunction', None)
    if fn is not None:
        return fn(path)
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)) and not path.is_symlink()


class Installer:
    def __init__(self, dry):
        self.dry, self.saved = dry, False
        self.backup = HOME / 'backups' / ('claude-code-setup-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))

    def save(self, dst, name=None):
        if not dst.exists() and not dst.is_symlink():
            return
        rel = Path(name) / dst.name if name else dst.relative_to(HOME)
        print(f'  backup  {label(dst)}')
        self.saved = True
        if not self.dry:
            if dst.exists():
                (self.backup / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, self.backup / rel)
            dst.unlink()

    def place(self, src, dst, name=None):
        if dst.is_file() and not dst.is_symlink() and filecmp.cmp(src, dst, shallow=False):
            return
        self.save(dst, name)
        print(f'  copy    {label(dst)}')
        if not self.dry:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    def settings(self, name='settings.json'):
        dst = HOME / name
        cur = read_json(dst) if dst.exists() else {}
        new = json.loads(json.dumps(cur))
        if name == 'settings.json':
            merge(new, read_json(REPO / 'settings.json'))
            set_hooks(new)
        clean_claude(new)
        if new == cur:
            return
        self.save(dst)
        print(f'  merge   {name}')
        if not self.dry:
            dst.write_text(json.dumps(new, indent=2) + '\n', encoding='utf-8', newline='\n')

    def retire_harnesses(self):
        for harness, root in RETIRED_HARNESSES.items():
            for rel, digest in RETIRED_FILES[harness].items():
                dst = root / rel
                if not dst.is_file():
                    continue
                text = dst.read_text(encoding='utf-8')
                if text.startswith('# Working rules') if digest == 'rules' else hashlib.sha256(text.encode()).hexdigest() == digest:
                    self.save(dst, harness)
                    print(f'  remove  {label(dst)}')

    def retire_agents(self):
        for name, digest in RETIRED_AGENTS.items():
            harness, filename = name.split('/')
            base = HOME if harness == 'claude' else GEMINI
            dst = base / 'agents' / filename
            if dst.is_file() and hashlib.sha256(dst.read_text(encoding='utf-8').encode()).hexdigest() == digest:
                self.save(dst, harness + '-agents')
                print(f'  remove  {label(dst)}')

    def install_agents(self):
        if HOME.is_dir():
            for src in sorted((REPO / 'agents').glob('*.*')):
                self.place(src, HOME / 'agents' / src.name, 'claude-agents')

    def antigravity(self, personal):
        """Install Antigravity GEMINI.md, merged hooks.json, and shared skills into ~/.gemini/config."""
        if not GEMINI.is_dir():
            return
        # 1. GEMINI.md (inlines personal.md)
        rules_dst = GEMINI / 'GEMINI.md'
        text = antigravity_rules(personal)
        if not (rules_dst.is_file() and not rules_dst.is_symlink() and rules_dst.read_text(encoding='utf-8') == text):
            self.save(rules_dst, 'antigravity')
            print(f'  write   {label(rules_dst)}')
            if not self.dry:
                rules_dst.write_text(text, encoding='utf-8', newline='\n')
        # 2. hooks.json (safety-only workflow-guards, drop stale orca-status, preserve custom user groups)
        hooks_dst = GEMINI / 'hooks.json'
        cur = read_json(hooks_dst) if hooks_dst.is_file() else {}
        new = json.loads(json.dumps(cur))
        set_gemini_hooks(new)
        if new != cur:
            self.save(hooks_dst, 'antigravity')
            print(f'  merge   {label(hooks_dst)}')
            if not self.dry:
                hooks_dst.write_text(json.dumps(new, indent=2) + '\n', encoding='utf-8', newline='\n')
        # 3. Lean shared skills for Antigravity (retire Orca-only issue-batch skill; preserve user skills)
        for name in GEMINI_SKILLS:
            src = REPO / 'skills' / name / 'SKILL.md'
            if src.is_file():
                self.place(src, GEMINI / 'skills' / name / 'SKILL.md', f'antigravity-skills/{name}')
        for name in GEMINI_RETIRED_SKILLS:
            skill_dir = GEMINI / 'skills' / name
            if skill_dir.is_symlink() or _is_junction(skill_dir):
                print(f'  remove  {label(skill_dir)}')
                if not self.dry:
                    if skill_dir.is_symlink():
                        try:
                            skill_dir.unlink()
                        except OSError:
                            skill_dir.rmdir()
                    else:
                        skill_dir.rmdir()
                continue
            dst = skill_dir / 'SKILL.md'
            try:
                exists = dst.is_file() or dst.is_symlink()
            except OSError:
                exists = False
            if exists:
                self.save(dst, f'antigravity-skills/{name}')
                print(f'  remove  {label(dst)}')
                if not self.dry:
                    try:
                        dst.parent.rmdir()
                    except OSError:
                        pass

    def run(self):
        print(f'Installing into {HOME}' + (' (dry run)' if self.dry else ''))
        self.place(REPO / 'CLAUDE.md', HOME / 'CLAUDE.md')
        self.retire_agents()
        self.retire_harnesses()
        self.install_agents()
        for src in sorted((REPO / 'hooks').glob('*.py')):
            self.place(src, HOME / 'hooks' / src.name)
        for name in RETIRED_TOOLS:
            self.save(HOME / 'tools' / name)
        for src in sorted((REPO / 'tools').glob('*.py')):
            self.place(src, HOME / 'tools' / src.name)
        for src in sorted((REPO / 'skills').glob('*/SKILL.md')):
            self.place(src, HOME / 'skills' / src.parent.name / 'SKILL.md')
        if not (HOME / 'machine.json').exists():
            print('  create  machine.json (edit it: where task folders go on this machine, what is never deleted)')
            if not self.dry:
                shutil.copy2(REPO / 'machine.example.json', HOME / 'machine.json')
        self.settings()
        self.settings('settings.local.json')
        personal = HOME / 'personal.md'
        if not personal.exists():
            print('  create  personal.md (edit it: your OS, project paths, extra rules)')
            if not self.dry:
                shutil.copy2(REPO / 'personal.example.md', personal)
        self.antigravity(personal if personal.exists() else REPO / 'personal.example.md')
        if self.saved:
            print(f'Backups in {self.backup}')
        print('Done. Start new agent sessions to reload the rules and settings.')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true', help='print changes without making them')
    Installer(ap.parse_args().dry_run).run()
