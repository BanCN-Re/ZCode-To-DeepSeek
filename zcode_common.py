"""Shared helpers for moving a DeepSeek Harness export into a ZCode profile.

Everything that touches the ZCode database lives here: locating it, snapshotting
it before a write, learning the row shapes native rows actually use, and making
ids and project keys in the formats the app itself produces.
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import sqlite3
import string
import time
import uuid
import zipfile

ALPH = string.ascii_lowercase + string.digits

# ZCode stores everything in one SQLite file per profile.
DEFAULT_DB = os.path.join(os.path.expanduser('~'), '.zcode', 'cli', 'db', 'db.sqlite')
DEFAULT_ROLLOUT_DIR = os.path.join(os.path.expanduser('~'), '.zcode', 'cli', 'rollout')

EXPORT_MAIN = 'session.v4.jsonl'
EXPORT_SUBAGENTS = 'subagents'


# ── ids ─────────────────────────────────────────────────────────────────────
def _tok(n=8):
    return ''.join(random.choice(ALPH) for _ in range(n))


def new_message_id():
    return 'msg_mig%s_%s' % (_tok(), uuid.uuid4())


def new_part_id():
    return 'part_mig%s_%s' % (_tok(), uuid.uuid4())


def new_turn_id():
    return 'turn_mig_%s' % _tok(10)


def new_session_id():
    return 'sess_%s' % uuid.uuid4()


# ── database ────────────────────────────────────────────────────────────────
def connect(db, readonly=False):
    uri = 'file:%s?mode=ro' % db.replace('\\', '/') if readonly else db
    con = sqlite3.connect(uri, uri=readonly, timeout=60)
    con.row_factory = sqlite3.Row
    if not readonly:
        con.execute('pragma busy_timeout=60000')
    return con


def project_id_for(directory):
    """Derive the project key ZCode builds from a workspace path."""
    return 'proj_' + re.sub(r'[:\\/]+', '-', directory).strip('-').lower()


def session_row(db, sid):
    con = connect(db, readonly=True)
    try:
        return con.execute('select * from session where id=?', (sid,)).fetchone()
    finally:
        con.close()


def find_session_by_title(db, fragment):
    con = connect(db, readonly=True)
    try:
        return con.execute('select id, title, directory from session where title like ? '
                           'order by time_updated desc', ('%' + fragment + '%',)).fetchall()
    finally:
        con.close()


def backup_db(db, tag='migration'):
    """Copy the database (plus WAL) next to itself before any write."""
    stamp = time.strftime('%Y%m%d-%H%M%S')
    dest = os.path.join(os.path.dirname(db), '_%s-backup-%s' % (tag, stamp))
    os.makedirs(dest, exist_ok=True)
    src = connect(db, readonly=True)
    dst = sqlite3.connect(os.path.join(dest, 'db.sqlite'))
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    for suffix in ('-wal', '-shm'):
        p = db + suffix
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(dest, os.path.basename(p)))
    return dest


def native_shapes(db):
    """Learn the key sets present in rows this tool did not write.

    Migrated rows are validated against these rather than against a hand-copied
    schema, so the check follows whatever the installed ZCode build produces.
    """
    part_keys, state_keys, msg_keys = {}, {}, set()
    con = connect(db, readonly=True)
    try:
        for (d,) in con.execute("select data from part where message_id not like 'msg_mig%'"):
            o = json.loads(d)
            t = o.get('type')
            part_keys.setdefault(t, set()).update(o.keys())
            if t == 'tool':
                st = o.get('state') or {}
                state_keys.setdefault(st.get('status'), set()).update(st.keys())
        for (d,) in con.execute("select data from message where id not like 'msg_mig%'"):
            msg_keys.update(json.loads(d).keys())
    finally:
        con.close()
    return part_keys, state_keys, msg_keys


# ── export loading ──────────────────────────────────────────────────────────
def load_export(path, extract_to=None):
    """Read a DSH export from a .zip or a directory holding session.v4.jsonl.

    Returns (events, source_dir). A zip is extracted next to itself (or into
    extract_to) so subagent logs and media land on disk next to the transcript.
    """
    if os.path.isdir(path):
        src = path
    elif zipfile.is_zipfile(path):
        src = extract_to or os.path.splitext(path)[0]
        os.makedirs(src, exist_ok=True)
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if EXPORT_MAIN not in names:
                raise SystemExit('not a DSH session export (no %s): %s' % (EXPORT_MAIN, path))
            z.extractall(src)
    else:
        raise SystemExit('expected a .zip or a directory: %s' % path)

    main = os.path.join(src, EXPORT_MAIN)
    if not os.path.exists(main):
        raise SystemExit('missing %s under %s' % (EXPORT_MAIN, src))

    events = []
    with open(main, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events, src


def export_meta(events):
    """Header record + the last successful compaction, if any."""
    header = next((e for e in events if e.get('type') == 'session'), {})
    compactions = [e for e in events if e.get('type') == 'compaction/summary']
    ended = [e for e in events if e.get('type') == 'compaction/end']
    ok_ends = {e['data'].get('compactionId') for e in ended if not e['data'].get('error')}
    good = [e for e in compactions if e['data'].get('compactionId') in ok_ends]
    return {
        'session_id': header.get('id'),
        'cwd': header.get('cwd'),
        'created_at': header.get('createdAt'),
        'agent_preset': header.get('agentPreset'),
        'compaction': good[-1] if good else None,
        'compaction_count': len(compactions),
    }


def list_user_prompts(events):
    """Every genuine human turn, in order."""
    out = []
    for e in events:
        if e.get('type') != 'user/message':
            continue
        d = e.get('data') or {}
        if ((d.get('source') or {}).get('kind')) != 'user':
            continue
        txt = '\n'.join(b.get('text') or '' for b in d.get('content') or []
                        if isinstance(b, dict) and b.get('type') == 'text').strip()
        if txt:
            out.append({'seq': e.get('seq'), 'time': e.get('time'), 'text': txt})
    return out


def summarize_export(events):
    """Counts used by `--inspect`."""
    import collections
    c = collections.Counter()
    tools = collections.Counter()
    for e in events:
        c[e.get('type')] += 1
        if e.get('type') == 'tool/call':
            tools[(e.get('data') or {}).get('name')] += 1
    return c, tools
