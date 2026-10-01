"""Translate ZCode parts back into DeepSeek Harness event content.

ZCode stores a tool call and its result in one `tool` part (`state.input` +
`state.output`); DSH splits them across an `assistant/message` tool-call block
and a following `tool/result` event. The mapping therefore has to be inverted,
not copied.

Tool names are also mapped back: ZCode's catalog is what the importing harness
expects to see in history. The reverse tables below are exact inverses of the
forward ones and fall through unchanged when a name has no counterpart.
"""
from __future__ import annotations

import json

# ZCode tool name -> preferred DSH tool name
REVERSE_TOOL = {
    'Bash': 'pwsh',
    'Read': 'read',
    'Write': 'write',
    'Edit': 'edit',
    'Skill': 'skill',
    'AskUserQuestion': 'ask_user_question',
    'WebSearch': 'web_search',
    'WebFetch': 'web_fetch',
    'TodoWrite': 'todo_write',
    'TodoRead': 'get_goal',
    'TaskOutput': 'job_output',
    'TaskStop': 'job_kill',
    'Agent': 'subagent',
}

# DSH tool argument schemas, for shaping the reverse direction.
DSH_ARG_KEYS = {
    'pwsh': {'command', 'description'},
    'read': {'file_path', 'limit', 'offset'},
    'write': {'file_path', 'content'},
    'edit': {'file_path', 'old_string', 'new_string', 'replace_all'},
    'skill': {'name'},
    'ask_user_question': {'questions'},
    'web_search': {'queries'},
    'web_fetch': {'url'},
    'todo_write': {'todos'},
    'job_output': {'job_id', 'timeout_ms', 'wait'},
    'job_kill': {'job_id'},
    'subagent': {'description', 'prompt'},
}


def dsh_tool_name(zcode_name, fallback=None):
    return REVERSE_TOOL.get(zcode_name, fallback or zcode_name)


def dsh_arguments(zcode_name, tool_part):
    """Turn a ZCode tool part's `state.input` back into DSH argument JSON."""
    st = tool_part.get('state') or {}
    a = st.get('input')
    if not isinstance(a, dict):
        a = {}

    if zcode_name == 'Bash':
        out = {'command': a.get('command', ''),
               'description': a.get('description') or 'command'}
        if a.get('timeout'):
            out['timeoutMs'] = int(a['timeout']) * 1000
        return out

    if zcode_name == 'Skill':
        return {'name': a.get('skill') or a.get('name') or 'unknown',
                **({'args': a['args']} if a.get('args') else {})}

    if zcode_name == 'TaskOutput':
        return {'job_id': a.get('task_id') or a.get('job_id') or '',
                'timeout_ms': int(a.get('timeout') or 30000),
                'wait': bool(a.get('block', True))}

    if zcode_name == 'TaskStop':
        return {'job_id': a.get('task_id') or a.get('job_id') or ''}

    if zcode_name == 'WebSearch':
        return {'queries': [a.get('query')] if a.get('query') else []}

    if zcode_name == 'WebFetch':
        return {'url': a.get('url', '')}

    if zcode_name == 'Agent':
        return {'description': a.get('description') or 'subagent task',
                'prompt': a.get('prompt') or ''}

    if zcode_name == 'TodoRead':
        return {}

    return a


def tool_result_text(tool_part):
    st = tool_part.get('state') or {}
    if st.get('status') == 'error':
        return st.get('error') or ''
    return st.get('output') or ''


def tool_is_error(tool_part):
    return (tool_part.get('state') or {}).get('status') == 'error'


def assistant_content(parts, *, keep_reasoning=True):
    """Build a DSH assistant `content` block list from ZCode parts.

    ZCode interleaves `step-start` / `step-finish` markers with content; those
    have no assistant-content counterpart, so only `text`, `reasoning`, and
    `tool-call` blocks come through.
    """
    content = []
    for p in parts or []:
        t = p.get('type')
        if t == 'reasoning' and keep_reasoning and (p.get('text') or '').strip():
            content.append({'type': 'reasoning', 'text': p['text']})
        elif t == 'text' and (p.get('text') or '').strip():
            content.append({'type': 'text', 'text': p['text']})
        elif t == 'tool':
            zname = p.get('tool') or 'Bash'
            args = dsh_arguments(zname, p)
            content.append({
                'type': 'tool-call',
                'id': p.get('callID') or '',
                'name': dsh_tool_name(zname),
                'arguments': json.dumps(args, ensure_ascii=False),
            })
    return content


def build_stream(content, usage=None):
    """A minimal but well-formed chunk log for one assistant message.

    DSH records the SSE chunks it received. An imported turn has no such record,
    so the stream is synthesised from the final content: the reader validates
    only that the field is a list, and a faithful replay is impossible anyway.
    """
    stream = []
    idx = 0
    for b in content:
        if b.get('type') == 'reasoning':
            stream.append({'type': 'chunk', 'chunk': {'type': 'block-start', 'index': idx,
                                                      'blockType': 'reasoning'}})
            stream.append({'type': 'chunk', 'chunk': {'type': 'block-end', 'index': idx,
                                                      'block': {'type': 'reasoning',
                                                                'text': b['text']}}})
            idx += 1
        elif b.get('type') == 'text':
            stream.append({'type': 'chunk', 'chunk': {'type': 'block-start', 'index': idx,
                                                      'blockType': 'text'}})
            stream.append({'type': 'chunk', 'chunk': {'type': 'block-end', 'index': idx,
                                                      'block': {'type': 'text',
                                                                'text': b['text']}}})
            idx += 1
        elif b.get('type') == 'tool-call':
            stream.append({'type': 'chunk', 'chunk': {'type': 'block-start', 'index': idx,
                                                      'blockType': 'tool-call'}})
            stream.append({'type': 'chunk', 'chunk': {'type': 'block-end', 'index': idx,
                                                      'block': b}})
            idx += 1
    if usage:
        stream.append({'type': 'chunk', 'chunk': {'type': 'usage', 'usage': usage}})
    stream.append({'type': 'chunk', 'chunk': {'type': 'finish',
                                              'reason': {'kind': 'tool-calls'
                                                         if any(b.get('type') == 'tool-call'
                                                                for b in content)
                                                         else 'stop'}}})
    return stream
