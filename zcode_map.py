"""Translate DeepSeek Harness tool calls into the ZCode tool vocabulary.

A replayed transcript is sent back to the provider on the next turn, so a tool
name outside the declared catalog, or an argument object that does not match the
target tool's schema, produces an invalid request. These tables and rewriters are
what keep the imported history loadable.
"""
from __future__ import annotations

import json

# DSH tool name -> ZCode tool name
TOOL_MAP = {
    'pwsh': 'Bash', 'bash': 'Bash', 'shell': 'Bash',
    'read': 'Read', 'read_image': 'Read',
    'write': 'Write', 'edit': 'Edit',
    'skill': 'Skill',
    'ask_user_question': 'AskUserQuestion',
    'web_search': 'WebSearch', 'web_fetch': 'WebFetch',
    'todo_write': 'TodoWrite', 'get_goal': 'TodoRead',
    'create_goal': 'TodoWrite', 'update_goal': 'TodoWrite',
    'subagent': 'Agent', 'subagent_fork': 'Agent',
    'job_output': 'TaskOutput', 'job_list': 'TaskOutput', 'job_kill': 'TaskStop',
}

# The catalog ZCode advertises, recorded on each imported user message.
ZCODE_TOOLS = [
    "Agent", "AskUserQuestion", "Bash", "CronCreate", "CronDelete", "CronList", "CronUpdate",
    "Edit", "EnterPlanMode", "ExitPlanMode", "Read", "Skill", "TaskOutput", "TaskStop",
    "TodoRead", "TodoWrite", "WebFetch", "WebSearch", "Write", "OffPeakCreate", "OffPeakList",
    "SendMessage", "ReadSessionContext", "CreateWorkflow", "AmendWorkflow", "SaveWorkflow",
    "EvalWorkflowSnippet", "ListWorkflowRuns", "GetWorkflowRun", "ResumeWorkflowRun",
    "ResolveWorkflowQuestion", "ListSavedWorkflows", "ListModels",
]

# ZCode's own argument schemas, for validating what we emit.
ARG_SCHEMAS = {
    'Bash': {'command', 'description', 'timeout', 'run_in_background'},
    'Edit': {'file_path', 'old_string', 'new_string', 'replace_all'},
    'Read': {'file_path', 'limit', 'offset'},
    'Write': {'file_path', 'content'},
    'Skill': {'skill', 'args'},
    'TaskOutput': {'task_id', 'block', 'timeout'},
    'TaskStop': {'task_id'},
    'TodoWrite': {'todos'},
    'TodoRead': set(),
    'Agent': {'description', 'prompt', 'subagent_type', 'run_in_background'},
    'AskUserQuestion': {'questions'},
    'WebSearch': {'query'},
    'WebFetch': {'url', 'prompt'},
    'SendMessage': {'to', 'message', 'summary'},
    'ReadSessionContext': {'sessionId', 'query', 'strategy', 'maxTokens'},
    'EnterPlanMode': set(),
    'ExitPlanMode': {'plan', 'allowedPrompts'},
    'ListModels': set(),
}

TODO_STATES = ('pending', 'in_progress', 'completed', 'cancelled')
TODO_PRIORITIES = ('high', 'medium', 'low')


def map_tool(name):
    """ZCode name for a DSH tool, falling back to the original name."""
    return TOOL_MAP.get(name, name or 'Bash')


def fix_args(zname, a):
    """Rewrite a DSH tool input into the ZCode schema for `zname`.

    Every branch preserves the meaning of the original call; nothing is invented
    and no key is dropped from the result, because the object is replayed as
    history rather than re-executed.
    """
    if not isinstance(a, dict):
        return a

    if zname == 'Skill':
        out = {'skill': a.get('name') or a.get('skill') or 'unknown'}
        if a.get('args'):
            out['args'] = a['args'] if isinstance(a['args'], str) else json.dumps(a['args'], ensure_ascii=False)
        return out

    if zname == 'Bash':
        out = {'command': a.get('command') or a.get('cmd') or '',
               'description': a.get('description') or 'command'}
        if a.get('run_in_background'):
            out['run_in_background'] = True
        if a.get('timeoutMs'):
            out['timeout'] = int(a['timeoutMs'] / 1000)
        elif a.get('timeout_ms'):
            out['timeout'] = int(a['timeout_ms'] / 1000)
        return out

    if zname == 'TaskOutput':
        job = a.get('job_id') or a.get('task_id') or 'unknown'
        return {'task_id': job, 'block': bool(a.get('wait', True)),
                'timeout': int(a.get('timeout_ms', a.get('timeout', 30000)))}

    if zname == 'TaskStop':
        return {'task_id': a.get('job_id') or a.get('task_id') or 'unknown'}

    if zname == 'Read':
        out = {'file_path': a.get('file_path') or a.get('path') or ''}
        for k in ('limit', 'offset'):
            if a.get(k) is not None:
                out[k] = a[k]
        return out

    if zname == 'TodoWrite':
        todos = a.get('todos')
        if not isinstance(todos, list) or not todos:
            # goal tools carry an objective instead of a todo list
            seed = a.get('objective') or a.get('action') or a.get('goal_id') or 'migrated goal state'
            todos = [{'content': str(seed)[:400], 'status': 'in_progress', 'priority': 'medium'}]
        out = []
        for td in todos[:60]:
            if not isinstance(td, dict):
                continue
            status = td.get('status') if td.get('status') in TODO_STATES else 'pending'
            prio = td.get('priority') if td.get('priority') in TODO_PRIORITIES else 'medium'
            out.append({'content': str(td.get('content') or td.get('text') or '')[:500],
                        'status': status, 'priority': prio})
        return {'todos': out or [{'content': 'migrated todo', 'status': 'pending',
                                 'priority': 'medium'}]}

    if zname in ('TodoRead', 'EnterPlanMode', 'ListModels'):
        return {}

    if zname == 'Agent':
        return {'description': a.get('description') or 'subagent task',
                'prompt': a.get('prompt') or json.dumps(a, ensure_ascii=False)[:4000],
                'subagent_type': a.get('subagent_type') or 'general'}

    if zname == 'AskUserQuestion':
        qs = a.get('questions')
        if isinstance(qs, list) and qs:
            return {'questions': qs}
        return {'questions': [{'question': str(a.get('question') or 'continue?'),
                               'header': 'Confirm',
                               'options': [{'label': 'Continue', 'description': 'proceed'}]}]}

    if zname == 'WebSearch':
        q = a.get('query')
        if not q and isinstance(a.get('queries'), list) and a['queries']:
            q = a['queries'][0]
        return {'query': q or ''}

    if zname == 'WebFetch':
        return {'url': a.get('url') or '',
                'prompt': a.get('prompt') or 'summarize the page'}

    return a
