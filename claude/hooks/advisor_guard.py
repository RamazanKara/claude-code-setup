#!/usr/bin/env python3
"""SubagentStart hook: tells every subagent not to call the advisor tool.

Claude Code gives subagents the advisor too, and neither `tools:` nor `disallowedTools`
in an agent's definition takes it away (probed 2026-11-19). The rule in CLAUDE.md did not
reach them: after it landed, subagents still made most of the advisor calls, while the
reviewer, whose own prompt says no, made none. Each call re-reads the subagent's whole
transcript on the advisor model and draws on that model's weekly allowance, which the
user's main chats need. So the line goes into every subagent's context as it starts.

A brief that asks for the advisor in so many words still gets it.
CLAUDE_ADVISOR_GUARD=0 turns the hook off.
"""
import json, os, sys

NOTE = ('You are a subagent: do not call the advisor tool unless your brief explicitly asks you to. '
        'The agent that briefed you, or the reviewer, is the independent check, and each advisor call '
        're-reads your whole transcript and draws on the advisor model\'s weekly allowance.')


def main():
    off = os.environ.get('CLAUDE_CODE_DISABLE_ADVISOR_TOOL', '').strip().lower() in ('1', 'true', 'yes', 'on')
    if os.environ.get('CLAUDE_ADVISOR_GUARD', '1') == '0' or off:
        return
    event = json.loads(sys.stdin.buffer.read().decode('utf-8', 'replace'))
    if event.get('hook_event_name', 'SubagentStart') != 'SubagentStart':
        return
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SubagentStart', 'additionalContext': NOTE}}))


if __name__ == '__main__':
    try:
        main()
    except Exception:  # a hook must never break the session it serves
        pass
