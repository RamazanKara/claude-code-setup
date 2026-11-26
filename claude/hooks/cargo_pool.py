#!/usr/bin/env python3
"""PreToolUse hook on the shell tools: warm target pools, Gradle daemon safety, and firewall hygiene.

1. Cargo builds in Rubylight (`tools/dev_cargo.py` present) go through its warm target pool
   (`python tools/dev.py cargo -- <args>` or `tools\\dev.ps1 check`), keeping stable exe paths for
   Windows Firewall rules and leaving no private target/ folder behind.
2. Direct `gradlew` runs in Rubylight (`tools/build_lock.py` present) go through the shared Gradle
   lane with `--no-daemon` so leaked 4+ GB Gradle daemons never pile up.
3. Ad-hoc local dev servers (`python -m http.server`, `vite --host`) must bind loopback
   (`127.0.0.1` / `localhost` / `::1`) so Windows Defender Firewall never pops up modal prompts.

A command carrying `# POOL_SKIP=<reason>` passes. On any error the command runs.
"""
import json, os, re, sys

from review_gate import MOVE, QUOTED, START, allow, deny, extract_command, extract_cwd, git, local

BUILDING = 'build|test|check|clippy|nextest|run|bench|doc|rustc|b|c|t|r|d'  # the last five are Cargo's own short names
# Options that may stand before the subcommand and take the next word with them.
VALUED = '--color|--config|--manifest-path|--lockfile-path|--target-dir|--target|--jobs|--package|--profile|--features|--message-format|-Z|-C|-j|-p|-F'
CARGO = re.compile(
    r'(?:' + START + r'|\s--\s+|\b(?:if|while)\s+)'
    r'(?:\$?[A-Za-z_][\w:.]*(?:\s*\+=|\s+=|=(?=\s))\s*)?'  # `$out = cargo build`
    r'(?:[A-Za-z_]\w*=\S*\s+|(?:time|env|nice|nohup|exec|timeout(?:\s+-\S+)*\s+\d+\w?)\s+)*'
    r'(?:[^\s;&|()]*[\\/])?(?P<cargo>cargo)(?:\.exe)?'
    r'(?:\s+\+\S+|\s+(?:' + VALUED + r')(?:\s+|=)(?:"[^"]*"|\'[^\']*\'|[^\s;&|]+)|\s+-\S+)*'
    r'\s+(?:' + BUILDING + r')(?![\w-])[^|;&\n)}]*', re.I)
SCRIPTS = r'tools[\\/]+dev\.(?:py|ps1)|rust[\\/]+build\.ps1|rl-env\.ps1'
THROUGH = re.compile(SCRIPTS + r'|Enter-RlSlot|CARGO_TARGET_DIR\s*=|--target-dir\b', re.I)
# What also holds for the later parts of a command line.
LASTING = re.compile(r'rl-env\.ps1|Enter-RlSlot|\$env:CARGO_TARGET_DIR\s*=|\b(?:export|set)\s+(?:\S+\s+)*CARGO_TARGET_DIR=|^\s*CARGO_TARGET_DIR=\S*\s*$', re.I)
PARTS = re.compile(r'&&|\|\||(?<!>)\||[;\n]')
SKIP = re.compile(r'POOL_SKIP\s*[=:]\s*[\'"]?([^\'";\n]+)', re.I)
# Text that is no command of this shell: here-documents, here-strings, comments and quoted strings.
BLOCKS = re.compile(r'<<-?[ \t]*([\'"]?)(\w+)\1[^\n]*\n.*?^[ \t]*\2[ \t]*$|@([\'"])[ \t]*\r?\n.*?^\3@', re.S | re.M)
TEXT = re.compile(r'(?<!\S)#[^\n]*|"(?:\\.|`.|[^"\\`])*"|\'[^\']*\'')

GRADLEW = re.compile(
    r'(?:' + START + r'|\s--\s+|\b(?:if|while)\s+|\bcmd(?:\.exe)?\s+/[ck]\s+|&\s+)'
    r'(?:[A-Za-z_]\w*=\S*\s+|(?:time|env|nice|nohup|exec)\s+)*'
    r'(?:[^\s;&|()]*[\\/])?gradlew(?:\.bat)?\b', re.I)
GRADLE_LOCKED = re.compile(r'\btools[\\/]+build_lock\.py\b[^\n;|&]*(?:--lane\s+gradle\b|--lock-directory\b)', re.I)
GRADLE_NO_DAEMON = re.compile(r'--no-daemon\b', re.I)
DEV_TOOL = re.compile(r'\btools[\\/]+dev\.(?:ps1|py)\b', re.I)

LOOPBACK = r'(?:127\.0\.0\.1|localhost|\[::1\]|::1)'
WILDCARD_HTTP_SERVER = re.compile(
    r'\b(?:python3?|py)(?:\.exe)?\b[^\n;|&]*\s-m\s+http\.server\b(?![^\n;|&]*(?:-b|--bind)(?:\s+|=)' + LOOPBACK + r'(?=$|[\s;&|<>]))',
    re.I)
