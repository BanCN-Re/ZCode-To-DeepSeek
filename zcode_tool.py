#!/usr/bin/env python3
"""Cross-platform entry point for both directions.

    python zcode_tool.py dsh2zcode ...     # DeepSeek Harness  -> ZCode
    python zcode_tool.py zcode2dsh ...     # ZCode -> DeepSeek Harness
    python zcode_tool.py doctor            # check this machine's setup
    python zcode_tool.py selftest          # round-trip a synthetic session

Everything else is forwarded to the direction that owns it, so the two tools can
also be called directly (`python zcode_migrate.py ...`, `python dsh_import.py ...`).
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DIRECTIONS = {'dsh2zcode': 'zcode_migrate.py', 'zcode2dsh': 'dsh_import.py'}


def run(script, argv):
    return subprocess.call([sys.executable, os.path.join(HERE, script)] + list(argv))


def cmd_doctor(_argv):
    import shutil

    ok = True
    print('python          ', sys.version.split()[0], '(%s)' % sys.executable)
    print('platform        ', sys.platform)
    for name in ('node', 'git'):
        p = shutil.which(name)
        print('%-16s%s' % (name, p or 'NOT FOUND'))
    try:
        import zstandard
        print('zstandard       ', zstandard.__version__)
    except ImportError:
        print('zstandard        NOT FOUND (pip install zstandard)')
        ok = False

    try:
        import zcode_read
        import zcode_common
        db = zcode_common.DEFAULT_DB
        print('zcode db        ', db, 'exists' if os.path.exists(db) else 'NOT FOUND')
        if os.path.exists(db):
            try:
                n = len(zcode_read.list_sessions(db))
                print('zcode sessions  ', n)
            except Exception as e:
                print('zcode sessions   unreadable:', e)
                ok = False
    except Exception as e:
        print('zcode modules    error:', e)
        ok = False

    try:
        import dsh_format as fmt
        root = fmt.SESSION_ROOT_DEFAULT
        print('dsh sessions    ', root, 'exists' if os.path.isdir(root) else 'NOT FOUND')
        if os.path.isdir(root):
            n = sum(1 for _ in fmt.iter_sessions(root))
            print('dsh logs        ', n)
    except Exception as e:
        print('dsh modules      error:', e)
        ok = False

    found = None
    for cand in ['LOCALAPPDATA', 'PROGRAMFILES']:
        base = os.environ.get(cand)
        if not base:
            continue
        p = os.path.join(base, 'Programs', 'DeepSeek Harness', 'resources', 'app.asar')
        if os.path.exists(p):
            found = p
            break
    print('harness app.asar', found or 'NOT FOUND (only needed for --harness-check)')
    print()
    print('ready' if ok else 'some prerequisites are missing')
    return 0 if ok else 1


def cmd_selftest(argv):
    """Round-trip a synthetic session through both converters."""
    import json
    import sqlite3
    import tempfile
    import time
    import uuid

    import dsh_format as fmt
    import dsh_import
    import dsh_events as ev
    import zcode_read
    import zcode_common

    tmp = tempfile.mkdtemp(prefix='zcode-tool-selftest-')
    failures = []

    def check(label, cond, detail=''):
        print('%-42s %s%s' % (label, 'ok' if cond else 'FAIL',
                              '' if cond else '  ' + str(detail)))
        if not cond:
            failures.append(label)

    # a session designed to exercise every shape the converters care about
    session_id = 'session-selftest-%s' % uuid.uuid4()
    cwd = os.path.abspath(os.path.join(tmp, 'workspace'))
    root = os.path.join(tmp, 'dsh-sessions')
    os.makedirs(cwd, exist_ok=True)

    log = ev.Log()
    header = ev.header(session_id, cwd, agent_preset='standard')
    log.model_selection('workbuddy', 'deepseek-v4.1-flash', 'high')
    log.turn_start(1)
    log.user_message('hello from the selftest')
    log.step_start(1, 1)
    content = [
        {'type': 'reasoning', 'text': 'thinking about it'},
        {'type': 'text', 'text': 'running a command'},
        {'type': 'tool-call', 'id': 'call_selftest_1', 'name': 'pwsh',
         'arguments': json.dumps({'command': 'echo hi', 'description': 'say hi'})},
    ]
    log.assistant_message(content, turn=1, step=1, provider='workbuddy',
                          model='deepseek-v4.1-flash',
                          usage={'inputTokens': 10, 'outputTokens': 5})
    log.tool_call('call_selftest_1', 'pwsh',
                  json.dumps({'command': 'echo hi', 'description': 'say hi'}),
                  turn=1, step=1)
    log.tool_result('hi\n', call_id='call_selftest_1')
    log.step_end(1, 1)
    log.step_start(1, 2)
    log.assistant_message([{'type': 'text', 'text': 'done'}], turn=1, step=2,
                          provider='workbuddy', model='deepseek-v4.1-flash')
    log.step_end(1, 2)
    log.turn_end(1)

    path = fmt.session_path(session_id, cwd, root, 'zstd')
    fmt.write_jsonl(path, header, log.events, 'zstd')
    check('write a v4 log', os.path.exists(path))

    problems = fmt.validate_header(header) + fmt.validate_events(log.events)
    check('self-validate the log', not problems, problems[:2])

    back_header, back_events = fmt.read_jsonl(path)
    check('read it back', len(back_events) == len(log.events),
          '%d vs %d' % (len(back_events), len(log.events)))

    # ---- the harness's own reader, when it can be assembled ----
    runtime = os.path.join(HERE, '_dshrt')
    runner = os.path.join(HERE, 'dsh_check.mjs')
    if not os.path.isdir(runtime) and os.path.exists(runner):
        print('%-42s preparing ...' % 'deepseek harness reader')
        try:
            subprocess.call([sys.executable,
                             os.path.join(HERE, 'dsh_extract_reader.py')],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    if os.path.isdir(runtime) and os.path.exists(runner):
        try:
            p = subprocess.run(['node', runner, path, '--full'], capture_output=True,
                               text=True, encoding='utf-8', errors='replace', timeout=180)
            out = (p.stdout or '') + (p.stderr or '')
            check('deepseek harness reads it', 'session : OK' in out,
                  out.strip().splitlines()[-1] if out.strip() else '')
            check('harness rebuilds the transcript', 'model-visible messages' in out)
        except FileNotFoundError:
            print('%-42s skipped (node not on PATH)' % 'deepseek harness reads it')
        except Exception as e:
            check('deepseek harness reads it', False, e)
    else:
        print('%-42s skipped (harness app.asar not found)'
              % 'deepseek harness reads it')

    # ---- build a ZCode side and convert back ----
    db = os.path.join(tmp, 'zcode.db')
    con = sqlite3.connect(db)
    try:
        con.executescript('''
          create table session (id text primary key, project_id text not null,
            workspace_id text, parent_id text, slug text not null, directory text not null,
            path text, title text not null, version text not null, share_url text,
            summary_additions integer, summary_deletions integer, summary_files integer,
            summary_diffs text, revert text, permission text, time_created integer not null,
            time_updated integer not null, time_compacting integer, time_archived integer,
            task_type text not null default 'interactive',
            title_source text not null default 'first_input',
            title_message_id text, time_title_updated integer, trace_id text);
          create table message (id text primary key, session_id text not null,
            time_created integer not null, time_updated integer not null, data text not null,
            sequence integer);
          create table part (id text primary key, message_id text not null,
            session_id text not null, time_created integer not null,
            time_updated integer not null, data text not null, sequence integer);
          create table todo (session_id text not null, content text not null,
            status text not null, priority text not null, position integer not null,
            time_created integer not null, time_updated integer not null,
            primary key (session_id, position));
          create table input_history (id text primary key, project_id text not null,
            session_id text, text text not null, kind text not null,
            time_created integer not null, attachments text);
        ''')
        zid = 'sess_selftest'
        now = int(time.time() * 1000)
        con.execute('insert into session (id, project_id, slug, directory, title, version, '
                    'time_created, time_updated) values (?,?,?,?,?,?,?,?)',
                    (zid, 'proj_selftest', zid, cwd, 'selftest', '0.16.9', now, now))
        mid = 'msg_selftest_1'
        con.execute('insert into message (id, session_id, time_created, time_updated, data, '
                    'sequence) values (?,?,?,?,?,?)',
                    (mid, zid, now, now, json.dumps({
                        'role': 'user', 'time': {'created': now},
                        'semantics': {'origin': 'real_user', 'kind': 'user_prompt',
                                      'uiVisibility': 'visible',
                                      'providerVisibility': 'visible',
                                      'transcriptVisibility': 'visible'},
                        'anchor': {'turnId': 'turn_selftest'}}, ensure_ascii=False), 0))
        con.execute('insert into part (id, message_id, session_id, time_created, '
                    'time_updated, data, sequence) values (?,?,?,?,?,?,?)',
                    ('part_selftest_1', mid, zid, now, now,
                     json.dumps({'type': 'text', 'text': 'hello',
                                 'time': {'start': now, 'end': now}}), 0))
        amid = 'msg_selftest_2'
        con.execute('insert into message (id, session_id, time_created, time_updated, data, '
                    'sequence) values (?,?,?,?,?,?)',
                    (amid, zid, now, now, json.dumps({
                        'role': 'assistant', 'time': {'created': now, 'completed': now},
                        'parentID': mid, 'finish': 'tool-calls',
                        'semantics': {'origin': 'agent_runtime',
                                      'kind': 'assistant_response',
                                      'uiVisibility': 'visible',
                                      'providerVisibility': 'visible',
                                      'transcriptVisibility': 'visible'},
                        'anchor': {'turnId': 'turn_selftest'}}, ensure_ascii=False), 1))
        for i, p in enumerate([
            {'type': 'step-start'},
            {'type': 'text', 'text': 'let me check',
             'time': {'start': now, 'end': now}},
            {'type': 'tool', 'callID': 'call_z_1', 'tool': 'Bash',
             'state': {'status': 'completed', 'title': 'Bash',
                       'input': {'command': 'echo hi', 'description': 'say hi'},
                       'output': 'hi\n',
                       'metadata': {'schemaVersion': 1},
                       'time': {'start': now, 'end': now}}},
            {'type': 'step-finish', 'reason': 'tool-calls', 'cost': 0,
             'tokens': {'total': 0, 'input': 0, 'output': 0, 'reasoning': 0,
                        'cache': {'read': 0, 'write': 0}}},
        ]):
            con.execute('insert into part (id, message_id, session_id, time_created, '
                        'time_updated, data, sequence) values (?,?,?,?,?,?,?)',
                        ('part_selftest_%d' % (i + 2), amid, zid, now, now,
                         json.dumps(p, ensure_ascii=False), i))
        con.commit()
    finally:
        con.close()

    data = zcode_read.read_session(db, zid)
    check('read the synthetic zcode session', len(data['messages']) == 2)

    ex = dsh_import.Exporter(data, provider='workbuddy', model='deepseek-v4.1-flash',
                             cwd=cwd)
    out_log = ex.build()
    calls = sum(1 for e in out_log.events if e['type'] == 'assistant/message'
                for b in ((e['data'].get('message') or {}).get('content') or [])
                if b.get('type') == 'tool-call')
    results = sum(1 for e in out_log.events if e['type'] == 'tool/result')
    check('tool calls and results pair up', calls == results and calls == 1,
          '%d vs %d' % (calls, results))

    out_path = fmt.session_path('session-selftest-out', cwd, root, 'zstd')
    fmt.write_jsonl(out_path, ev.header('session-selftest-out', cwd), out_log.events)
    ph = fmt.validate_header(ev.header('session-selftest-out', cwd))
    pe = fmt.validate_events(out_log.events)
    check('zcode2dsh output validates', not ph and not pe, (ph + pe)[:2])

    print()
    print('workdir:', tmp)
    if failures:
        print('%d check(s) failed: %s' % (len(failures), ', '.join(failures)))
        return 1
    print('all checks passed')
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__)
        print('directions:')
        for k, v in DIRECTIONS.items():
            print('  %-10s -> %s' % (k, v))
        print('  %-10s    environment check' % 'doctor')
        print('  %-10s    round-trip self test' % 'selftest')
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == 'doctor':
        return cmd_doctor(rest)
    if cmd == 'selftest':
        return cmd_selftest(rest)
    if cmd in DIRECTIONS:
        return run(DIRECTIONS[cmd], rest)
    print('unknown command: %s' % cmd, file=sys.stderr)
    print('expected one of: %s' % ', '.join(list(DIRECTIONS) + ['doctor', 'selftest']),
          file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
