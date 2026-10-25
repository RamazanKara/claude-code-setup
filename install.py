#!/usr/bin/env python3
"""Install this setup into ~/.claude.

Copies CLAUDE.md and the agents, merges settings.json into your existing one,
and creates ~/.claude/personal.md from the example once. Anything it replaces
is backed up to ~/.claude/backups/claude-code-setup-<time>/ first.

  python install.py            # install or update
  python install.py --link     # symlink instead of copy, so `git pull` updates you
  python install.py --dry-run  # show what would change
"""
import argparse, datetime, filecmp, json, os, shutil, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent / 'claude'
HOME = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')


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


class Installer:
    def __init__(self, link, dry):
        self.link, self.dry, self.saved = link, dry, False
        self.backup = HOME / 'backups' / ('claude-code-setup-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))

    def save(self, dst):
        if not dst.exists() and not dst.is_symlink():
            return
        rel = dst.relative_to(HOME)
        print(f'  backup  {rel}')
        self.saved = True
        if not self.dry:
            if dst.exists():
                (self.backup / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, self.backup / rel)
            dst.unlink()

    def place(self, src, dst):
        if self.link and dst.is_symlink() and dst.resolve() == src.resolve():
            return
        if not self.link and dst.is_file() and not dst.is_symlink() and filecmp.cmp(src, dst, shallow=False):
            return
        self.save(dst)
        print(f'  {"link" if self.link else "copy"}    {dst.relative_to(HOME)}')
        if self.dry:
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        if self.link:
            try:
                dst.symlink_to(src)
            except OSError as e:
                sys.exit(f'cannot symlink {dst}: {e}\nOn Windows, enable Developer Mode or run elevated, or drop --link.')
        else:
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

    def run(self):
        print(f'Installing into {HOME}' + (' (dry run)' if self.dry else ''))
        self.place(REPO / 'CLAUDE.md', HOME / 'CLAUDE.md')
        for src in sorted((REPO / 'agents').glob('*.md')):
            self.place(src, HOME / 'agents' / src.name)
        self.settings()
        personal = HOME / 'personal.md'
        if not personal.exists():
            print('  create  personal.md (edit it: your OS, project paths, extra rules)')
            if not self.dry:
                shutil.copy2(REPO / 'personal.example.md', personal)
        if self.saved:
            print(f'Backups in {self.backup}')
        print('Done. Restart Claude Code (agents reload on the next message).')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--link', action='store_true', help='symlink files instead of copying')
    ap.add_argument('--dry-run', action='store_true', help='print changes without making them')
    a = ap.parse_args()
    Installer(a.link, a.dry_run).run()
