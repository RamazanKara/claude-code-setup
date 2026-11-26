#!/usr/bin/env python3
"""PreToolUse hook on the shell tools: Cargo builds in Rubylight go through its warm target pool.

In a checkout that has tools/build_lock.py, a command that builds with a bare `cargo` is denied: it leaves a
private target/ folder behind and makes executables at new paths, for which Windows Firewall asks again. A
command passes when it goes through tools/dev.py, tools\\dev.ps1, rust\\build.ps1, rl-env.ps1 or Enter-RlSlot,
or names its target folder itself (CARGO_TARGET_DIR, --target-dir). build_lock.py with a lane alone claims no
pool slot, so it does not pass. A command that carries POOL_SKIP=<reason>, as a trailing
`# POOL_SKIP=<reason>` comment in any shell, passes. On any error the command runs.
"""
import json, os, re, sys

from review_gate import MOVE, QUOTED, START, deny, git, local

BUILDING = 'build|test|check|clippy|nextest|run|bench|doc|rustc|b|c|t|r|d'  # the last five are Cargo's own short names
CARGO = re.compile(
    r'(?:' + START + r'|\s--\s+|\b(?:if|while)\s+)'
    r'(?:[A-Za-z_]\w*=\S*\s+|(?:time|env|nice|nohup|exec|timeout(?:\s+-\S+)*\s+\d+\w?)\s+)*'
    r'(?:[^\s;&|()]*[\\/])?cargo(?:\.exe)?'
    r'(?:\s+\+\S+|\s+(?:--color|--config|-Z|-C)\s+\S+|\s+-\S+)*'
    r'\s+(?:' + BUILDING + r')(?![\w-])(?P<rest>[^|;&\n)}]*)', re.I)
THROUGH = re.compile(r'tools[\\/]+dev\.(?:py|ps1)|rust[\\/]+build\.ps1|rl-env\.ps1|Enter-RlSlot|CARGO_TARGET_DIR\s*=|--target-dir\b', re.I)
SKIP = re.compile(r'POOL_SKIP\s*[=:]\s*[\'"]?([^\'";\n]+)', re.I)
# Text that is no command of this shell: here-documents, here-strings, comments and quoted strings.
BLOCKS = re.compile(r'<<-?[ \t]*([\'"]?)(\w+)\1[^\n]*\n.*?^[ \t]*\2[ \t]*$|@([\'"])[ \t]*\r?\n.*?^\3@', re.S | re.M)
TEXT = re.compile(r'(?<!\S)#[^\n]*|"(?:\\.|`.|[^"\\`])*"|\'[^\']*\'')


def bare(command):
    """The command without text. What a shell is handed with -c or -Command stays, and so does a quoted cargo path."""
    def text(m):
        inner = m.group(0)[1:-1]
        if m.group(0).startswith('#'):
            return ''
        if re.search(r'(?:\s-l?c|\s-Command)\s+$', m.string[:m.start()], re.I):
            return '; ' + inner + ' ;'
        if re.search(r'(?:^|[\\/])cargo(?:\.exe)?$', inner, re.I):
            return 'cargo'
        if re.search(r'(?:\b(?:cd|sl|pushd|Set-Location|Push-Location)|\s-(?:Literal)?Path|\s-C|--manifest-path)[\s=]+$', m.string[:m.start()], re.I):
            return m.group(0)  # a folder the command works in
        return '""' if re.search(r'[\s;&|]', inner) else inner
    command = re.sub(r'(?:\\|`)\r?\n', ' ', command)
    return TEXT.sub(text, BLOCKS.sub('""', command))


def pool_checkout(command, cwd):
    """The Rubylight checkout in which this command builds with Cargo, or None."""
    plain = bare(command)
    for m in CARGO.finditer(plain):
        where = local(cwd, '.')
        for move in MOVE.findall(plain[:m.start() + 1]):
            step = local(next(p for p in move if p), where)
            where = step if os.path.isdir(step) else where
        manifest = re.search(r'--manifest-path[=\s]+' + QUOTED, m.group('rest'))
        if manifest:
            folder = os.path.dirname(local(next(p for p in manifest.groups() if p), where))
            where = folder if os.path.isdir(folder) else where
        top = git(where, 'rev-parse', '--show-toplevel')
        if top and os.path.isfile(os.path.join(top, 'tools', 'build_lock.py')):
            return top
    return None


def main():
    event = json.load(sys.stdin)
    command = str((event.get('tool_input') or {}).get('command') or '')
    if 'cargo' not in command.lower() or THROUGH.search(command) or SKIP.search(command):
        return
    if pool_checkout(command, event.get('cwd') or os.getcwd()):
        deny('Cargo build held back: run it as `python tools/dev.py cargo -- <args>`, which claims a warm target-pool '
             'slot (stable exe paths keep the existing Windows Firewall allow rules, and no private target/ folder is '
             'left behind), or `tools\\dev.ps1 check`. If it must run as it is, repeat it with the comment '
             '`# POOL_SKIP=<reason>` at the end of the command.')


if __name__ == '__main__':
    try:
        main()
    except Exception:  # a hook must never break the session or hold a command it could not judge
        pass
