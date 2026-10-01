"""Work out which events a DSH export still considers live.

A DSH transcript is an append-only event log plus a set of supersede/compaction
records. Two things remove events from the live view:

  * `compaction/summary` carries the seqs it condensed into prose.
  * `compaction/prune` carries individual seqs dropped as redundant.

An event is live when no such record covers it. Sequence order alone is not a
boundary: the compaction that produced the checkpoint shadowed seqs up to 3399,
yet live events continue from 2864 and the checkpoint itself sits at 4551.
"""
from __future__ import annotations

import collections

# Event types that carry conversation the provider sees.
MESSAGE_TYPES = ('user/message', 'assistant/message', 'tool/result')
# `tool/call` is a UI record; the authoritative call lives in the assistant
# message's tool-call block, and the two are not always in step.
UI_ONLY_TYPES = ('tool/call', 'step/start', 'step/end')


def surface_op(event):
    """`append`, `replace`, or None for events outside the surface log."""
    op = event.get('surfaceOp')
    if op is None:
        return None
    if isinstance(op, dict):
        return op.get('op')
    return op


def shadowed_seqs(events):
    """Every seq any compaction record has taken out of the live view."""
    out = set()
    for e in events:
        if e['type'] in ('compaction/summary', 'compaction/prune'):
            out.update((e.get('data') or {}).get('shadowedSeqs') or [])
    return out


def compaction_records(events):
    """Successful summaries, newest last."""
    ended = {}
    for e in events:
        if e['type'] == 'compaction/end':
            d = e.get('data') or {}
            ended[d.get('compactionId')] = d.get('error')
    good = []
    for e in events:
        if e['type'] != 'compaction/summary':
            continue
        cid = (e.get('data') or {}).get('compactionId')
        if ended.get(cid) is None:
            good.append(e)
    return good


def live_message_events(events):
    """The conversation the provider would be sent, in order.

    Only surface-log events count: an event with no `surfaceOp` was never part
    of the transcript the model saw.
    """
    shadow = shadowed_seqs(events)
    out = []
    for e in events:
        if e['type'] not in MESSAGE_TYPES:
            continue
        if surface_op(e) not in ('append', 'replace'):
            continue
        if e.get('seq') in shadow:
            continue
        out.append(e)
    return out


def tool_calls_of(assistant_event):
    """Tool-call blocks carried inside an assistant message."""
    out = []
    body = (assistant_event.get('data') or {}).get('message') or {}
    for b in body.get('content') or []:
        if isinstance(b, dict) and b.get('type') == 'tool-call':
            out.append({'id': b.get('id'), 'name': b.get('name'),
                        'arguments': b.get('arguments')})
    return out


def tool_results_by_call(live_events):
    out = {}
    for e in live_events:
        if e['type'] != 'tool/result':
            continue
        d = e.get('data') or {}
        cid = (d.get('message') or {}).get('toolCallId')
        if cid:
            out[cid] = e
    return out


def audit(events):
    """Checks that the derived live set is internally consistent.

    A transcript whose assistant tool-call blocks and tool results do not pair
    up one-to-one would be rejected by the provider, so this is the signal that
    the boundary logic is right.
    """
    live = live_message_events(events)
    results = tool_results_by_call(live)

    calls = {}
    for e in live:
        if e['type'] != 'assistant/message':
            continue
        for c in tool_calls_of(e):
            if c['id']:
                calls[c['id']] = c

    human = [e for e in live if e['type'] == 'user/message'
             and ((e['data'].get('source') or {}).get('kind')) == 'user']

    return {
        'live_events': len(live),
        'by_type': dict(collections.Counter(e['type'] for e in live)),
        'calls': len(calls),
        'results': len(results),
        'calls_without_result': sorted(set(calls) - set(results)),
        'results_without_call': sorted(set(results) - set(calls)),
        'human_turns': len(human),
        'seq_range': (min(e['seq'] for e in live), max(e['seq'] for e in live)),
        'live_below_last_summary': sum(
            1 for e in live
            if e['seq'] < (compaction_records(events)[-1]['seq'] if compaction_records(events) else 0)),
        'live': live,
        'results_by_call': results,
        'calls_by_id': calls,
    }
