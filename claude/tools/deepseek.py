#!/usr/bin/env python3
"""Hand a reading or scaffolding task to DeepSeek through OpenCode and print only its report.

For an agent that needs the answer in the same turn, such as a subagent, which an Orca
worker cannot report back to. The files DeepSeek reads and the code it writes never
enter the caller's context; the report does.

  python deepseek.py read  "<brief>"     # a survey; reports with path:line
  python deepseek.py write "<brief>"     # writes the files the brief names; reports their paths
  python deepseek.py read -              # brief on stdin
  options: --dir <folder to work in>, --timeout <seconds, default 900>

It runs in the current folder unless --dir is given, and with OpenCode's permission
prompts answered for it: `read` is told to change nothing, not prevented from it. A run
takes from ten seconds to a few minutes, so call it with a long tool timeout. Exit code
124 is the timeout; what DeepSeek had said by then is still printed.
"""
import argparse, json, os, shutil, signal, subprocess, sys

MODEL = 'opencode-go/deepseek-v4.1-flash'
MODES = {
    'read': 'This is a read-only task from another agent: edit, create and delete nothing, and run no builds '
            'or tests. Search first and read only what the task needs. Reply with the report alone: start each '
            'line with the exact name and its `path:line`, and say what you could not find.',
    'write': 'This is a scaffolding task from another agent: write the files the task names and change nothing '
             'else; run only the checks it names. Reply with the paths you wrote, one line each on what the file '
             'holds, and what you could not do. Do not print the code.',
}


def report(lines):
    """The text of the last step that said something, from OpenCode's JSON event stream."""
    steps, failed = [[]], []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        part = event.get('part') or {}
        if event.get('type') == 'step_start':
            steps.append([])
        elif event.get('type') == 'text' and part.get('text'):
            steps[-1].append(part['text'])
        elif event.get('type') == 'error':
            failed.append(json.dumps(event.get('error') or event)[:500])
    said = [''.join(step).strip() for step in steps if ''.join(step).strip()]
    return (said[-1] if said else ''), failed


def stop(run):
    """End the run with the processes it started; the shim's child would otherwise keep the pipes open."""
    if os.name == 'nt':
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(run.pid)], capture_output=True)
    else:
        os.killpg(run.pid, signal.SIGKILL)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('mode', choices=sorted(MODES))
    ap.add_argument('brief', help='the task, or - to read it from stdin')
    ap.add_argument('--dir', default=None)
    ap.add_argument('--timeout', type=float, default=900)
    a = ap.parse_args()
    brief = sys.stdin.read() if a.brief == '-' else a.brief
    if not brief.strip():
        sys.exit('deepseek.py: empty brief')
    opencode = shutil.which('opencode')
    if not opencode:
        sys.exit('deepseek.py: opencode is not on PATH')
    title = 'worker: ' + ''.join(c if c.isalnum() or c in ' -_' else ' ' for c in brief[:48]).strip()
    command = [opencode, 'run', '-m', MODEL, '--format', 'json', '--archived', '--auto', '--title', title]
    # The brief goes in on stdin: on Windows opencode is a .cmd shim, which mangles quotes in arguments.
    run = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                           encoding='utf-8', errors='replace', cwd=a.dir, start_new_session=os.name != 'nt')
    try:
        out, err = run.communicate(MODES[a.mode] + '\n\n' + brief, timeout=a.timeout)
        code = run.returncode
    except subprocess.TimeoutExpired:
        stop(run)
        out = run.communicate()[0]
        err, code = f'timed out after {a.timeout:g} s', 124
    text, failed = report(out.splitlines())
    if text:
        print(text)
    if code or failed or not text:
        print('deepseek.py: ' + '; '.join(failed or [err.strip()[-500:] or 'no report came back']), file=sys.stderr)
        sys.exit(code or 1)


if __name__ == '__main__':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass
    main()
