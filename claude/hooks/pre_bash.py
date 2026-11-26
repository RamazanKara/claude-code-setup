#!/usr/bin/env python3
"""Combined dual-harness PreToolUse dispatcher for shell tools (`Bash|PowerShell` in Claude Code, `run_command` in Antigravity).

Runs `cargo_pool.check()` (warm target pools, Gradle `--no-daemon`, firewall-safe dev servers)
and `review_gate.push()` in ONE Python process. Emits Antigravity's `{"decision": "allow"|"deny"}`
contract for camelCase payloads and Claude Code's `hookSpecificOutput` (silent on allow) for
snake_case payloads.
"""
import json, os, sys

import cargo_pool, review_gate


def main():
    event = json.load(sys.stdin)
    try:
        why = cargo_pool.check(event)
        if why:
            review_gate.deny(why, event)
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
            review_gate.log(decision='error', error=repr(error)[:200])
        except Exception:
            pass
