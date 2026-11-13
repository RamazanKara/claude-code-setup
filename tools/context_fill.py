#!/usr/bin/env python3
"""What fills Claude Code contexts and where the turns go, from your local transcripts.

For the main chat and each agent type it prints:
- what entered the context: tool results by tool and the text of edits, in tokens (chars / 4),
  and how much of it was whole-file reads of long files;
- which kinds of shell command produced the shell output;
- turns: tool calls per turn, turns that only read, and the spend in streaks of turns that made
  one read-only call each, which is what claude/hooks/read_streak.py counts.

  python tools/context_fill.py            # last 4 days
  python tools/context_fill.py --days 7

Costs use cost_report.py's list prices: an estimate for comparing habits, not a bill.
"""
import argparse, collections, datetime, glob, json, os, re, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'claude', 'hooks'))
import cost_report as cr
from read_streak import CMD, read_only

SHELL = [(kind, re.compile(rx, re.I)) for kind, rx in (  # first match wins
    ('build and test', CMD + r'(?:cargo|gradlew?|npm|pytest|msbuild|cmake|ninja)\b|\.ps1\b|unittest|node --test'),
    ('git diff, show, log', r'\bgit\b[^|;&]*\b(diff|show|log|blame)\b'),
    ('git other', CMD + r'git\b'),
    ('gh', CMD + r'gh\b'),
    ('orca', CMD + r'orca\b'),
    ('script (python, node)', CMD + r'(?:python3?|py|node)\b'),
    ('file dump (Get-Content, cat, head, tail)', r'Get-Content|\b(cat|head|tail|gc)\b'),
    ('search (grep, rg, Select-String)', r'Select-String|\b(grep|rg|findstr)\b'),
    ('listing', r'Get-ChildItem|\b(ls|dir|find)\b'),
)]
STREAKS = [(2, '1-2'), (4, '3-4'), (9, '5-9'), (10 ** 9, '10+')]
LONG = (350, 1000)


def size(content):
    """Characters and lines of a tool result's text."""
    texts = [content] if isinstance(content, str) else [b.get('text', '') for b in content or [] if isinstance(b, dict)]
    return sum(map(len, texts)), sum(t.count('\n') + 1 for t in texts)


