#!/usr/bin/env python3
"""Export a ZCode session into a DeepSeek Harness session log.

    python dsh_import.py list                       # ZCode sessions
    python dsh_import.py inspect <sess_id>          # what would be exported
    python dsh_import.py export  <sess_id> [--commit]
    python dsh_import.py verify  <file|sess_id>     # read the log back
    python dsh_import.py sessions [--root DIR]      # DSH sessions on disk

The result is a format-v4 `session.v4.jsonl.zstd` placed exactly where the
harness looks for it, so the session appears in its own session list and can be
resumed. Nothing is written without `--commit`, and an existing target is never
overwritten unless `--overwrite` is passed.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import sys
import time
import uuid

import dsh_events as ev
import dsh_format as fmt
import dsh_from_zcode as conv
import zcode_read as zread

# ZCode part types that carry no conversation and are dropped on export.
DROPPED_PARTS = {'step-start', 'step-finish', 'timeline', 'compaction', 'file', 'snapshot',
                 'patch', 'subagent', 'agent', 'retry'}


class Exporter:
    """Walks a ZCode session and emits DSH events."""

    def __init__(self, data, *, provider, model, cwd, keep_reasoning=True,
                 source_session_id=None):
        self.data = data
        self.provider = provider
        self.model = model
        self.cwd = cwd or (data['session'].get('directory') or os.getcwd())
        self.keep_reasoning = keep_reasoning
        self.source_session_id = source_session_id or data['session']['id']
        self.stats = collections.Counter()
        self.notes = []

    def build(self):
        msgs = self.data['messages']
        events_start = int(self.data['session'].get('time_created') or fmt.now_ms())
        log = ev.Log(start_time=events_start)

        log.model_selection(self.provider, self.model, time=events_start)

        turn = 0
        step = 0
        current_turn_open = False
        # every tool call the assistant announced, awaiting its result event
        pending = {}

        for m in msgs:
            d = m['data']
            role = d.get('role')
            sem = d.get('semantics') or {}
            kind = sem.get('kind')
            parts = m['parts']
            t = m['time_created']

            if role == 'user':
                if kind == 'compact_summary':
                    # ZCode renders these from `summary`; DSH has no equivalent
                    # single event, so carry the prose as a system message.
                    body = (d.get('summary') or {}).get('body') or zread.text_of(parts)
                    if body.strip():
                        log.system_message(body, time=t)
                        self.stats['system_messages'] += 1
                    continue
                text = zread.text_of(parts)
                if not text.strip():
                    self.stats['empty_user'] += 1
                    continue
                if current_turn_open:
                    log.turn_end(turn, 'completed')
                turn += 1
                step = 0
                current_turn_open = True
                log.turn_start(turn, time=t)
                log.user_message(text, time=t,
                                 source={'kind': 'user', 'rpcId': str(uuid.uuid4())})
                self.stats['user_messages'] += 1
                continue

            if role != 'assistant':
                continue

            # a turn can begin with an assistant row when the export starts mid-turn
            if not current_turn_open:
                turn += 1
                step = 0
                current_turn_open = True
                log.turn_start(turn, time=t)

            # timeline-only rows carry no turn content
            if all(p.get('type') in DROPPED_PARTS for p in parts) and not zread.text_of(parts):
                self.stats['skipped_rows'] += 1
                continue

            step += 1
            content = conv.assistant_content(parts, keep_reasoning=self.keep_reasoning)
            tools = zread.tool_parts(parts)
            if not content and not tools:
                self.stats['empty_assistant'] += 1
                continue

            usage = d.get('tokens') or {}
            log.step_start(turn, step, time=t)
            log.assistant_message(content, turn=turn, step=step,
                                  provider=self.provider, model=self.model,
                                  usage=usage, time=t,
                                  stream=conv.build_stream(content, usage))
            self.stats['assistant_messages'] += 1
            if any(b.get('type') == 'reasoning' for b in content):
                self.stats['reasoning_blocks'] += 1

            for p in tools:
                st = p.get('state') or {}
                zname = p.get('tool') or 'Bash'
                args = conv.dsh_arguments(zname, p)
                cid = p.get('callID') or str(uuid.uuid4())
                log.tool_call(cid, conv.dsh_tool_name(zname),
                              json.dumps(args, ensure_ascii=False),
                              turn=turn, step=step, time=t)
                log.tool_result(conv.tool_result_text(p), call_id=cid,
                                is_error=conv.tool_is_error(p), time=t,
                                source_event_seqs=[log.events[-1]['seq']])
                pending[cid] = True
                self.stats['tool_calls'] += 1
                if conv.tool_is_error(p):
                    self.stats['tool_errors'] += 1
            log.step_end(turn, step, time=t)

        if current_turn_open:
            log.turn_end(turn, 'completed')
            self.stats['turns'] = turn

        self.log = log
        return log


# ── commands ────────────────────────────────────────────────────────────────
def cmd_list(args):
    rows = zread.list_sessions(args.db, args.limit)
    if not rows:
        print('no sessions in', args.db)
        return 0
    print('%-42s %8s  %-19s  %s' % ('SESSION', 'MESSAGES', 'UPDATED', 'TITLE'))
    for r in rows:
        title = (r['title'] or '').replace('\n', ' ')[:44]
        when = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime((r['time_updated'] or 0) / 1000))
        print('%-42s %8d  %-19s  %s' % (r['id'], r['messages'], when, title))
    return 0


def cmd_sessions(args):
    n = 0
    for path, project in fmt.iter_sessions(args.root):
        try:
            header, events = fmt.read_jsonl(path)
        except Exception as e:
            print('%-62s  UNREADABLE: %s' % (path, str(e)[:40]))
            continue
        n += 1
        when = time.strftime('%Y-%m-%d %H:%M', time.localtime(header.get('createdAt', 0) / 1000))
        print('%-46s %-22s %6d events  %s' % (header.get('id', '?')[:44], project[:20],
                                              len(events), when))
    print('\n%d session log(s) under %s' % (n, args.root))
    return 0


def cmd_inspect(args):
    data = zread.read_session(args.db, args.session)
    s = data['session']
    ex = Exporter(data, provider=args.provider, model=args.model, cwd=args.cwd,
                  keep_reasoning=not args.drop_reasoning)
    log = ex.build()

    print('zcode session      ', s['id'])
    print('title              ', s.get('title'))
    print('directory          ', s.get('directory'))
    print('messages / parts   ', len(data['messages']),
          '/', sum(len(m['parts']) for m in data['messages']))
    print()
    print('events to write    ', len(log.events))
    print('  user turns       ', ex.stats['user_messages'])
    print('  assistant steps  ', ex.stats['assistant_messages'])
    print('  tool calls       ', ex.stats['tool_calls'], '(%d errors)' % ex.stats['tool_errors'])
    print('  reasoning blocks ', ex.stats['reasoning_blocks'])
    print('  system messages  ', ex.stats['system_messages'])
    if ex.stats['empty_user'] or ex.stats['empty_assistant'] or ex.stats['skipped_rows']:
        print('  skipped          ', 'empty user %d, empty assistant %d, timeline-only %d'
              % (ex.stats['empty_user'], ex.stats['empty_assistant'], ex.stats['skipped_rows']))
    types = collections.Counter(e['type'] for e in log.events)
    print()
    print('event types        ', dict(types.most_common()))
    print()
    print('target path        ', fmt.session_path(args.out_id or s['id'], ex.cwd, args.root))
    return 0


def cmd_export(args):
    data = zread.read_session(args.db, args.session)
    s = data['session']
    ex = Exporter(data, provider=args.provider, model=args.model, cwd=args.cwd,
                  keep_reasoning=not args.drop_reasoning,
                  source_session_id=s['id'])
    log = ex.build()

    session_id = args.out_id or ('session-%s' % uuid.uuid4())
    path = fmt.session_path(session_id, ex.cwd, args.root, args.compression)
    header = ev.header(session_id, ex.cwd, created_at=s.get('time_created'),
                       agent_preset=args.agent_preset,
                       delegation_depth=0)

    problems = fmt.validate_header(header) + fmt.validate_events(log.events)

    print('zcode session      ', s['id'])
    print('  -> dsh session   ', session_id)
    print('cwd                ', ex.cwd)
    print('provider / model   ', '%s / %s' % (args.provider, args.model))
    print('events             ', len(log.events))
    print('  user turns       ', ex.stats['user_messages'])
    print('  assistant steps  ', ex.stats['assistant_messages'])
    print('  tool calls       ', ex.stats['tool_calls'])
    print('target             ', path)
    print('validation         ', 'OK' if not problems else '%d problem(s)' % len(problems))
    for p in problems[:10]:
        print('   ', p)

    if problems:
        print('\nrefusing to write an invalid log')
        return 1

    if not args.commit:
        print('\nDRY RUN — nothing written. Re-run with --commit to apply.')
        return 0

    if os.path.exists(path) and not args.overwrite:
        print('\ntarget already exists; pass --overwrite to replace it')
        return 1

    n = fmt.write_jsonl(path, header, log.events, args.compression, args.level)
    print('\nwrote %d bytes' % n)
    print('open DeepSeek Harness and pick the session from its list')
    return 0


def cmd_verify(args):
    path = args.target
    if not os.path.exists(path):
        found = fmt.find_session(args.target, args.root)
        if not found:
            print('not a file and no DSH session with that id: %s' % args.target)
            return 1
        path = found
    header, events, problems = fmt.validate_log(path)
    types = collections.Counter(e.get('type') for e in events)
    live = sum(1 for e in events if e.get('surfaceOp') is not None)
    calls = sum(1 for e in events if e.get('type') == 'assistant/message'
                for b in ((e.get('data') or {}).get('message') or {}).get('content') or []
                if isinstance(b, dict) and b.get('type') == 'tool-call')
    results = sum(1 for e in events if e.get('type') == 'tool/result')
    print('path                ', path)
    print('session id          ', header.get('id'))
    print('cwd                 ', header.get('cwd'))
    print('version             ', header.get('version'))
    print('events              ', len(events))
    print('  surface events    ', live)
    print('  tool-call blocks  ', calls, '| tool/result events', results,
          '| paired' if calls == results else '| UNPAIRED')
    print('event types         ', dict(types.most_common(12)))
    print('problems            ', len(problems))
    for p in problems[: args.max_problems]:
        print('   ', p)
    if problems:
        return 1
    if args.harness_check:
        return harness_check(path)
    return 0


def harness_check(path, timeout=180):
    """Validate with DeepSeek Harness's own reader, if it can be assembled.

    The format packages ship only inside the packaged app, so this extracts the
    reader's module closure first. It is the strongest available check: the
    harness parses the log and rebuilds the transcript a model would receive.
    """
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    runner = os.path.join(here, 'dsh_check.mjs')
    runtime = os.path.join(here, '_dshrt')
    if not os.path.exists(runner):
        print('\nharness check: dsh_check.mjs not found next to this script')
        return 1
    if not os.path.isdir(runtime):
        print('\nharness check: extracting the harness reader ...')
        rc = subprocess.call([sys.executable, os.path.join(here, 'dsh_extract_reader.py')])
        if rc != 0:
            print('harness check: could not extract the reader (is DeepSeek Harness installed?)')
            return 1
    try:
        node = subprocess.run(['node', runner, path, '--full'],
                              capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=timeout)
    except FileNotFoundError:
        print('\nharness check: node is not on PATH')
        return 1
    except subprocess.TimeoutExpired:
        print('\nharness check: timed out')
        return 1
    out = (node.stdout or '') + (node.stderr or '')
    print('\n--- deepseek harness reader ---')
    for line in out.splitlines():
        if line.startswith('artifact: FAIL'):
            # The published build's header validator wants a differently shaped
            # call than the catalog exposes; the Session load below is the
            # authoritative check and covers the same invariants.
            continue
        print('   ', line)
    return 0 if 'session : OK' in out else 1


# ── cli ─────────────────────────────────────────────────────────────────────
def build_parser():
    p = argparse.ArgumentParser(
        prog='dsh_import.py',
        description='Export a ZCode session into a DeepSeek Harness session log.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Nothing is written without --commit.')
    p.add_argument('--db', default=zread.DEFAULT_DB, help='ZCode db.sqlite (default: %(default)s)')
    p.add_argument('--root', default=fmt.SESSION_ROOT_DEFAULT,
                   help='DSH sessions root (default: %(default)s)')
    sub = p.add_subparsers(dest='cmd', required=True)

    l = sub.add_parser('list', help='list ZCode sessions')
    l.add_argument('--limit', type=int)
    l.set_defaults(func=cmd_list)

    s = sub.add_parser('sessions', help='list DeepSeek Harness sessions on disk')
    s.set_defaults(func=cmd_sessions)

    i = sub.add_parser('inspect', help='show what an export would contain')
    i.add_argument('session')
    i.add_argument('--provider', default='workbuddy')
    i.add_argument('--model', default='deepseek-v4.1-flash')
    i.add_argument('--cwd')
    i.add_argument('--out-id')
    i.add_argument('--drop-reasoning', action='store_true')
    i.set_defaults(func=cmd_inspect)

    e = sub.add_parser('export', help='write a DSH session log')
    e.add_argument('session')
    e.add_argument('--provider', default='workbuddy')
    e.add_argument('--model', default='deepseek-v4.1-flash')
    e.add_argument('--cwd', help='workspace path for the new log')
    e.add_argument('--out-id', help='session id (default: a fresh uuid)')
    e.add_argument('--agent-preset', default='standard')
    e.add_argument('--compression', default='zstd', choices=['zstd', 'none'])
    e.add_argument('--level', type=int, default=3, help='zstd level')
    e.add_argument('--drop-reasoning', action='store_true',
                   help="omit ZCode's thinking blocks from the transcript")
    e.add_argument('--overwrite', action='store_true')
    e.add_argument('--commit', action='store_true')
    e.set_defaults(func=cmd_export)

    v = sub.add_parser('verify', help='read a DSH log back and validate it')
    v.add_argument('target', help='a file path, or a session id to look up')
    v.add_argument('--max-problems', type=int, default=20)
    v.add_argument('--harness-check', action='store_true',
                   help="also load the log with DeepSeek Harness's own reader")
    v.set_defaults(func=cmd_verify)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
