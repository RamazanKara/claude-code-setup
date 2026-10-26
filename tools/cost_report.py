#!/usr/bin/env python3
"""Estimate Claude Code spend from your local transcripts (~/.claude/projects).

Splits cost by who spent it (main chat vs each agent type), by model, and by
token kind (cache reads, cache writes, output, uncached input), so you can see
which rule in CLAUDE.md would save the most. A handoff section shows what
delegating costs: main-chat vs agent turn price, main-chat turns spent reacting
to agent reports, and how much of each agent's spend comes before its first edit.

  python tools/cost_report.py            # last 7 days
  python tools/cost_report.py --days 30

Prices are list prices per million tokens; edit PRICES if yours differ. This is
an estimate for comparing habits, not a bill.
"""
import argparse, collections, datetime, glob, json, os

# model substring: (input, output, cache read, cache write 1h, cache write 5m)
PRICES = {
    'opus': (4, 20, .20, 8, 5),
    'sonnet': (2, 10, .20, 4, 2.5),
    'haiku': (.10, .50, .01, .20, .125),
}
KINDS = ('cache read', 'cache write', 'output', 'input')


def cost(model, u):
    p = next((v for k, v in PRICES.items() if k in model), None)
    if not p:
        return None
    i, o, r, w1, w5 = p
    cc = u.get('cache_creation') or {}
    if cc:
        w = cc.get('ephemeral_1h_input_tokens', 0) * w1 + cc.get('ephemeral_5m_input_tokens', 0) * w5
    else:
        w = u.get('cache_creation_input_tokens', 0) * w5
    return {
        'cache read': u.get('cache_read_input_tokens', 0) * r / 1e6,
        'cache write': w / 1e6,
        'output': u.get('output_tokens', 0) * o / 1e6,
        'input': u.get('input_tokens', 0) * i / 1e6,
    }


def user_text(e):
    """Text the user (or the harness) typed; None for tool results."""
    c = (e.get('message') or {}).get('content')
    if isinstance(c, str):
        return c
    if isinstance(c, list) and not any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in c):
        return ' '.join(b.get('text', '') for b in c if isinstance(b, dict))
    return None


def usage(path, since):
    """Return (model, usage, trigger, before_first_edit) once per API message after `since`, in order.

    Transcripts repeat a message per content block. `trigger` is what started the
    turn: 'user', 'agent report' (a finished agent's notification) or 'compaction'.
    """
    seen, trigger, edited = {}, 'user', False
    with open(path, encoding='utf-8', errors='ignore') as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get('type') == 'user':
                t = user_text(e)
                if t and t.strip():
                    trigger = ('agent report' if '<task-notification' in t[:2000]
                               else 'compaction' if t.lstrip().startswith('This session is being continued') else 'user')
                continue
            m = e.get('message') if e.get('type') == 'assistant' else None
            if not (isinstance(m, dict) and m.get('id')):
                continue
            ts = e.get('timestamp')
            if ts and datetime.datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp() < since:
                continue
            if m['id'] not in seen:
                seen[m['id']] = [str(m.get('model', '')), None, trigger, not edited]
            if m.get('usage'):
                seen[m['id']][1] = m['usage']
            if any(isinstance(b, dict) and b.get('type') == 'tool_use' and b.get('name') in ('Edit', 'Write', 'NotebookEdit')
                   for b in m.get('content') or []):
                edited = True
    return [tuple(v) for v in seen.values() if v[1]]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--days', type=float, default=7)
    ap.add_argument('--root', default=os.path.join(os.environ.get('CLAUDE_CONFIG_DIR') or os.path.expanduser('~/.claude'), 'projects'))
    a = ap.parse_args()
    since = (datetime.datetime.now() - datetime.timedelta(days=a.days)).timestamp()

    by_who = collections.defaultdict(lambda: {'runs': 0, 'turns': 0, 'cost': 0.0, **{k: 0.0 for k in KINDS},
                                              'edit runs': 0, 'edit runs cost': 0.0, 'before first edit': 0.0,
                                              'agent report': 0.0})
    by_model = collections.Counter()
    unpriced = collections.Counter()

    def add(who, path):
        if os.path.getmtime(path) < since:
            return
        turns = usage(path, since)
        if not turns:
            return
        g = by_who[who]
        g['runs'] += 1
        run, pre = 0.0, 0.0
        for model, u, trigger, before_edit in turns:
            c = cost(model, u)
            if c is None:
                if not model.startswith('<'):  # '<synthetic>' marks local, unbilled messages
                    unpriced[model] += 1
                continue
            g['turns'] += 1
            for k, v in c.items():
                g[k] += v
                g['cost'] += v
                by_model[next(k for k in PRICES if k in model)] += v
            t = sum(c.values())
            run += t
            pre += t if before_edit else 0
            g['agent report'] += t if trigger == 'agent report' else 0
        if not turns[-1][3]:  # this run edited something
            g['edit runs'] += 1
            g['edit runs cost'] += run
            g['before first edit'] += pre

    for jl in glob.glob(os.path.join(a.root, '*', '*.jsonl')):
        add('main chat', jl)
    for jl in glob.glob(os.path.join(a.root, '*', '*', 'subagents', '*.jsonl')):
        try:
            with open(jl[:-len('.jsonl')] + '.meta.json', encoding='utf-8') as f:
                who = json.load(f).get('agentType') or 'agent'
        except (OSError, ValueError):
            who = 'agent'
        add(who, jl)

    total = sum(g['cost'] for g in by_who.values())
    if not total:
        print(f'No priced usage in the last {a.days:g} days under {a.root}')
        return
    print(f'Estimated spend, last {a.days:g} days: ${total:,.2f}\n')
    print(f'{"who":18}{"runs":>6}{"turns":>8}{"$":>10}{"%":>5}{"$/turn":>8}   ' + '  '.join(f'{k:>11}' for k in KINDS))
    for who, g in sorted(by_who.items(), key=lambda x: -x[1]['cost']):
        if not g['turns']:
            continue
        print(f'{who:18}{g["runs"]:6}{g["turns"]:8}{g["cost"]:10.2f}{100 * g["cost"] / total:5.0f}{g["cost"] / g["turns"]:8.3f}   '
              + '  '.join(f'{100 * g[k] / g["cost"]:10.0f}%' for k in KINDS))
    print('\nBy model: ' + ', '.join(f'{m} ${v:,.2f} ({100 * v / total:.0f}%)' for m, v in by_model.most_common()))

    # Handoffs: what delegating costs beyond the work itself (CLAUDE.md "Delegation").
    main = by_who.get('main chat')
    agents = sorted(((w, g) for w, g in by_who.items() if w != 'main chat' and g['turns']), key=lambda x: -x[1]['cost'])
    if main and main['turns'] and agents:
        top, tg = agents[0]
        an = 'an' if top[0] in 'aeiou' else 'a'
        print(f'\nHandoffs:\n  A main-chat turn costs {main["cost"] / main["turns"] / (tg["cost"] / tg["turns"]):.1f}x {an} {top} turn.')
        print(f'  Main-chat turns reacting to agent reports: {100 * main["agent report"] / main["cost"]:.0f}% of main-chat spend.')
        for who, g in agents:
            if g['edit runs cost'] >= 1:
                print(f'  {who}: {100 * g["before first edit"] / g["edit runs cost"]:.0f}% of its spend comes before its first edit.')
    if unpriced:
        print('Unpriced models (add to PRICES): ' + ', '.join(f'{m} ({n} turns)' for m, n in unpriced.most_common()))


if __name__ == '__main__':
    main()