WILDCARD_VITE_HOST = re.compile(
    r'\b(?:vite|npm(?:\.cmd)?\s+run\s+(?:dev|preview))\b[^\n;|&]*--host(?!(?:\s+|=)' + LOOPBACK + r'(?=$|[\s;&|<>]))(?=$|[\s=;&|<>])',
    re.I)


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
        script = re.search(r'(?:' + SCRIPTS + r')$', inner, re.I)
        if script:
            return script.group(0)
        return '""' if re.search(r'[\s;&|]', inner) else inner
    command = re.sub(r'(?:\\|`)\r?\n', ' ', command)
    return TEXT.sub(text, BLOCKS.sub('""', command))


def _resolve_repo(plain, offset, cwd):
    where = local(cwd, '.')
    for move in MOVE.findall(plain[:offset + 1]):
        step = local(next(p for p in move if p), where)
        where = step if os.path.isdir(step) else where
    return git(where, 'rev-parse', '--show-toplevel')


def pool_checkout(command, cwd):
    """The Rubylight checkout in which this command builds with Cargo, or None."""
    plain = bare(command)
    for m in CARGO.finditer(plain):
        at = m.start('cargo')
        *before, part = PARTS.split(plain[:at])
        if THROUGH.search(part + PARTS.split(plain[at:])[0]) or any(LASTING.search(p) for p in before):
            continue
        where = local(cwd, '.')
        for move in MOVE.findall(plain[:m.start() + 1]):
            step = local(next(p for p in move if p), where)
            where = step if os.path.isdir(step) else where
        manifest = re.search(r'--manifest-path[=\s]+' + QUOTED, m.group(0))
        if manifest:
            folder = os.path.dirname(local(next(p for p in manifest.groups() if p), where))
            where = folder if os.path.isdir(folder) else where
        top = git(where, 'rev-parse', '--show-toplevel')
        if top and os.path.isfile(os.path.join(top, 'tools', 'dev_cargo.py')):
            return top
    return None


def gradle_violation(command, cwd):
    """True when a direct gradlew call in a Rubylight checkout skips the Gradle lane or --no-daemon."""
    plain = bare(command)
    for part in PARTS.split(plain):
        m = GRADLEW.search(part)
        if not m or DEV_TOOL.search(part):
            continue
        if GRADLE_LOCKED.search(part) and GRADLE_NO_DAEMON.search(part):
            continue
        top = _resolve_repo(plain, plain.find(part), cwd)
        if top and os.path.isfile(os.path.join(top, 'tools', 'build_lock.py')):
            return True
    return False


def firewall_violation(command):
    """Reason string if a shell segment starts an ad-hoc server on 0.0.0.0, else None."""
    plain = bare(command)
    for part in PARTS.split(plain):
        if WILDCARD_HTTP_SERVER.search(part):
            return ('HTTP server held back: pass `--bind 127.0.0.1` to `python -m http.server` so Windows '
                    'Defender Firewall does not prompt. Repeat with `# POOL_SKIP=<reason>` if LAN bind is required.')
        if WILDCARD_VITE_HOST.search(part):
            return ('Dev server held back: pass `--host 127.0.0.1` instead of bare `--host` or `0.0.0.0` so '
                    'Windows Defender Firewall does not prompt. Repeat with `# POOL_SKIP=<reason>` if LAN bind is required.')
    return None


def check(event):
    """Return a denial reason string if blocked, else None."""
    command = extract_command(event)
    if not command or SKIP.search(command):
        return None
    low = command.lower()
    cwd = extract_cwd(event)
    if 'cargo' in low and pool_checkout(command, cwd):
        return ('Cargo build held back: run it as `python tools/dev.py cargo -- <args>`, which claims a warm target-pool '
                'slot (stable exe paths keep the existing Windows Firewall allow rules, and no private target/ folder is '
                'left behind), or `tools\\dev.ps1 check`. If it must run as it is, repeat it with the comment '
                '`# POOL_SKIP=<reason>` at the end of the command.')
    if 'gradlew' in low and gradle_violation(command, cwd):
        return ('Gradle build held back: wrap direct `gradlew` runs in the shared Gradle lane with `--no-daemon`: '
                '`python tools/build_lock.py --lane gradle --label <name> -- cmd /c client\\gradlew.bat -p client '
                '--no-daemon --max-workers=2 <task>` (or `tools\\dev.ps1 check`). Repeat with `# POOL_SKIP=<reason>` to bypass.')
    if 'http.server' in low or '--host' in low:
        why = firewall_violation(command)
        if why:
            return why
    return None


def main():
    event = json.load(sys.stdin)
    why = check(event)
    if why:
        deny(why, event)
    else:
        allow(event)


if __name__ == '__main__':
    try:
        main()
    except Exception:  # a hook must never break the session or hold a command it could not judge
        pass
