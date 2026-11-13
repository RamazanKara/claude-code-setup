#!/usr/bin/env python3
"""Install this setup into ~/.claude and the other installed agents.

Copies CLAUDE.md, optional helper agents and the read-streak hook, merges settings.json into
your existing one, and creates ~/.claude/personal.md from the example once. The same rules, with
personal.md inlined, are written for every other installed agent: Codex
(~/.codex/AGENTS.md), OpenCode (~/.config/opencode/AGENTS.md) and Antigravity
(~/.gemini/GEMINI.md). opencode/opencode.json is merged into OpenCode's global
config. Retired helper definitions, deny rules and workflow overrides are removed.
Anything it replaces is backed up to ~/.claude/backups/claude-code-setup-<time>/ first.

Orca's Claude profiles set CLAUDE_CONFIG_DIR and import ~/.claude/CLAUDE.md,
so the installer ignores that variable when it points into an Orca profile.

  python install.py            # install, or update after `git pull` or editing personal.md
  python install.py --dry-run  # show what would change
"""
import argparse, datetime, filecmp, hashlib, json, os, re, shutil, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT / 'claude'
_cfg = os.environ.get('CLAUDE_CONFIG_DIR')
HOME = Path(_cfg) if _cfg and 'claude-profiles' not in Path(_cfg).parts else Path.home() / '.claude'
OPENCODE = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'opencode'
SHARED = {
    'codex': Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'AGENTS.md',
    'opencode': OPENCODE / 'AGENTS.md',
    'antigravity': Path.home() / '.gemini' / 'GEMINI.md',
}
# Only remove the definitions shipped by this setup; preserve user replacements.
RETIRED_AGENTS = {
    'claude/architect.md': '9542ffe407ed1894aeddf7549a4f741616ea7fa3076d70a12e42f622ee9218a2',
    'claude/designer.md': 'c77acd7c49f9dc8b2ab05262dc7ec15e2b5e13f3247719703a17796dd16da269',
    'claude/implementer.md': 'ef1e6edbf9a7fc10760df32522ec101751ba0aa21bd3f6d7d024b6e3b66b3479',
    'claude/reviewer.md': 'f95a910b92760caf14010455f749de6936ace734bd5ae268fb7448520d1d37a3',
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


def clean_permissions(config):
    for key in ('permission', 'permissions'):
        if key in config:
            if config[key] == 'deny':
                del config[key]
                continue
            config[key] = without_denies(config[key])
            if not config[key]:
                del config[key]


def clean_claude(config):
    clean_permissions(config)
    overrides = config.get('skillOverrides', {})
    for skill in ('built-in-browser', 'chrome-browser', 'computer-use', 'docs'):
        if overrides.get(skill) == 'off':
            del overrides[skill]
    if not overrides:
        config.pop('skillOverrides', None)


def set_hooks(config):
    """Add this setup's hook to settings, replacing an earlier entry for the same script."""
    script = HOME / 'hooks' / 'read_streak.py'
    entry = {'hooks': [{'type': 'command', 'command': Path(sys.executable).as_posix(),
                        'args': [script.as_posix()], 'timeout': 10}]}
    groups = config.setdefault('hooks', {}).setdefault('PostToolBatch', [])
    groups[:] = [g for g in groups if script.name not in json.dumps(g)] + [entry]


def clean_opencode(config):
    clean_permissions(config)
    agents = config.get('agents', {})
    for name in ('explore', 'title', 'summary'):
        if agents.get(name, {}).get('model') == 'opencode-go/deepseek-v4.1-flash':
            del agents[name]['model']
            if not agents[name]:
                del agents[name]
    if agents.get('general', {}).get('disabled') is True:
        del agents['general']['disabled']
        if not agents['general']:
            del agents['general']
    if not agents:
        config.pop('agents', None)
    opus = config.get('providers', {}).get('anthropic', {}).get('models', {}).get('claude-opus-5-5', {})
    if opus.get('settings') == {'thinking': {'type': 'adaptive', 'display': 'summarized'}, 'effort': 'medium'}:
        del opus['settings']


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


def shared_rules(personal):
    """CLAUDE.md for agents without @imports: drop import lines, inline personal.md."""
    rules = [l for l in (REPO / 'CLAUDE.md').read_text(encoding='utf-8').splitlines() if not l.startswith('@')]
    text = '\n'.join(rules).rstrip() + '\n'
    if personal.exists():
        text += '\n' + personal.read_text(encoding='utf-8').strip() + '\n'
    return text


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

    def shared(self, personal):
        text = shared_rules(personal)
        for name, dst in SHARED.items():
            if not dst.parent.is_dir():
                continue
            if dst.is_file() and not dst.is_symlink() and dst.read_text(encoding='utf-8') == text:
                continue
            self.save(dst, name)
            print(f'  write   {label(dst)}')
            if not self.dry:
                dst.write_text(text, encoding='utf-8', newline='\n')

    def retire_agents(self):
        for name, digest in RETIRED_AGENTS.items():
            harness, filename = name.split('/')
            dst = (HOME if harness == 'claude' else OPENCODE) / 'agents' / filename
            if dst.is_file() and hashlib.sha256(dst.read_text(encoding='utf-8').encode()).hexdigest() == digest:
                self.save(dst, harness + '-agents')
                print(f'  remove  {label(dst)}')

    def install_agents(self):
        targets = [('claude', HOME, HOME / 'agents'), ('opencode', OPENCODE, OPENCODE / 'agents')]
        if 'codex' in SHARED:
            targets.append(('codex', SHARED['codex'].parent, SHARED['codex'].parent / 'agents'))
        if 'antigravity' in SHARED:
            targets.append(('antigravity', SHARED['antigravity'].parent, SHARED['antigravity'].parent / 'config' / 'agents'))
        for harness, directory, agents in targets:
            if not directory.is_dir():
                continue
            for src in sorted((ROOT / harness / 'agents').glob('*.*')):
                self.place(src, agents / src.name, harness + '-agents')

    def opencode(self):
        if not OPENCODE.is_dir():
            return
        dst = next((OPENCODE / n for n in ('opencode.jsonc', 'opencode.json') if (OPENCODE / n).is_file()), OPENCODE / 'opencode.jsonc')
        try:
            cur = read_json(dst) if dst.is_file() else {}
        except json.JSONDecodeError:
            raise ValueError(f'Invalid OpenCode config: {dst}')
        new = merge(json.loads(json.dumps(cur)), read_json(ROOT / 'opencode' / 'opencode.json'))
        clean_opencode(new)
        if new == cur:
            return
        self.save(dst, 'opencode')
        print(f'  merge   {dst}')
        if not self.dry:
            dst.write_text(json.dumps(new, indent=2) + '\n', encoding='utf-8', newline='\n')

    def run(self):
        print(f'Installing into {HOME}' + (' (dry run)' if self.dry else ''))
        self.place(REPO / 'CLAUDE.md', HOME / 'CLAUDE.md')
        self.retire_agents()
        self.install_agents()
        for src in sorted((REPO / 'hooks').glob('*.py')):
            self.place(src, HOME / 'hooks' / src.name)
        if OPENCODE.is_dir():  # the tools hand work to OpenCode
            for src in sorted((REPO / 'tools').glob('*.py')):
                self.place(src, HOME / 'tools' / src.name)
        self.settings()
        self.settings('settings.local.json')
        personal = HOME / 'personal.md'
        if not personal.exists():
            print('  create  personal.md (edit it: your OS, project paths, extra rules)')
            if not self.dry:
                shutil.copy2(REPO / 'personal.example.md', personal)
        self.shared(personal if personal.exists() else REPO / 'personal.example.md')
        self.opencode()
        if self.saved:
            print(f'Backups in {self.backup}')
        print('Done. Start new agent sessions to reload the rules and settings.')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true', help='print changes without making them')
    Installer(ap.parse_args().dry_run).run()
