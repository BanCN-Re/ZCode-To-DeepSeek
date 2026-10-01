"""The event envelope DeepSeek Harness writes, and the reader it has to satisfy.

Every event is `{type, seq, time, data}` plus, for message-producing types, a
`surfaceOp`. `seq` must run contiguously from 0, so any importer has to emit
events in one uninterrupted run — which also means a session written from
scratch here replaces the log rather than appending to it.

The surface is the harness's model-visible view: an event either appended to the
tail (`surfaceOp: "append"`) or shadowed an existing range (`surfaceOp:
{op: "replace", startSeq, endSeq}`). Both forms are required exactly where the
harness itself uses them, so reconstructed logs fold identically.
"""
from __future__ import annotations

import uuid

import dsh_format as fmt

# message-part vocabulary in ZCode -> nothing; message content differs entirely,
# so each side keeps its own shape and only the conversation is carried across.


def _uid():
    return str(uuid.uuid4())


def header(session_id, cwd, *, created_at=None, is_seeded=False, delegation_depth=0,
           agent_preset='standard', origin=None, parent_session=None):
    """Build a v4 header. Only the six required keys plus what the caller sets."""
    h = {
        'type': 'session',
        'version': fmt.FORMAT_VERSION,
        'id': session_id,
        'createdAt': created_at if created_at is not None else fmt.now_ms(),
        'cwd': cwd,
        'isSeeded': is_seeded,
        'delegationDepth': delegation_depth,
    }
    if agent_preset:
        h['agentPreset'] = agent_preset
    if origin:
        h['origin'] = origin
    if parent_session:
        h['parentSession'] = parent_session
    return h


class Log:
    """Accumulates events with automatic contiguous sequencing."""

    def __init__(self, start_time=None):
        self.events = []
        self._t = start_time if start_time is not None else fmt.now_ms()

    def _stamp(self, t=None):
        t = t if t is not None else self._t
        t = max(t, self._t)
        self._t = t
        return t

    def add(self, type, data, *, time=None, surface_op=None, source_event_seqs=None,
            ignorable=None):
        e = {
            'type': type,
            'seq': len(self.events),
            'time': self._stamp(time),
            'data': data,
        }
        if surface_op is not None:
            e['surfaceOp'] = surface_op
        if source_event_seqs is not None:
            e['sourceEventSeqs'] = source_event_seqs
        if ignorable is not None:
            e['ignorable'] = ignorable
        self.events.append(e)
        return e['seq']

    # -- surface messages ---------------------------------------------------
    def user_message(self, text, *, time=None, source=None, msg_id=None, replace=None):
        """A user turn. `replace` is `(startSeq, endSeq)` when it supersedes a range."""
        data = {
            'content': [{'type': 'text', 'text': text}],
            'source': source or {'kind': 'user', 'rpcId': _uid()},
            'role': 'user',
            'id': msg_id or _uid(),
        }
        op = 'append' if replace is None else {'op': 'replace', 'startSeq': replace[0],
                                              'endSeq': replace[1]}
        return self.add('user/message', data, time=time, surface_op=op)

    def system_message(self, text, *, time=None, msg_id=None, replace=None):
        data = {
            'content': [{'type': 'text', 'text': text}],
            'source': {'kind': 'system-prompt'},
            'role': 'system',
            'id': msg_id or _uid(),
        }
        op = 'append' if replace is None else {'op': 'replace', 'startSeq': replace[0],
                                              'endSeq': replace[1]}
        return self.add('system/message', data, time=time, surface_op=op)

    def assistant_message(self, content, *, turn, step, provider, model, usage=None,
                          time=None, msg_id=None, stream=None, attempt=1):
        """One assistant step.

        `content` is the harness's own block list: `text`, `reasoning`, and
        `tool-call` entries. `stream` is the recorded chunk log; an empty one is
        accepted by the reader and keeps the row honest about its provenance.
        """
        data = {
            'turn': turn,
            'step': step,
            'message': {
                'role': 'assistant',
                'content': content,
                'source': {'kind': 'model', 'provider': provider, 'model': model},
                'id': msg_id or _uid(),
            },
            'usage': usage or {},
            'stream': stream or [],
        }
        return self.add('assistant/message', data, time=time, surface_op='append')

    def tool_result(self, text, *, call_id, is_error=False, time=None, msg_id=None,
                    source_event_seqs=None):
        data = {
            'turn': None,
            'step': None,
            'message': {
                'role': 'tool',
                'source': {'kind': 'tool', 'callId': call_id},
                'toolCallId': call_id,
                'content': [{'type': 'text', 'text': text}],
                'isError': bool(is_error),
                'id': msg_id or _uid(),
            },
        }
        return self.add('tool/result', data, time=time, surface_op='append',
                        source_event_seqs=source_event_seqs)

    # -- log-only events ----------------------------------------------------
    def step_start(self, turn, step, time=None):
        return self.add('step/start', {'turn': turn, 'step': step}, time=time)

    def step_end(self, turn, step, time=None):
        return self.add('step/end', {'turn': turn, 'step': step}, time=time)

    def turn_start(self, turn, time=None):
        return self.add('turn/start', {'turn': turn}, time=time)

    def turn_end(self, turn, reason='completed', time=None):
        return self.add('turn/end', {'turn': turn, 'reason': {'kind': reason}}, time=time)

    def tool_call(self, call_id, name, arguments, *, turn=None, step=None, time=None):
        """The UI-only mirror of a tool call; the authoritative copy is in the
        assistant message's `tool-call` block."""
        return self.add('tool/call', {
            'turn': turn, 'step': step, 'callId': call_id, 'name': name,
            'arguments': arguments,
        }, time=time)

    def model_selection(self, provider, model, effort=None, time=None):
        data = {'provider': provider, 'model': model}
        if effort:
            data['reasoningEffort'] = effort
        return self.add('model/selection', data, time=time)

    def session_title(self, title, *, source='fallback', message_seqs=None, time=None):
        return self.add('session/title', {
            'title': title,
            'messageSeqs': message_seqs or [],
            'source': {'kind': source},
        }, time=time)

    def metadata(self, **pairs):
        """Convenience for the small bookkeeping events."""
        for t, d in pairs.items():
            self.add(t, d)
