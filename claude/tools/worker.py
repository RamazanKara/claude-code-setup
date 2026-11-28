#!/usr/bin/env python3
"""Hand a task to a Claude Code or Antigravity worker, wait, and print only its report.

The files the worker reads and the code it writes never enter the caller's context; the report does.
By default this stays on the active harness (never switches harnesses silently):
  - Inside Claude Code (or default): runs `claude -p` (Haiku low for read, Sonnet medium for write,
    Sonnet high for code, Opus high for review; `--disallowedTools` enforces read-only on read/review).
  - Inside Antigravity (`ANTIGRAVITY_AGENT=1`): runs `agy --print` (Gemini 4 Hydrogen low in plan mode
    for read, Neon high for write, Neon xhigh for code, Neon high in plan mode for review).

Opt-in cross-harness delegation / second-opinion review (ONLY when explicitly requested):
  python worker.py review --harness agy    --dir <repo> "<brief>"   # from Claude Code -> Antigravity Neon
  python worker.py review --harness claude --effort xhigh --dir <repo> "<brief>"  # from Antigravity -> Opus 5.5

A finished `review` with no High findings ending with `Reviewed: <repo>` unlocks the push gate
(`review_gate.py`) for either harness. Options: `--dir <checkout>`, `--harness auto|claude|agy`,
`--model <id>`, `--effort low|medium|high|xhigh|max`, `--timeout <seconds, default none>`.
Exit code 124 is a `--timeout` that ran out; whatever the worker had printed is preserved.
"""
import argparse, os, shutil, signal, subprocess, sys, tempfile

CLAUDE_MODELS = {'read': 'haiku', 'write': 'sonnet', 'code': 'sonnet', 'review': 'opus'}
CLAUDE_EFFORT = {'read': 'low', 'write': 'medium', 'code': 'high', 'review': 'high'}
AGY_BASES = {
    'read': ('gemini-4-hydrogen', ('low', 'medium', 'high', 'xhigh')),
    'write': ('gemini-4-neon', ('low', 'medium', 'high', 'xhigh')),
    'code': ('gemini-4-neon', ('low', 'medium', 'high', 'xhigh')),
    'review': ('gemini-4-neon', ('low', 'medium', 'high', 'xhigh')),
}
AGY_EFFORT = {'read': 'low', 'write': 'high', 'code': 'xhigh', 'review': 'high'}
READONLY_DISALLOWED = ['Write', 'Edit', 'NotebookEdit']
ORCA_PARENT_ENV = (
    'ORCA_AGENT_SESSION_ID', 'ORCA_AGENT_SESSION_SPAWN_TOKEN', 'ORCA_PANE_KEY',
    'ORCA_TAB_ID', 'ORCA_AGENT_LAUNCH_TOKEN', 'ORCA_WORKTREE_ID',
)
AGY_INLINE_LIMIT = 20_000  # bytes; larger prompts go through a temp file cleaned in finally

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT = ('Reply with the report alone: start each line with the exact name and its `path:line`, '
          'and say what you could not find or do.')
MODES = {
    'read': ('This is a read-only task from another agent: edit, create and delete nothing, and run no builds '
             'or tests. Search first and read only what the task needs. ' + REPORT),
    'write': ('This is a scaffolding task from another agent: write the files the task names and change nothing '
              'else; run only the checks it names. Reply with the paths you wrote, one line each on what the file '
              'holds, and what you could not do. Do not print the code.'),
    'code': ('This is a coding task from another agent: make the change it describes with a regression test, change '
             'nothing else, and run only the narrowest check that proves it plus the formatter, never a full gate. '
             'Commit only if told to. Reply with the files you changed, one line each on what changed, the checks '
             'you ran with their results, and what stays unverified. Do not print the code.'),
    'review': None,  # loaded from agents/reviewer.md
}


def default_harness():
    """Stay native to the caller's harness unless --harness explicitly opts into the other."""
    if os.environ.get('ANTIGRAVITY_AGENT') == '1' and os.environ.get('CLAUDECODE') != '1':
        return 'agy'
    return 'claude'


def instructions(mode):
    if MODES[mode]:
        return MODES[mode]
    text = open(os.path.join(HERE, 'agents', 'reviewer.md'), encoding='utf-8').read()
    return 'This is a review task from another agent: change nothing. ' + text.split('\n---\n', 1)[-1].strip()


def count_review(text, folder):
    """Let a finished review unlock the push hook, as the reviewer agent's SubagentStop hook does."""
    try:
        sys.path.insert(0, os.path.join(HERE, 'hooks'))
        import review_gate
        review_gate.reviewed({'agent_type': 'reviewer', 'last_assistant_message': text, 'cwd': folder})
    except Exception:
        pass


def stop(run):
    """End the run with the processes it started; a wrapper's child would otherwise keep pipes open."""
    if os.name == 'nt':
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(run.pid)], capture_output=True)
    else:
        os.killpg(run.pid, signal.SIGKILL)


def resolve_agy_model_effort(mode, model_override, effort_override):
    """Pick matching Antigravity model ID and effort tier (clamping max -> xhigh and unknown tiers)."""
    effort = effort_override or AGY_EFFORT[mode]
    if model_override:
        return model_override, effort
    base, tiers = AGY_BASES[mode]
    tier = 'xhigh' if effort == 'max' else effort
    if tier not in tiers:
        tier = tiers[-1]
    return f'{base}-{tier}', tier


