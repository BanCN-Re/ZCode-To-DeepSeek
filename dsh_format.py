"""Read and write DeepSeek Harness session logs (format v4).

A DSH session is one JSONL file whose first line is a header and whose remaining
lines are events. Each file lives at

    <sessions-root>/<projectKey(cwd)>/<encodeSegment(sessionId)>/session.v4.jsonl[.zstd]

`projectKey` folds filesystem separators and the drive colon to `-` and strips
leading dashes, then wraps the result in `--…--`; `encodeSegment` escapes every
character outside `[A-Za-z0-9._-]` (plus `~`) as `~XXXX` with an uppercase hex
code unit. Both rules are reproduced here exactly as the harness applies them,
so a log written by this tool lands where the harness will look for it.

The format is validated strictly on read: events must start at seq 0 and be
contiguous, the envelope may carry only known keys, and each message event must
carry an identified message whose role and source match its type.
"""
from __future__ import annotations

import io
import json
import os
import re
import time

FORMAT_VERSION = 4
SESSION_ROOT_DEFAULT = os.path.join(os.path.expanduser('~'), '.dsh', 'sessions')

# envelope keys the reader accepts; anything else is rejected outright
ENVELOPE_KEYS = {'type', 'seq', 'time', 'data', 'surfaceOp', 'sourceEventSeqs', 'ignorable'}

# header keys: these six are required, the rest are optional
HEADER_REQUIRED = ('type', 'version', 'id', 'createdAt', 'isSeeded', 'delegationDepth')
HEADER_OPTIONAL = ('cwd', 'parentSession', 'origin', 'agentPreset')

# event types that carry a message onto the model-visible surface
SURFACE_EVENT_TYPES = ('system/message', 'developer/message', 'user/message',
                       'assistant/message', 'tool/result')
ROLE_BY_TYPE = {
    'system/message': 'system',
    'developer/message': 'developer',
    'user/message': 'user',
    'assistant/message': 'assistant',
    'tool/result': 'tool',
}

_SAFE = re.compile(r'^[A-Za-z0-9._-]$')


# ── path rules (mirror the harness) ─────────────────────────────────────────
def project_key(cwd):
    """`--D-Work-Fut--` for `D:\\Work\\Fut`."""
    if not cwd:
        raise ValueError('cannot encode an empty project path')
    readable = ''
    separator_run = False
    for ch in cwd:
        if ch in '/\\:':
            if not separator_run:
                readable += '-'
            separator_run = True
        elif ch != '~' and _SAFE.match(ch):
            readable += ch
            separator_run = False
        else:
            readable += '~%04X' % ord(ch)
            separator_run = False
    return '--%s--' % (re.sub(r'^-+', '', readable) or 'root')[:251]


def encode_segment(raw):
    """Path-encode one id so it survives as a single directory name."""
    if not raw:
        raise ValueError('cannot encode an empty path segment')
    if raw == '.':
        return '~002E'
    if raw == '..':
        return '~002E~002E'
    out = ''
    for ch in raw:
        if ch != '~' and _SAFE.match(ch):
            out += ch
        else:
            out += '~%04X' % ord(ch)
    return out


def session_dir(session_id, cwd, root=SESSION_ROOT_DEFAULT):
    return os.path.join(root, project_key(cwd), encode_segment(session_id))


def session_path(session_id, cwd, root=SESSION_ROOT_DEFAULT, compression='zstd'):
    suffix = '.zstd' if compression == 'zstd' else ''
    return os.path.join(session_dir(session_id, cwd, root), 'session.v4.jsonl' + suffix)


# ── compression ─────────────────────────────────────────────────────────────
def _dctx():
    import zstandard
    return zstandard.ZstdDecompressor()


def _cctx(level=3):
    import zstandard
    return zstandard.ZstdCompressor(level=level)


def read_jsonl(path):
    """Return (header, events) from a `.jsonl` or `.jsonl.zstd` file."""
    raw = open(path, 'rb').read()
    if path.endswith('.zstd'):
        data = _dctx().stream_reader(io.BytesIO(raw)).read()
    else:
        data = raw
    lines = [l for l in data.decode('utf-8').split('\n') if l.strip()]
    if not lines:
        raise ValueError('empty session log: %s' % path)
    header = json.loads(lines[0])
    events = [json.loads(l) for l in lines[1:]]
    return header, events


