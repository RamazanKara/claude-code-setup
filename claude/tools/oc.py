#!/usr/bin/env python3
"""Hand a task to a flat-rate OpenCode model, wait, and print only its report.

The files the worker reads and the code it writes never enter the caller's context; the report does.

  python oc.py read   "<brief>"   # DeepSeek V4.1 Flash: a search or survey, reported with path:line
  python oc.py write  "<brief>"   # DeepSeek V4.1 Flash: scaffolding, translation, a fully specified edit
  python oc.py code   "<brief>"   # GLM 5.3: a fix with its regression test and the checks the brief names
  python oc.py review "<brief>"   # GLM 5.3: reviews the checkout as the reviewer agent does; a clean
                                  # finish ("Reviewed: <path>") counts for the push hook like that agent
  brief "-" reads it from stdin; options: --dir <checkout>, --timeout <seconds, default 1800>,
  --model flash|glm|glm-flash|<provider/model>

It runs with OpenCode's permission prompts answered for it: `read` and `review` are told to change
nothing, not prevented from it. Exit code 124 is the timeout; what the model had said is still printed.
"""
import argparse, json, os, re, shutil, signal, subprocess, sys

MODELS = {'flash': 'opencode-go/deepseek-v4.1-flash', 'glm': 'opencode-go/glm-5.3', 'glm-flash': 'opencode-go/glm-5.3-flash'}
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # the Claude config folder, or the repo's claude/
REPORT = ('Reply with the report alone: start each line with the exact name and its `path:line`, '
          'and say what you could not find or do.')
MODES = {
    'read': ('flash', 'This is a read-only task from another agent: edit, create and delete nothing, and run no builds '
                      'or tests. Search first and read only what the task needs. ' + REPORT),
    'write': ('flash', 'This is a scaffolding task from another agent: write the files the task names and change nothing '
                       'else; run only the checks it names. Reply with the paths you wrote, one line each on what the file '
                       'holds, and what you could not do. Do not print the code.'),
    'code': ('glm', 'This is a coding task from another agent: make the change it describes with a regression test, change '
                    'nothing else, and run only the narrowest check that proves it plus the formatter, never a full gate. '
                    'Commit only if told to. Reply with the files you changed, one line each on what changed, the checks '
                    'you ran with their results, and what stays unverified. Do not print the code.'),
    'review': ('glm', None),  # the reviewer agent's own instructions
}


def instructions(mode):
    if MODES[mode][1]:
        return MODES[mode][1]
    text = open(os.path.join(HERE, 'agents', 'reviewer.md'), encoding='utf-8').read()
    return 'This is a review task from another agent: change nothing. ' + text.split('\n---\n', 1)[-1].strip()


def count_review(text, folder):
    """Let a finished review unlock the push hook, as the reviewer agent's SubagentStop hook does."""
    sys.path.insert(0, os.path.join(HERE, 'hooks'))
    import review_gate
    review_gate.reviewed({'agent_type': 'reviewer', 'last_assistant_message': text, 'cwd': folder})


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
    ap.add_argument('--timeout', type=float, default=1800)
    ap.add_argument('--model', default=None, help='flash, glm, glm-flash or a provider/model id')
    a = ap.parse_args()
    brief = sys.stdin.read() if a.brief == '-' else a.brief
    if not brief.strip():
        sys.exit('oc.py: empty brief')
    opencode = shutil.which('opencode')
    if not opencode:
        sys.exit('oc.py: opencode is not on PATH')
    model = a.model or MODES[a.mode][0]
    title = f'{a.mode}: ' + ''.join(c if c.isalnum() or c in ' -_' else ' ' for c in brief[:48]).strip()
    command = [opencode, 'run', '-m', MODELS.get(model, model), '--format', 'json', '--archived', '--auto', '--title', title]
    # The brief goes in on stdin: on Windows opencode is a .cmd shim, which mangles quotes in arguments.
    run = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                           encoding='utf-8', errors='replace', cwd=a.dir, start_new_session=os.name != 'nt')
    try:
        out, err = run.communicate(instructions(a.mode) + '\n\n' + brief, timeout=a.timeout)
        code = run.returncode
    except subprocess.TimeoutExpired:
        stop(run)
        out = run.communicate()[0]
        err, code = f'timed out after {a.timeout:g} s', 124
    text, failed = report(out.splitlines())
    if text:
        print(text)
    if code or failed or not text:
        print('oc.py: ' + '; '.join(failed or [err.strip()[-500:] or 'no report came back']), file=sys.stderr)
        sys.exit(code or 1)
    if a.mode == 'review':
        count_review(text, os.path.abspath(a.dir or os.getcwd()))


if __name__ == '__main__':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass
    main()
