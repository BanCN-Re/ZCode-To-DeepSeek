"""Read a ZCode session out of its SQLite profile.

ZCode keeps one row per message and one row per part. Messages carry the
conversation-level metadata (`role`, `time`, `semantics`, `parentID`); parts
carry the content (`text`, `reasoning`, `tool`, `step-start`, `step-finish`,
`compaction`, `timeline`). Reconstructing a transcript means walking messages in
`sequence` order and reading the parts of each.

This module only reads; the writer in `zcode_migrate.py` owns mutations.
"""
from __future__ import annotations

import collections
import json
import os
import sqlite3

DEFAULT_DB = os.path.join(os.path.expanduser('~'), '.zcode', 'cli', 'db', 'db.sqlite')


def connect(db=DEFAULT_DB, readonly=True):
    uri = 'file:%s?mode=ro' % db.replace('\\', '/') if readonly else db
    con = sqlite3.connect(uri, uri=readonly, timeout=60)
    con.row_factory = sqlite3.Row
    return con


def list_sessions(db=DEFAULT_DB, limit=None):
    """Every session, newest first, with its message count."""
    con = connect(db)
    try:
        sql = ('select s.id, s.title, s.directory, s.time_created, s.time_updated, '
               '(select count(*) from message m where m.session_id = s.id) as messages '
               'from session s order by s.time_updated desc')
        if limit:
            sql += ' limit %d' % int(limit)
        return [dict(r) for r in con.execute(sql)]
    finally:
        con.close()


def session_info(db, session_id):
    con = connect(db)
    try:
        row = con.execute('select * from session where id=?', (session_id,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def read_session(db, session_id):
    """Return `{'session': {...}, 'messages': [{... , 'parts': [...]}]}`."""
    con = connect(db)
    try:
        srow = con.execute('select * from session where id=?', (session_id,)).fetchone()
        if srow is None:
            raise KeyError('no such session: %s' % session_id)
        parts = collections.defaultdict(list)
        for r in con.execute('select message_id, sequence, data from part where session_id=? '
                             'order by message_id, sequence', (session_id,)):
            parts[r['message_id']].append(json.loads(r['data']))
        messages = []
        for r in con.execute('select id, sequence, time_created, data from message where session_id=? '
                             'order by sequence', (session_id,)):
            d = json.loads(r['data'])
            messages.append({
                'id': r['id'],
                'sequence': r['sequence'],
                'time_created': r['time_created'],
                'data': d,
                'parts': parts.get(r['id'], []),
            })
        return {'session': dict(srow), 'messages': messages}
    finally:
        con.close()


def text_of(parts):
    """Concatenate the visible text of a message's parts."""
    out = []
    for p in parts or []:
        if p.get('type') == 'text' and p.get('text'):
            out.append(p['text'])
    return '\n'.join(out)


def reasoning_of(parts):
    out = []
    for p in parts or []:
        if p.get('type') == 'reasoning' and p.get('text'):
            out.append(p['text'])
    return '\n'.join(out)


def tool_parts(parts):
    return [p for p in parts or [] if p.get('type') == 'tool']


def todo_rows(db, session_id):
    con = connect(db)
    try:
        return [dict(r) for r in con.execute(
            'select content, status, priority, position from todo where session_id=? '
            'order by position', (session_id,))]
    finally:
        con.close()


def input_history(db, session_id):
    con = connect(db)
    try:
        return [r[0] for r in con.execute(
            'select text from input_history where session_id=? order by time_created',
            (session_id,))]
    finally:
        con.close()