def scan(path, since):
    """One transcript: its priced turns as (cost, tool calls), result sizes by source, long whole-file reads, shell output by kind."""
    turns, calls, fill, long_reads, shell = {}, {}, collections.Counter(), collections.Counter(), collections.Counter()
    with open(path, encoding='utf-8', errors='ignore') as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            m = e.get('message')
            ts = e.get('timestamp')
            if not isinstance(m, dict) or (ts and datetime.datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp() < since):
                continue
            blocks = [b for b in m.get('content') or [] if isinstance(b, dict)] if isinstance(m.get('content'), list) else []
            if e.get('type') == 'assistant' and m.get('id'):
                turn = turns.setdefault(m['id'], [str(m.get('model', '')), None, []])
                turn[1] = m.get('usage') or turn[1]
                for b in blocks:
                    if b.get('type') == 'tool_use':
                        call = {'tool_name': b.get('name'), 'tool_input': b.get('input') or {}}
                        calls[b.get('id')] = call
                        turn[2].append(call)
                        if call['tool_name'] in ('Edit', 'Write', 'NotebookEdit'):
                            fill['edit and write text'] += len(json.dumps(call['tool_input']))
            for b in blocks:
                call = calls.get(b.get('tool_use_id')) if b.get('type') == 'tool_result' else None
                if not call:
                    continue
                name, args = call['tool_name'], call['tool_input']
                chars, lines = size(b.get('content'))
                if name == 'Read':
                    whole = not any(k in args for k in ('offset', 'limit', 'pages'))
                    fill['Read, whole file' if whole else 'Read, a range'] += chars
                    for limit in LONG:
                        long_reads[limit] += chars if whole and lines > limit else 0
                elif name in ('Bash', 'PowerShell'):
                    fill['shell output'] += chars
                    command = str(args.get('command', ''))
                    shell[next((kind for kind, rx in SHELL if rx.search(command)), 'other')] += chars
                else:
                    fill['Agent report' if name in ('Agent', 'Task') else 'MCP tools' if str(name).startswith('mcp__') else name] += chars
    priced = [(sum(c.values()), t[2]) for t in turns.values() if t[1] for c in [cr.cost(t[0], t[1])] if c]
    return priced, fill, long_reads, shell


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--days', type=float, default=4)
    ap.add_argument('--root', default=os.path.join(os.environ.get('CLAUDE_CONFIG_DIR') or os.path.expanduser('~/.claude'), 'projects'))
    a = ap.parse_args()
    since = (datetime.datetime.now() - datetime.timedelta(days=a.days)).timestamp()
    groups = collections.defaultdict(lambda: {'cost': 0.0, 'turns': 0, 'reads': 0.0, 'ncalls': collections.Counter(),
                                              'streak': collections.Counter(), 'fill': collections.Counter(),
                                              'long': collections.Counter(), 'shell': collections.Counter()})
    paths = [(p, 'main chat') for p in glob.glob(os.path.join(a.root, '*', '*.jsonl'))]
    for p in glob.glob(os.path.join(a.root, '*', '*', 'subagents', '*.jsonl')):
        try:
            with open(p[:-len('.jsonl')] + '.meta.json', encoding='utf-8') as f:
                paths.append((p, json.load(f).get('agentType') or 'agent'))
        except (OSError, ValueError):
            paths.append((p, 'agent'))
    for path, who in paths:
        if os.path.getmtime(path) < since:
            continue
        priced, fill, long_reads, shell = scan(path, since)
        g = groups[who]
        for key, part in (('fill', fill), ('long', long_reads), ('shell', shell)):
            g[key].update(part)
        run = spent = 0
        for cost, calls in priced + [(0.0, None)]:  # the sentinel closes the last streak
            if calls is not None:
                g['cost'] += cost
                g['turns'] += 1
                g['ncalls'][min(len(calls), 3)] += 1
                g['reads'] += cost if calls and all(map(read_only, calls)) else 0
            if calls is not None and len(calls) == 1 and read_only(calls[0]):
                run, spent = run + 1, spent + cost
            elif run:
                g['streak'][next(label for limit, label in STREAKS if run <= limit)] += spent
                run = spent = 0
    total = sum(g['cost'] for g in groups.values())
    if not total:
        print(f'No priced usage in the last {a.days:g} days under {a.root}')
        return
    print(f'Estimated spend, last {a.days:g} days: ${total:,.2f}')
    for who, g in sorted(groups.items(), key=lambda x: -x[1]['cost']):
        if g['cost'] < total / 100 or not g['turns']:
            continue
        filled = sum(g['fill'].values()) or 1
        print(f'\n{who}: ${g["cost"]:,.0f}, {g["turns"]} turns, ${g["cost"] / g["turns"]:.3f}/turn')
        print(f'  entered the context: {filled / 4e6:.1f}M tokens - '
              + ', '.join(f'{k} {100 * v / filled:.0f}%' for k, v in g['fill'].most_common(6)))
        print('  whole-file reads of ' + ', '.join(f'over {n} lines {100 * g["long"][n] / filled:.1f}%' for n in LONG) + ' of that')
        shell = sum(g['shell'].values()) or 1
        print('  shell output: ' + ', '.join(f'{k} {100 * v / shell:.0f}%' for k, v in g['shell'].most_common(5)))
        print('  tool calls per turn: ' + ', '.join(f'{n}{"+" if n == 3 else ""}: {100 * g["ncalls"][n] / g["turns"]:.0f}%' for n in range(4))
              + f'; turns that only read: {100 * g["reads"] / g["cost"]:.0f}% of spend')
        print('  spend in streaks of single-read turns: '
              + ', '.join(f'{label} turns ${g["streak"][label]:,.0f} ({100 * g["streak"][label] / g["cost"]:.0f}%)' for _, label in STREAKS))


if __name__ == '__main__':
    main()
