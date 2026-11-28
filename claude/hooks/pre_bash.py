#!/usr/bin/env python3
"""Combined dual-harness PreToolUse dispatcher for shell tools (`Bash|PowerShell` in Claude Code, `run_command` in Antigravity).

Runs `cargo_pool.check()` (warm target pools, Gradle `--no-daemon`, firewall-safe dev servers)
and, unless `--pool-only` is passed, `review_gate.push()` in ONE Python process. Emits
Antigravity's `{"decision": "allow"|"deny"}` contract for camelCase payloads and Claude Code's
`hookSpecificOutput` (silent on allow) for snake_case payloads.
"""
import json, os, sys

POOL_TRIGGERS = ('cargo', 'gradlew', 'http.server', '--host')
ALL_TRIGGERS = (*POOL_TRIGGERS, 'push')


def _is_antigravity(event):
    return isinstance(event, dict) and any(k in event for k in ('toolCall', 'hookEventName', 'conversationId', 'workspacePaths'))


def _extract_command(event):
    if not isinstance(event, dict):
        return ''
    if 'toolCall' in event:
        args = (event.get('toolCall') or {}).get('args') or {}
        return str(args.get('CommandLine') or args.get('command') or '')
    return str((event.get('tool_input') or {}).get('command') or '')


def main():
    pool_only = '--pool-only' in sys.argv[1:]
    event = json.load(sys.stdin)
    low = _extract_command(event).lower()
    triggers = POOL_TRIGGERS if pool_only else ALL_TRIGGERS
    if not any(token in low for token in triggers):
        if _is_antigravity(event):
            print('{"decision": "allow"}')
        return
    import cargo_pool, review_gate
    try:
        why = cargo_pool.check(event)
        if why:
            review_gate.deny(why, event)
            return
        if pool_only:
            review_gate.allow(event)
            return
        least = int(os.environ.get('CLAUDE_REVIEW_MIN_LINES', '150') or 0)
        if not review_gate.push(event, least):
            review_gate.allow(event)
    except Exception as error:
        try:
            review_gate.log(decision='error', error=repr(error)[:200])
        except Exception:
            pass
        review_gate.allow(event)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:  # malformed stdin: never block a command
        try:
            import review_gate
            review_gate.log(decision='error', error=repr(error)[:200])
        except Exception:
            pass
