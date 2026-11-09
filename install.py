#!/usr/bin/env python3
"""Install this setup into ~/.claude and the other installed agents.

Copies CLAUDE.md and the agents, merges settings.json into your existing one,
and creates ~/.claude/personal.md from the example once. The same rules, with
personal.md inlined, are written for every other installed agent: Codex
(~/.codex/AGENTS.md), OpenCode (~/.config/opencode/AGENTS.md) and Antigravity
(~/.gemini/GEMINI.md). opencode/opencode.json is merged into OpenCode's global
config and opencode/agents/ holds the DeepSeek helper agents every agent can
call through `opencode run`; the Haiku Claude agents they replace are removed.
Anything it replaces is backed up to
~/.claude/backups/claude-code-setup-<time>/ first.

Orca's Claude profiles set CLAUDE_CONFIG_DIR and import ~/.claude/CLAUDE.md,
so the installer ignores that variable when it points into an Orca profile.

  python install.py            # install, or update after `git pull` or editing personal.md
  python install.py --dry-run  # show what would change
"""
import argparse, datetime, filecmp, json, os, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT / 'claude'
_cfg = os.environ.get('CLAUDE_CONFIG_DIR')
HOME = Path(_cfg) if _cfg and 'claude-profiles' not in Path(_cfg).parts else Path.home() / '.claude'
OPENCODE = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'opencode'
# Haiku-tier Claude agents replaced by the DeepSeek helpers in opencode/agents.
RETIRED_AGENTS = ('mechanic', 'runner', 'scout')
SHARED = {
    'codex': Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'AGENTS.md',
    'opencode': OPENCODE / 'AGENTS.md',
    'antigravity': Path.home() / '.gemini' / 'GEMINI.md',
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

    def settings(self):
        dst = HOME / 'settings.json'
        cur = json.loads(dst.read_text(encoding='utf-8')) if dst.exists() else {}
        new = merge(json.loads(json.dumps(cur)), json.loads((REPO / 'settings.json').read_text(encoding='utf-8')))
        if new == cur:
            return
        self.save(dst)
        print('  merge   settings.json')
        if not self.dry:
            dst.write_text(json.dumps(new, indent=2) + '\n', encoding='utf-8')

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

    def opencode(self):
        if not OPENCODE.is_dir():
            return
        for src in sorted((ROOT / 'opencode' / 'agents').glob('*.md')):
            self.place(src, OPENCODE / 'agents' / src.name, 'opencode-agents')
        dst = next((OPENCODE / n for n in ('opencode.jsonc', 'opencode.json') if (OPENCODE / n).is_file()), OPENCODE / 'opencode.jsonc')
        try:
            cur = json.loads(dst.read_text(encoding='utf-8')) if dst.is_file() else {}
        except json.JSONDecodeError:
            print(f'  skip    {dst} (not plain JSON; merge opencode/opencode.json by hand)')
            return
        new = merge(json.loads(json.dumps(cur)), json.loads((ROOT / 'opencode' / 'opencode.json').read_text(encoding='utf-8')))
        if new == cur:
            return
        self.save(dst, 'opencode')
        print(f'  merge   {dst}')
        if not self.dry:
            dst.write_text(json.dumps(new, indent=2) + '\n', encoding='utf-8', newline='\n')

    def run(self):
        print(f'Installing into {HOME}' + (' (dry run)' if self.dry else ''))
        self.place(REPO / 'CLAUDE.md', HOME / 'CLAUDE.md')
        for src in sorted((REPO / 'agents').glob('*.md')):
            self.place(src, HOME / 'agents' / src.name)
        for name in RETIRED_AGENTS:
            dst = HOME / 'agents' / f'{name}.md'
            if dst.is_file() and not (REPO / 'agents' / dst.name).exists():
                self.save(dst)
                print(f'  remove  {label(dst)}')
        self.settings()
        personal = HOME / 'personal.md'
        if not personal.exists():
            print('  create  personal.md (edit it: your OS, project paths, extra rules)')
            if not self.dry:
                shutil.copy2(REPO / 'personal.example.md', personal)
        self.shared(personal if personal.exists() else REPO / 'personal.example.md')
        self.opencode()
        if self.saved:
            print(f'Backups in {self.backup}')
        print('Done. Restart Claude Code (agents reload on the next message).')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true', help='print changes without making them')
    Installer(ap.parse_args().dry_run).run()