def write_jsonl(path, header, events, compression='zstd', level=3):
    """Write a session log, creating parent directories as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = json.dumps(header, ensure_ascii=False) + '\n'
    text += '\n'.join(json.dumps(e, ensure_ascii=False) for e in events)
    text += '\n'
    blob = text.encode('utf-8')
    if compression == 'zstd':
        blob = _cctx(level).compress(blob)
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(blob)
    os.replace(tmp, path)
    return len(blob)


# ── validation (mirrors the harness read path) ──────────────────────────────
def validate_header(header):
    problems = []
    for k in HEADER_REQUIRED:
        if k not in header:
            problems.append('header missing required key %r' % k)
    for k in header:
        if k not in HEADER_REQUIRED and k not in HEADER_OPTIONAL:
            problems.append('header has unknown key %r' % k)
    if header.get('type') != 'session':
        problems.append('header type must be "session"')
    if header.get('version') != FORMAT_VERSION:
        problems.append('header version must be %d, got %r' % (FORMAT_VERSION, header.get('version')))
    if not isinstance(header.get('id'), str) or not header.get('id'):
        problems.append('header id must be a non-empty string')
    ts = header.get('createdAt')
    if not isinstance(ts, int) or ts < 0:
        problems.append('header createdAt must be a non-negative integer')
    if not isinstance(header.get('isSeeded'), bool):
        problems.append('header isSeeded must be a boolean')
    dd = header.get('delegationDepth')
    if not isinstance(dd, int) or dd < 0:
        problems.append('header delegationDepth must be a non-negative integer')
    cwd = header.get('cwd')
    if cwd is not None and not (isinstance(cwd, str) and os.path.isabs(cwd)):
        problems.append('header cwd must be an absolute path')
    if header.get('parentSession') is not None and not isinstance(header['parentSession'], str):
        problems.append('header parentSession must be a string')
    if header.get('origin') is not None and header['origin'] != 'subagent':
        problems.append('header origin must be "subagent"')
    if header.get('agentPreset') is not None and not isinstance(header['agentPreset'], str):
        problems.append('header agentPreset must be a string')
    return problems


def validate_events(events):
    problems = []
    for i, e in enumerate(events):
        if not isinstance(e, dict):
            problems.append('event %d is not an object' % i)
            continue
        extra = set(e) - ENVELOPE_KEYS
        if extra:
            problems.append('event %d has envelope keys outside the schema: %s' % (i, sorted(extra)))
        if e.get('type') not in (None,):
            pass
        seq = e.get('seq')
        if not isinstance(seq, int) or seq < 0:
            problems.append('event %d has a non-integer seq' % i)
        elif seq != i:
            problems.append('event %d has seq %s; events must be contiguous from 0' % (i, seq))
        if not isinstance(e.get('time'), int):
            problems.append('event %d has a non-integer time' % i)
        if 'data' not in e:
            problems.append('event %d has no data' % i)
        t = e.get('type')
        if t in SURFACE_EVENT_TYPES:
            if 'surfaceOp' not in e:
                problems.append('event %d (%s) is a message event without surfaceOp' % (i, t))
            else:
                op = e['surfaceOp']
                if not (op == 'append' or isinstance(op, dict)):
                    problems.append('event %d has an invalid surfaceOp %r' % (i, op))
        problems.extend(_validate_message(e, i))
    return problems


def _validate_message(e, i):
    t = e.get('type')
    if t not in ROLE_BY_TYPE:
        return []
    out = []
    data = e.get('data')
    if not isinstance(data, dict):
        return ['event %d (%s) data must be an object' % (i, t)]
    msg = data if t == 'user/message' else data.get('message')
    if not isinstance(msg, dict):
        return ['event %d (%s) lacks a message object' % (i, t)]
    if not isinstance(msg.get('id'), str) or not msg['id']:
        out.append('event %d (%s) message has no id' % (i, t))
    want = ROLE_BY_TYPE[t]
    if msg.get('role') != want:
        out.append('event %d (%s) message role must be %r, got %r' % (i, t, want, msg.get('role')))
    src = msg.get('source')
    if not isinstance(src, dict) or not isinstance(src.get('kind'), str) or not src.get('kind'):
        out.append('event %d (%s) message has an invalid source' % (i, t))
        return out
    if not isinstance(msg.get('content'), list):
        out.append('event %d (%s) message content must be a list' % (i, t))
    if t == 'assistant/message':
        if src.get('kind') != 'model' or not src.get('provider') or not src.get('model'):
            out.append('event %d assistant message source must be model with provider and model' % i)
        for k in ('turn', 'step'):
            v = data.get(k)
            if not isinstance(v, int) or v < 0:
                out.append('event %d assistant message has an invalid %s' % (i, k))
        if not isinstance(data.get('stream'), list):
            out.append('event %d assistant message needs a stream list' % i)
    if t == 'tool/result':
        if src.get('kind') != 'tool' or not src.get('callId'):
            out.append('event %d tool result source must be tool with a callId' % i)
        elif msg.get('toolCallId') != src.get('callId'):
            out.append('event %d tool result has mismatched call ids' % i)
    return out


def validate_log(path):
    """Full read-path check of one session file."""
    header, events = read_jsonl(path)
    problems = validate_header(header)
    problems.extend(validate_events(events))
    return header, events, problems


# ── discovery ───────────────────────────────────────────────────────────────
def iter_sessions(root=SESSION_ROOT_DEFAULT):
    """Yield `(path, project_dir)` for every session log under the root."""
    if not os.path.isdir(root):
        return
    for project in sorted(os.listdir(root)):
        pdir = os.path.join(root, project)
        if not os.path.isdir(pdir):
            continue
        for sess in sorted(os.listdir(pdir)):
            sdir = os.path.join(pdir, sess)
            if not os.path.isdir(sdir):
                continue
            for name in sorted(os.listdir(sdir)):
                if name.startswith('session.v4.jsonl'):
                    yield os.path.join(sdir, name), project


def find_session(session_id, root=SESSION_ROOT_DEFAULT):
    """Locate a session log by id, wherever its project directory is."""
    wanted = encode_segment(session_id)
    for path, project in iter_sessions(root):
        if os.path.basename(os.path.dirname(path)) == wanted:
            return path
    return None


def summarize(events):
    """Counts and the derived live view, for reporting."""
    import collections
    types = collections.Counter(e.get('type') for e in events)
    return types


def now_ms():
    return int(time.time() * 1000)
