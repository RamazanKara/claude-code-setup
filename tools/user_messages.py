#!/usr/bin/env python3
"""The messages you typed to Claude Code, from your local transcripts, one per line.

For reading where the agents' way of working cost you corrections: "did you also ...", "again ...",
"why ...". Tool results, system reminders, slash commands, task notifications and compaction summaries
are left out; a text repeated (the same brief sent twice) is printed once. Briefs one agent sent another
through Orca still appear, since they arrive as typed text.

  python tools/user_messages.py                 # last 5 days
  python tools/user_messages.py --days 2 --grep "did you|again|why"
  python tools/user_messages.py > msgs.txt      # then hand the file to a cheap reader to classify

Each line: `<time> <project folder> | <text, cut at --width characters>`.
"""
import argparse, glob, json, os, re, sys, time

SKIP_START = ('<command-name>', '<local-command', '<task-notification>', 'Caveat:')


def text_of(content):
    if isinstance(content, list):
        content = ' '.join(x.get('text', '') for x in content if isinstance(x, dict) and x.get('type') == 'text')
    return content if isinstance(content, str) else ''


def messages(root, since):
    for path in glob.glob(os.path.join(root, '*', '*.jsonl')):
        if os.path.getmtime(path) < since:
            continue
        project = os.path.basename(os.path.dirname(path))
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get('type') != 'user' or entry.get('isSidechain') or entry.get('isMeta') or entry.get('isCompactSummary'):
                    continue
                text = re.sub(r'<system-reminder>.*?</system-reminder>', '', text_of((entry.get('message') or {}).get('content')),
                              flags=re.S).strip()
                if not text or text.startswith(SKIP_START) or 'This session is being continued' in text[:200]:
                    continue
                yield entry.get('timestamp', ''), project, ' '.join(text.split())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--days', type=float, default=5)
    ap.add_argument('--root', default=os.path.join(os.environ.get('CLAUDE_CONFIG_DIR') or os.path.expanduser('~/.claude'), 'projects'))
    ap.add_argument('--grep', help='only messages matching this regular expression (case-insensitive)')
    ap.add_argument('--width', type=int, default=400)
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    since = time.time() - args.days * 86400
    pattern = re.compile(args.grep, re.I) if args.grep else None
    seen = set()
    for stamp, project, text in sorted(messages(args.root, since)):
        if text in seen or pattern and not pattern.search(text):
            continue
        seen.add(text)
        print(stamp[:16], project, '|', text[:args.width])


if __name__ == '__main__':
    main()
