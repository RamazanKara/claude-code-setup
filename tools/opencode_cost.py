#!/usr/bin/env python3
"""Where OpenCode's Anthropic spend goes, from its local database (read-only).

  python tools/opencode_cost.py                  # last 7 days
  python tools/opencode_cost.py --since 2026-11-01

Prints the cost split (reasoning, output, cache reads and writes), cost per
step by effort variant, cache-read spend by context size, what fills the
context (tool outputs, tool inputs, own text), and whether a 1-hour prompt
cache would have been cheaper than the default 5-minute one.
"""
import argparse
import datetime
import json
import os
import sqlite3
from collections import defaultdict
from pathlib import Path

# Opus 5.5, USD per million tokens.
P_IN, P_OUT, P_READ, P_W5M, P_W1H = 4, 20, 0.20, 5, 8
DB = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share') / 'opencode' / 'opencode.db'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', help='start date, YYYY-MM-DD (default: 7 days ago)')
    ap.add_argument('--db', default=str(DB))
    a = ap.parse_args()
    since = datetime.datetime.fromisoformat(a.since) if a.since else datetime.datetime.now() - datetime.timedelta(days=7)
    c = sqlite3.connect(f'file:{a.db}?mode=ro', uri=True)
    rows = c.execute(
        "select session_id, data from session_message where type='assistant' and time_created>? order by session_id, time_created",
        (int(since.timestamp() * 1000),),
    ).fetchall()

    cost = defaultdict(float)
    variants = defaultdict(lambda: [0, 0, 0.0])
    contexts = defaultdict(lambda: [0, 0.0])
    gaps = defaultdict(lambda: [0, 0])
    fill = defaultdict(lambda: [0, 0])
    big = defaultdict(lambda: [0, 0, 0])
    prev, writes, steps = {}, 0, 0
    for sid, d in rows:
        j = json.loads(d)
        m = j.get('model') or {}
        if m.get('providerID') != 'anthropic':
            continue
        for p in j.get('content') or []:
            if p.get('type') == 'tool':
                st = p.get('state') or {}
                out = st.get('content') or ''
                n = len(out if isinstance(out, str) else json.dumps(out)) // 4
                f = fill[p.get('name') or '?']
                f[0] += 1; f[1] += n
                fill['(tool inputs)'][1] += len(json.dumps(st.get('input') or {})) // 4
                b = big[p.get('name') or '?']
                b[0] += n; b[1] += n if n > 4000 else 0; b[2] += n > 4000
            elif p.get('type') in ('text', 'reasoning'):
                f = fill[f"(own {p['type']})"]
                f[0] += 1; f[1] += len(p.get('text') or '') // 4
        t, tm = j.get('tokens') or {}, j.get('time') or {}
        if 'opus' not in (m.get('id') or '') or not t or 'completed' not in tm:
            continue
        ca = t.get('cache') or {}
        r, w = ca.get('read', 0), ca.get('write', 0)
        steps += 1
        writes += w
        cost['reasoning'] += t.get('reasoning', 0) * P_OUT / 1e6
        cost['output'] += t.get('output', 0) * P_OUT / 1e6
        cost['cache read'] += r * P_READ / 1e6
        cost['cache write'] += w * P_W5M / 1e6
        cost['input'] += t.get('input', 0) * P_IN / 1e6
        v = variants[m.get('variant') or 'default']
        v[0] += 1; v[1] += t.get('reasoning', 0); v[2] += j.get('cost') or 0
        k = '<100k' if r < 1e5 else '100-200k' if r < 2e5 else '200-400k' if r < 4e5 else '>400k'
        contexts[k][0] += 1; contexts[k][1] += r * P_READ / 1e6
        if sid in prev:
            g = (tm['created'] - prev[sid]) / 60000
            k = '<5m' if g < 5 else '5-60m' if g < 60 else '>60m'
            gaps[k][0] += 1; gaps[k][1] += w
        prev[sid] = tm['completed']

    total = sum(cost.values()) or 1
    print(f'Opus steps since {since:%Y-%m-%d}: {steps}, ${total:.2f}')
    for k, v in sorted(cost.items(), key=lambda x: -x[1]):
        print(f'  {k:12} ${v:7.2f}  {100 * v / total:4.1f}%')
    print('Effort variant: steps, avg reasoning tok, avg $/step')
    for k, (n, r, s) in sorted(variants.items(), key=lambda x: -x[1][0]):
        print(f'  {k:8} {n:5}  {r / n:6.0f}  ${s / n:.3f}')
    rd = sum(v[1] for v in contexts.values()) or 1
    print('Context at step: steps, cache-read $, share')
    for k in ('<100k', '100-200k', '200-400k', '>400k'):
        n, s = contexts[k]
        print(f'  {k:9} {n:5}  ${s:6.2f}  {100 * s / rd:4.1f}%')
    ft = sum(v[1] for v in fill.values()) or 1
    print('What fills the context (all Anthropic steps): calls, ~tokens, share, share from outputs >4k tok')
    for k, (n, s) in sorted(fill.items(), key=lambda x: -x[1][1])[:10]:
        b = big.get(k)
        extra = f'  {100 * b[1] / b[0]:3.0f}% ({b[2]})' if b and b[0] else ''
        print(f'  {k:16} {n:5}  {s:8}  {100 * s / ft:4.1f}%{extra}')
    n_short, w_short = gaps['<5m']
    n_mid, w_mid = gaps['5-60m']
    saved = max(0, w_mid - n_mid * w_short / max(n_short, 1))
    w1h = (writes - saved) * P_W1H / 1e6 + saved * P_READ / 1e6
    print(f'Pauses of 5-60 min: {n_mid} of {steps} steps. Cache writes: ${writes * P_W5M / 1e6:.2f} on the 5-minute cache, '
          f'${w1h:.2f} if it had been 1 hour.')


if __name__ == '__main__':
    main()