def build_command(harness, args, prompt, prompt_file=None):
    """Return (argv, stdin_text_or_None, exe_name) for the chosen harness."""
    if harness == 'claude':
        exe = shutil.which('claude')
        if not exe:
            return None, None, 'claude'
        model = args.model or CLAUDE_MODELS[args.mode]
        effort = args.effort or CLAUDE_EFFORT[args.mode]
        cmd = [exe, '-p', '--model', model, '--effort', effort, '--output-format', 'text',
               '--dangerously-skip-permissions', '--no-session-persistence']
        if args.mode in ('read', 'review'):
            cmd += ['--disallowedTools', *READONLY_DISALLOWED]
        return cmd, prompt, 'claude'

    exe = shutil.which('agy')
    if not exe:
        return None, None, 'agy'
    model, effort = resolve_agy_model_effort(args.mode, args.model, args.effort)
    # Note: do NOT pass --disable-slash-commands with --mode plan (agy warns it disables plan mode).
    mode_flag = 'plan' if args.mode in ('read', 'review') else 'auto'
    prompt_arg = (f'Read and follow every instruction in "{prompt_file}" verbatim.'
                  if prompt_file else prompt)
    cmd = [exe, '--model', model, '--effort', effort, '--mode', mode_flag,
           '--dangerously-skip-permissions', '--output-format', 'text', '--print', prompt_arg]
    return cmd, None, 'agy'


def _strip_orca_parent_env(env):
    """Strip interactive Orca pane/session hook env vars and disable startup hooks in headless workers."""
    for key in list(env):
        if key in ORCA_PARENT_ENV or key.startswith('ORCA_AGENT_HOOK'):
            env.pop(key, None)
    env['CLAUDE_FRESH_CHECKOUT'] = '0'
    env['CLAUDE_TASK_DAILY'] = '0'
    return env


def claude_env():
    """Return child env for `claude -p`, resolving Orca's active Claude profile when CLAUDE_CONFIG_DIR is unset."""
    env = _strip_orca_parent_env(dict(os.environ))
    if env.get('CLAUDE_CONFIG_DIR'):
        return env
    orca = env.get('ORCA_USER_DATA_PATH') or (os.path.join(env['APPDATA'], 'orca') if env.get('APPDATA') else '')
    if not orca:
        return env
    sel_file = os.path.join(orca, 'claude-profiles', 'selected-host')
    try:
        with open(sel_file, encoding='utf-8') as f:
            raw = f.read().strip()
    except OSError:
        return env
    if not raw:
        return env
    cfg = raw if os.path.isabs(raw) else os.path.join(orca, 'claude-profiles', raw, 'home')
    if os.path.isdir(cfg):
        env['CLAUDE_CONFIG_DIR'] = cfg
    return env


def agy_env():
    """Return child env for `agy --print` with parent Orca pane/session hook vars stripped."""
    return _strip_orca_parent_env(dict(os.environ))


def execute(harness, args, prompt):
    """Run one worker invocation and return (report_text, error_reason, exit_code)."""
    prompt_file = None
    try:
        if harness == 'agy':
            exe = shutil.which('agy')
            if not exe:
                return '', 'agy is not on PATH', 1
            if exe.lower().endswith(('.cmd', '.bat')) or len(prompt.encode('utf-8')) > AGY_INLINE_LIMIT:
                fd, prompt_file = tempfile.mkstemp(prefix='worker-brief-', suffix='.md')
                with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
                    f.write(prompt)
        cmd, stdin_text, exe_name = build_command(harness, args, prompt, prompt_file)
        if not cmd:
            return '', f'{exe_name} is not on PATH', 1
        child_env = claude_env() if harness == 'claude' else agy_env()
        run = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            cwd=args.dir,
            env=child_env,
            start_new_session=os.name != 'nt',
        )
        try:
            out, err = run.communicate(stdin_text, timeout=args.timeout)
            code = run.returncode
        except subprocess.TimeoutExpired:
            stop(run)
            out = run.communicate()[0]
            err, code = f'timed out after {args.timeout:g} s', 124
        text = (out or '').strip()
        if code or not text:
            why = (err or '').strip()[-500:] or 'no report came back'
            return text, why, code or 1
        return text, '', 0
    finally:
        if prompt_file:
            try:
                os.unlink(prompt_file)
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('mode', choices=sorted(MODES))
    ap.add_argument('brief', help='the task, or - to read it from stdin')
    ap.add_argument('--dir', default=None, help='working directory / checkout for the worker')
    ap.add_argument('--harness', choices=('auto', 'claude', 'agy'), default='auto',
                    help='auto stays on the active harness; pass claude or agy to explicitly cross harnesses')
    ap.add_argument('--model', default=None, help='override the default model for this harness and mode')
    ap.add_argument('--effort', choices=('low', 'medium', 'high', 'xhigh', 'max'),
                    help='override default reasoning effort for this mode')
    ap.add_argument('--timeout', type=float, default=None, help='seconds; default none (wait until finished)')
    a = ap.parse_args()
    brief = sys.stdin.read() if a.brief == '-' else a.brief
    if not brief.strip():
        sys.exit('worker.py: empty brief')
    harness = default_harness() if a.harness == 'auto' else a.harness
    prompt = instructions(a.mode) + '\n\n' + brief
    text, why, code = execute(harness, a, prompt)
    if code or why:
        if text:
            print(text)
        print(f'worker.py: {harness}: {why}', file=sys.stderr)
        sys.exit(code)
    print(text)
    if a.mode == 'review':
        count_review(text, os.path.abspath(a.dir or os.getcwd()))


if __name__ == '__main__':
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass
    main()
