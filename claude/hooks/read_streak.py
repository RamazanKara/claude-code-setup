#!/usr/bin/env python3
"""PostToolBatch hook: a nudge after several read-only turns of one call each.

Every model turn re-reads the whole context, so five reads in five turns cost
about five times what one turn with five calls does. This hook counts turns in
a row that made a single read-only call (Read, Grep, Glob, or a shell command
that only reads) and, at the third and then every sixth, adds one line telling
the model to batch or delegate. It never blocks and never changes a call.

CLAUDE_READ_STREAK sets the first nudge (default 3); 0 turns the hook off.
The cheap helper agents are skipped: reading is their job.
"""
import json, os, re, sys, tempfile, time

HELPERS = {'scout', 'runner', 'mechanic'}
REPEAT = 6
CMD = r'(?:^|[;&|({=]\s*|\n\s*)(?:\.[\\/])?'  # where a command name can start, so a path that contains one is not a call
READS = re.compile(r'Get-Content|Select-String|Get-ChildItem|\b(cat|head|tail|gc|grep|rg|findstr|ls|dir)\b'
                   r'|\bgit\b[^|;&]*\b(diff|show|log|blame|status|grep|ls-files)\b', re.I)
ACTS = re.compile(CMD + r'(?:cargo|gradlew?|npm|pytest|gh|orca|adb|ssh|python3?|py|node)(?:\.exe)?\b|\.ps1\b'
                  r'|(?:Remove|Move|Copy|New)-Item|(?:Set|Add)-Content|Out-File'
                  r'|\bgit\b[^|;&]*\b(commit|push|rebase|add|merge|checkout|fetch|worktree|reset|stash)\b', re.I)


def read_only(call):
    name = call.get('tool_name')
    if name in ('Read', 'Grep', 'Glob'):
        return True
    if name in ('Bash', 'PowerShell'):
        command = str((call.get('tool_input') or {}).get('command', ''))
        return bool(READS.search(command)) and not ACTS.search(command)
    return False


def streak_after(event, folder):
    """Update the stored streak for this session and agent; return the new value."""
    key = re.sub(r'[^A-Za-z0-9-]', '', f"{event.get('session_id', '')}-{event.get('agent_id', 'main')}")
    path = os.path.join(folder, key)
    calls = event.get('tool_calls') or []
    if len(calls) != 1 or not read_only(calls[0]):
        try:
            os.unlink(path)
        except OSError:
            pass
        return 0
    try:
        with open(path, encoding='ascii') as f:
            streak = int(f.read()) + 1
    except (OSError, ValueError):
        streak = 1
    os.makedirs(folder, exist_ok=True)
    with open(path, 'w', encoding='ascii') as f:
        f.write(str(streak))
    if streak == 1 and int(time.time()) % 50 == 0:  # now and then, drop what ended sessions left behind
        for name in os.listdir(folder):
            old = os.path.join(folder, name)
            if time.time() - os.path.getmtime(old) > 86400:
                os.unlink(old)
    return streak


def main():
    first = int(os.environ.get('CLAUDE_READ_STREAK', '3') or 0)
    event = json.load(sys.stdin)
    if first <= 0 or event.get('agent_type') in HELPERS:
        return
    streak = streak_after(event, os.path.join(tempfile.gettempdir(), 'claude-read-streak'))
    if streak < first or (streak - first) % REPEAT:
        return
    where = '`scout`' if event.get('agent_id') else '`scout` or a DeepSeek worker'
    note = (f'{streak} read-only turns in a row with one call each, and every turn re-reads the whole context. '
            f'Put the next independent reads and searches into one turn, or hand a longer survey to {where}.')
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PostToolBatch', 'additionalContext': note}}))


if __name__ == '__main__':
    try:
        main()
    except Exception:  # a hook must never break the session it watches
        pass
