from __future__ import annotations

import asyncio
import copy
import logging
import time
import weakref
from datetime import timedelta
from uuid import uuid4

from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials
from open_webui.models.chat_messages import ChatMessages
from open_webui.models.chats import ChatForm, Chats
from open_webui.models.config import Config
from open_webui.models.auths import Auths
from open_webui.models.models import Models
from open_webui.models.users import UserModel, Users
from open_webui.tasks import create_task, has_active_tasks
from open_webui.utils.ask_user import request_user_input
from open_webui.utils.auth import VERIFIED_USER_ROLES, create_token
from open_webui.utils.delegations import (
    get_delegation,
    mark_orphaned,
    register_delegation,
    resolve_child_chat_id,
    update_delegation,
)
from open_webui.utils.json_codec import JSONCodec
from open_webui.utils.misc import add_or_update_system_message, get_message_list
from open_webui.utils.subagent_roles import (
    AGENT_TAG,
    DEFAULT_CONTACT_ALLOWANCE,
    agent_tool_ids,
    is_agent_model,
    needs_quota_approval,
    parse_decision,
    quota_question,
    summon_question,
)
from open_webui.utils.tool_search import strip_deferred_tools_manifest
from starlette.datastructures import Headers

log = logging.getLogger(__name__)

DEFAULT_SUBAGENT_SYSTEM_PROMPT = """You are a sub-agent working on a specific task assigned by the lead agent.

You have full access to the workspace — you can read, write, edit files, and run commands.
Focus exclusively on your assigned task. Do NOT work on anything outside your scope.

When done, end with a clear summary:
- What you did
- What files you changed (if any)
- Any issues or open questions
"""

MUTATING_MEMORY_TOOLS = {
    'add_memory',
    'delete_memory',
    'replace_memory_content',
    'update_memory',
}

_background_active: set[str] = set()
_background_lock = asyncio.Lock()
_foreground_semaphore: asyncio.Semaphore | None = None
_parent_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


async def _build_request(source: Request, user_id: str, *, internal: bool, is_subagent: bool = False) -> Request:
    scope = {
        'type': 'http',
        'asgi': {'version': '3.0', 'spec_version': '2.0'},
        'method': 'POST',
        'path': '/api/v1/subagents/internal',
        'query_string': b'',
        'headers': Headers({}).raw,
        'client': ('127.0.0.1', 0),
        'server': ('127.0.0.1', 80),
        'scheme': 'http',
        'app': source.app,
    }
    request = Request(scope)
    auth = await Auths.get_auth_by_id(user_id)
    if auth is None or not auth.active:
        raise ValueError('Subagent owner is no longer active')
    token = create_token(
        data={'id': user_id, 'typ': 'subagent', 'session_stamp': auth.session_stamp},
        expires_delta=timedelta(hours=1),
    )
    request.state.token = HTTPAuthorizationCredentials(scheme='Bearer', credentials=token)
    request.state.enable_api_keys = False
    if internal:
        request.state.internal = True
    if is_subagent:
        request.state.is_subagent = True
    return request


async def process_pending_internal_messages(
    source_request: Request,
    parent_chat_id: str,
    user_id: str,
    run: dict,
) -> None:
    lock = _parent_locks.setdefault(parent_chat_id, asyncio.Lock())
    while await has_active_tasks(source_request.app.state.redis, parent_chat_id):
        await asyncio.sleep(0.25)

    async with lock:
        if await has_active_tasks(source_request.app.state.redis, parent_chat_id):
            return

        user = await Users.get_user_by_id(user_id)
        if not user or user.role not in VERIFIED_USER_ROLES:
            return

        async with Chats._chat_transaction(parent_chat_id) as (_, chat):
            if not chat or chat.user_id != user_id:
                return

            history = copy.deepcopy((chat.chat or {}).get('history') or {})
            messages = history.get('messages') or {}
            pending = [
                message
                for message in messages.values()
                for meta in [message.get('meta') or {}]
                if message.get('role') == 'user'
                and not message.get('childrenIds')
                and (
                    (
                        meta.get('internal') is True
                        and meta.get('type') == 'subagent'
                        and meta.get('status') in (None, 'pending')
                    )
                    or (meta.get('internal') is True and meta.get('type') == 'timer')
                )
            ]
            if not pending:
                return

            first = pending[0]
            first_meta = first.get('meta') or {}
            kind = 'timer' if first_meta.get('internal') is True and first_meta.get('type') == 'timer' else 'subagent'
            parent_id = first.get('parentId')
            if kind == 'timer' and first_meta.get('timer_id'):
                timer = await Chats.get_chat_by_id(first_meta['timer_id'])
                timer_run = ((timer.meta or {}).get('run') if timer else None) or {}
                if timer_run:
                    run = {**run, **timer_run, 'chat_context': timer_run.get('chat_context')}
            model_id = first.get('model') or run['model_id']
            if kind == 'timer':
                batch = [first]
            else:
                batch = [
                    message
                    for message in pending
                    for meta in [message.get('meta') or {}]
                    if message.get('parentId') == parent_id
                    and (message.get('model') or model_id) == model_id
                    and (
                        meta.get('internal') is True
                        and meta.get('type') == 'subagent'
                        and meta.get('status') in (None, 'pending')
                    )
                ]
            combined_content = '\n\n'.join(message.get('content', '') for message in batch if message.get('content'))
            if kind == 'timer':
                timer_ids = [
                    message['meta']['timer_id'] for message in batch if (message.get('meta') or {}).get('timer_id')
                ]
                combined_meta = {'internal': True, 'type': 'timer'}
                if len(timer_ids) == 1:
                    combined_meta['timer_id'] = timer_ids[0]
                elif timer_ids:
                    combined_meta['timer_ids'] = timer_ids
            else:
                delegation_ids = [
                    message['meta']['delegation_id']
                    for message in batch
                    if (message.get('meta') or {}).get('delegation_id')
                ]
                subagent_chat_ids = [
                    message['meta']['subagent_chat_id']
                    for message in batch
                    if (message.get('meta') or {}).get('subagent_chat_id')
                ]
                combined_meta = {'internal': True, 'type': 'subagent'}
                if len(delegation_ids) == 1:
                    combined_meta['delegation_id'] = delegation_ids[0]
                elif delegation_ids:
                    combined_meta['delegation_ids'] = delegation_ids
                if len(subagent_chat_ids) == 1:
                    combined_meta['subagent_chat_id'] = subagent_chat_ids[0]
                elif subagent_chat_ids:
                    combined_meta['subagent_chat_ids'] = subagent_chat_ids

            reuse_message = len(batch) == 1 and (first.get('meta') or {}).get('status') != 'pending'
            user_message_id = first['id'] if reuse_message else str(uuid4())
            removed_ids = set()
            if not reuse_message:
                removed_ids = {message['id'] for message in batch}
                for message_id in removed_ids:
                    messages.pop(message_id, None)
                if parent_id and parent_id in messages:
                    messages[parent_id]['childrenIds'] = [
                        child_id
                        for child_id in messages[parent_id].get('childrenIds', [])
                        if child_id not in removed_ids
                    ]

            assistant_message_id = str(uuid4())
            message_list = get_message_list(messages, parent_id)
            user_message = {
                'id': user_message_id,
                'parentId': parent_id,
                'childrenIds': [assistant_message_id],
                'role': 'user',
                'content': combined_content,
                'model': model_id,
                'meta': combined_meta,
                'timestamp': int(time.time()),
            }
            assistant_message = {
                'id': assistant_message_id,
                'parentId': user_message_id,
                'childrenIds': [],
                'role': 'assistant',
                'content': '',
                'done': False,
                'model': model_id,
                'timestamp': int(time.time()),
            }

            if parent_id and parent_id in messages:
                parent_children = [
                    child_id for child_id in messages[parent_id].get('childrenIds', []) if child_id != user_message_id
                ]
                parent_children.append(user_message_id)
                messages[parent_id]['childrenIds'] = parent_children
            messages[user_message_id] = {**messages.get(user_message_id, {}), **user_message}
            messages[assistant_message_id] = assistant_message
            history['messages'] = messages
            history['currentId'] = assistant_message_id
            chat.chat = {**(chat.chat or {}), 'history': history}
            chat.current_message_id = assistant_message_id
            chat.updated_at = int(time.time())

        if removed_ids:
            await ChatMessages.delete_message_ids_by_chat_id(parent_chat_id, removed_ids)
        await ChatMessages.upsert_message(user_message_id, parent_chat_id, user_id, user_message)
        await ChatMessages.upsert_message(assistant_message_id, parent_chat_id, user_id, assistant_message)

        from open_webui.socket.main import sio

        await sio.emit(
            'events',
            {
                'chat_id': parent_chat_id,
                'message_id': assistant_message_id,
                'data': {'type': 'chat:reload'},
            },
            room=f'user:{user.id}',
        )

        chat_context = copy.deepcopy(run.get('chat_context') or {})
        # Runs saved before chat_context was introduced only have the assembled prompt.
        if run.get('chat_context') is None and run.get('system_prompt'):
            chat_context['messages'] = [
                {'role': 'system', 'content': strip_deferred_tools_manifest(run['system_prompt'])}
            ]
        form_data = {
            'model': model_id,
            'params': chat_context.get('params') or {},
            'messages': [
                *chat_context.get('messages', []),
                *message_list,
                {'role': 'user', 'content': combined_content},
            ],
            'stream': True,
            'chat_id': parent_chat_id,
            'id': assistant_message_id,
            'parent_id': parent_id,
            'user_message': user_message,
            'session_id': run.get('session_id') or f'{kind}-result:{parent_chat_id}',
            'background_tasks': {},
            'tool_ids': run.get('tool_ids') or [],
            'skill_ids': run.get('skill_ids') or [],
            'filter_ids': run.get('filter_ids') or [],
            'features': run.get('features') or {},
            'files': run.get('files') or [],
            'variables': run.get('variables') or {},
            'chat_variables': chat_context.get('chat_variables'),
        }
        if run.get('terminal_id'):
            form_data['terminal_id'] = run['terminal_id']

        request = await _build_request(source_request, user.id, internal=False)
        request.state.chat_context = chat_context
        await source_request.app.state.CHAT_COMPLETION_HANDLER(request, form_data, user=user)


async def delegate(
    task: str,
    context: str,
    background: bool,
    *,
    file_ids: list[str] | None = None,
    request: Request,
    user_data: dict,
    metadata: dict,
    parent_chat_id: str,
    parent_message_id: str | None,
) -> str:
    global _foreground_semaphore

    task = task.strip()
    if not task:
        return 'Error: task must not be empty.'
    if not parent_chat_id or not user_data.get('id'):
        return 'Error: chat and user context are required.'

    config = await Config.get_many(
        'subagents.background_enabled',
        'subagents.max_concurrent',
        'subagents.max_async',
        'subagents.max_iterations',
        'subagents.max_output',
        'subagents.system_prompt',
    )
    max_concurrent = int(config.get('subagents.max_concurrent') or 20)
    max_async = int(config.get('subagents.max_async') or 20)
    max_iterations = int(config.get('subagents.max_iterations') or 30)
    max_output = int(config.get('subagents.max_output') or 30_000)
    if max_concurrent != -1:
        max_concurrent = max(1, max_concurrent)
    if max_async != -1:
        max_async = max(1, max_async)

    if background and not config.get('subagents.background_enabled'):
        return 'Error: background sub-agents are disabled in settings.'

    features = copy.deepcopy(metadata.get('features') or {})
    if (
        background
        and features.get('code_interpreter')
        and await Config.get('code_interpreter.engine', 'pyodide') != 'jupyter'
    ):
        features.pop('code_interpreter')
    folder_id = await Chats.get_chat_folder_id(parent_chat_id, user_data['id']) or metadata.get('folder_id')
    run = {
        'model_id': metadata.get('model_id') or (metadata.get('model') or {}).get('id'),
        'session_id': metadata.get('session_id'),
        'tool_ids': copy.deepcopy(metadata.get('tool_ids') or []),
        'skill_ids': copy.deepcopy(metadata.get('skill_ids') or []),
        'chat_context': copy.deepcopy(metadata.get('chat_context') or {}),
        'tool_servers': [] if background else copy.deepcopy(metadata.get('tool_servers') or []),
        'filter_ids': copy.deepcopy(metadata.get('filter_ids') or []),
        'terminal_id': metadata.get('terminal_id'),
        'features': features,
        'files': copy.deepcopy(metadata.get('files') or []),
        'variables': copy.deepcopy(metadata.get('variables') or {}),
        'direct': bool(metadata.get('direct')),
        'folder_id': folder_id,
    }
    if not run.get('model_id'):
        return 'Error: model context is required.'
    if run.get('direct'):
        return 'Error: sub-agents are unavailable for direct connections.'
    if file_ids:
        requested_file_ids = {str(file_id) for file_id in file_ids if file_id}
        run['files'] = [
            copy.deepcopy(file)
            for file in metadata.get('files') or []
            if str(file.get('id') or '') in requested_file_ids
            or str(file.get('url') or '') in requested_file_ids
            or (isinstance(file.get('file'), dict) and str(file.get('file', {}).get('id') or '') in requested_file_ids)
        ]
        found_file_ids = {
            str(value)
            for file in run['files']
            for value in (
                file.get('id'),
                file.get('url'),
                file.get('file', {}).get('id') if isinstance(file.get('file'), dict) else None,
            )
            if value
        }
        missing_file_ids = sorted(requested_file_ids - found_file_ids)
        if missing_file_ids:
            return f'Error: file_ids not attached or unavailable: {", ".join(missing_file_ids)}'
    else:
        run['files'] = []

    delegation_id = f'deleg_{uuid4().hex[:8]}'
    foreground_semaphore = None
    if background:
        async with _background_lock:
            if max_async != -1 and len(_background_active) >= max_async:
                return (
                    f'Error: Async subagent capacity reached ({max_async} running). '
                    'Wait for one to finish or increase subagents.max_async.'
                )
            _background_active.add(delegation_id)
    elif max_concurrent != -1:
        if _foreground_semaphore is None:
            _foreground_semaphore = asyncio.Semaphore(max_concurrent)
        foreground_semaphore = _foreground_semaphore
        await foreground_semaphore.acquire()

    mode = 'background' if background else 'foreground'
    try:
        user = UserModel(**user_data)
        chat_id = str(uuid4())
        user_message_id = str(uuid4())
        assistant_message_id = str(uuid4())
        prompt = f'{task}\n\n## Context\n{context}' if context else task
        prompt_files = copy.deepcopy(run.get('files') or [])
        user_message = {
            'id': user_message_id,
            'parentId': None,
            'childrenIds': [assistant_message_id],
            'role': 'user',
            'content': prompt,
            'timestamp': int(time.time()),
            'models': [run['model_id']],
            **({'files': prompt_files} if prompt_files else {}),
        }
        chat = await Chats.insert_new_chat(
            chat_id,
            user.id,
            ChatForm(
                chat={
                    'id': chat_id,
                    'title': f'Sub-agent: {task[:60]}',
                    'models': [run['model_id']],
                    'history': {
                        'currentId': assistant_message_id,
                        'messages': {
                            user_message_id: user_message,
                            assistant_message_id: {
                                'id': assistant_message_id,
                                'parentId': user_message_id,
                                'childrenIds': [],
                                'role': 'assistant',
                                'content': '',
                                'done': False,
                                'model': run['model_id'],
                                'timestamp': int(time.time()),
                            },
                        },
                    },
                    'messages': [
                        {
                            'role': 'user',
                            'content': prompt,
                            **({'files': prompt_files} if prompt_files else {}),
                        }
                    ],
                    'files': prompt_files,
                }
            ),
            internal_meta={
                'internal': True,
                'type': 'subagent',
                'parent_chat_id': parent_chat_id,
                'parent_message_id': parent_message_id,
                'delegation_id': delegation_id,
                'mode': mode,
            },
        )
        if not chat:
            raise RuntimeError('Failed to create sub-agent chat')
    except Exception as exc:
        if background:
            async with _background_lock:
                _background_active.discard(delegation_id)
        elif foreground_semaphore:
            foreground_semaphore.release()
        prefix = 'background ' if background else ''
        return f'Error: failed to create {prefix}sub-agent: {exc}'

    async def run_reserved() -> dict:
        try:
            child_request = await _build_request(request, user.id, internal=True)
            child_request.state.max_tool_call_iterations = max_iterations
            chat_context = copy.deepcopy(run.get('chat_context') or {})
            child_request.state.chat_context = chat_context
            subagent_system_prompt = (
                str(config.get('subagents.system_prompt') or '').strip() or DEFAULT_SUBAGENT_SYSTEM_PROMPT
            )
            form_data = {
                'model': run['model_id'],
                'params': chat_context.get('params') or {},
                'messages': add_or_update_system_message(
                    subagent_system_prompt,
                    [*chat_context.get('messages', []), {'role': 'user', 'content': prompt}],
                    append=True,
                ),
                'stream': True,
                'chat_id': chat_id,
                'id': assistant_message_id,
                'parent_id': None,
                'user_message': user_message,
                'session_id': run.get('session_id') or f'subagent:{chat_id}',
                'background_tasks': {},
                'tool_ids': run.get('tool_ids') or [],
                'skill_ids': run.get('skill_ids') or [],
                'filter_ids': run.get('filter_ids') or [],
                'features': run.get('features') or {},
                'files': run.get('files') or [],
                'variables': run.get('variables') or {},
                'chat_variables': chat_context.get('chat_variables'),
                'folder_id': run.get('folder_id'),
            }
            if run.get('terminal_id'):
                form_data['terminal_id'] = run['terminal_id']
            if run.get('tool_servers'):
                form_data['tool_servers'] = run['tool_servers']
            await request.app.state.CHAT_COMPLETION_HANDLER(child_request, form_data, user=user)
            message = await Chats.get_message_by_id_and_message_id(chat_id, assistant_message_id)
            if not message:
                return {
                    'status': 'error',
                    'summary': '',
                    'error': 'Sub-agent chat or completion message no longer exists.',
                }

            summary = message.get('content') or ''
            if isinstance(summary, list):
                summary = ''.join(
                    str(item.get('text', ''))
                    for item in summary
                    if isinstance(item, dict) and item.get('type') == 'text'
                )
            if not summary:
                summary = ''.join(
                    str(part.get('text', ''))
                    for item in message.get('output') or []
                    if item.get('type') == 'message'
                    for part in item.get('content') or []
                    if part.get('type') == 'output_text'
                )
            if len(summary) > max_output:
                summary = f'{summary[:max_output]}\n\n[output truncated]'
            error = message.get('error')
            return {
                'status': 'error' if error else 'completed',
                'summary': summary or ('Sub-agent produced no output.' if not error else ''),
                'error': error,
            }
        except asyncio.CancelledError:
            await Chats.upsert_message_to_chat_by_id_and_message_id(
                chat_id,
                assistant_message_id,
                {'done': True, 'error': {'content': 'Sub-agent cancelled.'}},
            )
            raise
        except Exception as exc:
            await Chats.upsert_message_to_chat_by_id_and_message_id(
                chat_id,
                assistant_message_id,
                {'done': True, 'error': {'content': str(exc)}},
            )
            raise
        finally:
            if background:
                async with _background_lock:
                    _background_active.discard(delegation_id)
            elif foreground_semaphore:
                foreground_semaphore.release()

    async def run_background() -> dict:
        started_at = time.time()
        cancelled = False
        try:
            result = await run_reserved()
        except asyncio.CancelledError:
            result = {'status': 'interrupted', 'summary': '', 'error': 'cancelled'}
            cancelled = True
        except Exception as exc:
            result = {'status': 'error', 'summary': '', 'error': str(exc)}

        duration = f'{time.time() - started_at:.1f}s'
        lines = [
            f'[ASYNC SUBAGENT COMPLETE - {delegation_id}]',
            (
                'A background subagent you dispatched earlier has finished. '
                'The original task source is included so you can decide whether '
                'to use the result or continue without it.'
            ),
            '',
            f'Original task: {task}',
        ]
        if context:
            lines.append(f'Context provided: {context}')
        lines.extend(
            [
                f'Subagent chat: {chat_id}',
                f'Status: {result.get("status", "completed")}   Duration: {duration}',
                '--- RESULT ---',
            ]
        )
        if result.get('status') == 'completed':
            lines.append(result.get('summary') or 'Subagent completed without a final summary.')
        elif result.get('status') == 'interrupted':
            lines.append('The subagent was interrupted before completing.')
            if result.get('summary'):
                lines.extend(['Partial output:', result['summary']])
        else:
            detail = f' {result.get("error")}' if result.get('error') else ''
            lines.append(f'The subagent did not complete successfully.{detail}')
            if result.get('summary'):
                lines.extend(['Partial output:', result['summary']])

        pending_message_id = str(uuid4())
        pending_meta = {
            'internal': True,
            'type': 'subagent',
            'delegation_id': delegation_id,
            'subagent_chat_id': chat_id,
        }
        pending_message = {
            'id': pending_message_id,
            'parentId': None,
            'childrenIds': [],
            'role': 'user',
            'content': '\n'.join(lines),
            'model': run['model_id'],
            'meta': pending_meta,
            'timestamp': int(time.time()),
        }

        lock = _parent_locks.setdefault(parent_chat_id, asyncio.Lock())
        async with lock:
            async with Chats._chat_transaction(parent_chat_id) as (_, parent):
                if not parent or parent.user_id != user.id:
                    if cancelled:
                        raise asyncio.CancelledError
                    return result

                updated_chat = copy.deepcopy(parent.chat or {})
                updated_history = updated_chat.setdefault('history', {})
                updated_messages = updated_history.setdefault('messages', {})
                parent_message = updated_messages.get(parent_message_id)
                done_assistants = [
                    message
                    for message_id, message in updated_messages.items()
                    if message.get('role') == 'assistant'
                    and message.get('done') is not False
                    and (
                        parent_message is None
                        or any(entry is parent_message for entry in get_message_list(updated_messages, message_id))
                    )
                ]
                result_parent_id = (
                    max(done_assistants, key=lambda message: message.get('timestamp', 0)).get('id')
                    if done_assistants
                    else parent_message_id
                )
                pending_message['parentId'] = result_parent_id
                if await has_active_tasks(request.app.state.redis, parent_chat_id):
                    pending_message['meta']['status'] = 'pending'
                updated_messages[pending_message_id] = pending_message
                if result_parent_id and result_parent_id in updated_messages:
                    children = updated_messages[result_parent_id].setdefault('childrenIds', [])
                    if pending_message_id not in children:
                        children.append(pending_message_id)
                updated_history['messages'] = updated_messages
                parent.chat = {**(parent.chat or {}), **updated_chat, 'history': updated_history}
                parent.updated_at = int(time.time())

            await ChatMessages.upsert_message(
                message_id=pending_message_id,
                chat_id=parent_chat_id,
                user_id=user.id,
                data=pending_message,
            )

        if pending_message['meta'].get('status') == 'pending':
            from open_webui.socket.main import sio

            await sio.emit(
                'events',
                {
                    'chat_id': parent_chat_id,
                    'message_id': pending_message_id,
                    'data': {'type': 'chat:reload'},
                },
                room=f'user:{user.id}',
            )
        if not await has_active_tasks(request.app.state.redis, parent_chat_id):
            await process_pending_internal_messages(request, parent_chat_id, user.id, run)
        if cancelled:
            raise asyncio.CancelledError
        return result

    try:
        _, child_task = await create_task(
            request.app.state.redis,
            run_background() if background else run_reserved(),
            id=chat_id,
        )
    except Exception as exc:
        if background:
            async with _background_lock:
                _background_active.discard(delegation_id)
        elif foreground_semaphore:
            foreground_semaphore.release()
        return f'Error: {exc}'

    if background:
        return JSONCodec.dumps(
            {
                'status': 'dispatched',
                'delegation_id': delegation_id,
                'subagent_chat_id': chat_id,
                'mode': 'background',
                'task': task,
            },
            ensure_ascii=False,
        )

    try:
        result = await child_task
    except asyncio.CancelledError:
        if asyncio.current_task() and asyncio.current_task().cancelling():
            raise
        return 'Error: sub-agent was cancelled.'
    except Exception as exc:
        return f'Error: {exc}'

    if result.get('status') != 'completed':
        return f'Error: {result.get("error") or "sub-agent failed."}'
    return result.get('summary') or 'Sub-agent produced no output.'


# =============================================================================
# Visible role sessions
# =============================================================================


def _select_files(metadata: dict, file_ids: list[str] | None) -> list[dict]:
    files = copy.deepcopy(metadata.get('files') or [])
    if not file_ids:
        return []
    wanted = {str(file_id) for file_id in file_ids if file_id}
    selected = []
    for entry in files:
        values = {str(entry.get('id') or ''), str(entry.get('url') or '')}
        if isinstance(entry.get('file'), dict):
            values.add(str(entry['file'].get('id') or ''))
        if values & wanted:
            selected.append(entry)
    return selected


def _parent_run(metadata: dict) -> dict:
    return {
        'model_id': metadata.get('model_id') or (metadata.get('model') or {}).get('id'),
        'session_id': metadata.get('session_id'),
        'tool_ids': copy.deepcopy(metadata.get('tool_ids') or []),
        'skill_ids': copy.deepcopy(metadata.get('skill_ids') or []),
        'chat_context': copy.deepcopy(metadata.get('chat_context') or {}),
        'filter_ids': copy.deepcopy(metadata.get('filter_ids') or []),
        'terminal_id': metadata.get('terminal_id'),
        'features': copy.deepcopy(metadata.get('features') or {}),
        'variables': copy.deepcopy(metadata.get('variables') or {}),
        'chat_variables': copy.deepcopy(metadata.get('chat_variables')),
        'files': copy.deepcopy(metadata.get('files') or []),
    }


async def delegate_session(
    task: str,
    context: str,
    *,
    agent: str,
    file_ids: list[str] | None,
    request: Request,
    user_data: dict,
    metadata: dict,
    parent_chat_id: str,
    parent_message_id: str | None,
    event_call,
) -> str:
    """Summon a role model in a visible, user-owned chat.

    Approval happens before the chat exists. The child runs with isolated context
    (task + explicit context + files) and the parent is not blocked.
    """
    task = (task or '').strip()
    if not task:
        return 'Error: task must not be empty.'
    if not agent:
        return 'Error: agent is required for a visible session.'
    if not parent_chat_id or not user_data.get('id'):
        return 'Error: chat and user context are required.'

    role_model = await Models.get_model_by_id(agent)
    if not role_model:
        return f'Error: agent model {agent!r} not found.'
    role_meta = role_model.meta.model_dump() if role_model.meta else {}
    if not is_agent_model(role_meta, role_model.base_model_id):
        return f'Error: {agent!r} is not a delegatable agent (missing tag {AGENT_TAG}).'
    role_name = role_model.name or agent
    role_tool_ids = agent_tool_ids(role_meta)

    approval = await request_user_input(event_call, [summon_question(role_name, task)])
    if approval.get('status') != 'answered':
        return JSONCodec.dumps(
            {
                'status': 'error',
                'error': approval.get('error') or 'Summon approval requires an active browser session.',
            },
            ensure_ascii=False,
        )
    action, _, correction = parse_decision(approval.get('answers'), 'summon')
    if action != 'approve':
        return JSONCodec.dumps(
            {'status': 'rejected', 'reason': correction or 'User rejected the summon.'},
            ensure_ascii=False,
        )

    user = UserModel(**user_data)
    parent_run = _parent_run(metadata)
    prompt_files = _select_files(metadata, file_ids)
    session_id = metadata.get('session_id') or ''
    child_chat_context = {'messages': [], 'params': {}}
    child_run = {
        **copy.deepcopy(parent_run),
        'model_id': role_model.id,
        'tool_ids': role_tool_ids,
        'chat_context': child_chat_context,
        'files': [],
    }

    delegation_id = f'deleg_{uuid4().hex[:8]}'
    chat_id = str(uuid4())
    user_message_id = str(uuid4())
    assistant_message_id = str(uuid4())
    prompt = f'{task}\n\n## Context\n{context}' if context else task

    user_message = {
        'id': user_message_id,
        'parentId': None,
        'childrenIds': [assistant_message_id],
        'role': 'user',
        'content': prompt,
        'timestamp': int(time.time()),
        'models': [role_model.id],
        **({'files': prompt_files} if prompt_files else {}),
    }
    assistant_message = {
        'id': assistant_message_id,
        'parentId': user_message_id,
        'childrenIds': [],
        'role': 'assistant',
        'content': '',
        'done': False,
        'model': role_model.id,
        'timestamp': int(time.time()),
    }
    delegation = {
        'id': delegation_id,
        'agent': agent,
        'agent_name': role_name,
        'parent_chat_id': parent_chat_id,
        'parent_message_id': parent_message_id,
        'child_chat_id': chat_id,
        'status': 'active',
        'contact_count': 0,
        'contact_allowance': DEFAULT_CONTACT_ALLOWANCE,
        'parent_run': parent_run,
        'child_run': child_run,
        'created_at': int(time.time()),
        'updated_at': int(time.time()),
    }

    try:
        chat = await Chats.insert_new_chat(
            chat_id,
            user.id,
            ChatForm(
                chat={
                    'id': chat_id,
                    'title': f'{role_name} — {task[:60]}',
                    'models': [role_model.id],
                    'history': {
                        'currentId': assistant_message_id,
                        'messages': {
                            user_message_id: user_message,
                            assistant_message_id: assistant_message,
                        },
                    },
                    'messages': [
                        {
                            'role': 'user',
                            'content': prompt,
                            **({'files': prompt_files} if prompt_files else {}),
                        }
                    ],
                    'files': prompt_files,
                }
            ),
            internal_meta={'internal': False, 'delegation': delegation},
        )
    except Exception as exc:
        log.exception('Failed to create visible subagent chat')
        return f'Error: failed to create the subagent session: {exc}'
    if not chat:
        return 'Error: failed to create the subagent session.'

    await register_delegation(parent_chat_id, delegation_id, chat_id)

    child_request = await _build_request(request, user.id, internal=False, is_subagent=True)
    child_request.state.chat_context = child_chat_context
    child_request.state.max_tool_call_iterations = int(await Config.get('subagents.max_iterations', 30))
    form_data = {
        'model': role_model.id,
        'messages': [{'role': 'user', 'content': prompt}],
        'stream': True,
        'chat_id': chat_id,
        'id': assistant_message_id,
        'parent_id': None,
        'user_message': user_message,
        'session_id': session_id or f'subagent:{chat_id}',
        'background_tasks': {},
        'tool_ids': role_tool_ids,
        'skill_ids': copy.deepcopy(metadata.get('skill_ids') or []),
        'filter_ids': copy.deepcopy(metadata.get('filter_ids') or []),
        'features': copy.deepcopy(metadata.get('features') or {}),
        'files': prompt_files,
        'variables': copy.deepcopy(metadata.get('variables') or {}),
        'chat_variables': copy.deepcopy(metadata.get('chat_variables')),
    }
    if metadata.get('terminal_id'):
        form_data['terminal_id'] = metadata['terminal_id']
    if metadata.get('folder_id'):
        form_data['folder_id'] = metadata['folder_id']

    async def run_child() -> None:
        try:
            await request.app.state.CHAT_COMPLETION_HANDLER(child_request, form_data, user=user)
        except Exception:
            log.exception('Visible subagent completion failed for %s', chat_id)

    await create_task(request.app.state.redis, run_child(), id=chat_id)

    link = f'{str(request.base_url).rstrip("/")}/c/{chat_id}'
    return JSONCodec.dumps(
        {
            'status': 'dispatched',
            'delegation_id': delegation_id,
            'agent': role_name,
            'chat_id': chat_id,
            'link': link,
            'message': f'{role_name} summoned. Chat with the subagent: {link}',
        },
        ensure_ascii=False,
    )


async def _append_parent_message(
    parent_chat,
    *,
    delegation: dict,
    content: str,
    status: str,
    agent_name: str,
    user_id: str,
) -> str:
    history = (parent_chat.chat or {}).get('history') or {}
    messages = history.get('messages') or {}
    latest = parent_chat.current_message_id or history.get('currentId')
    if latest not in messages:
        latest = next(reversed(messages), None) if messages else None

    message_id = str(uuid4())
    message = {
        'id': message_id,
        'parentId': latest,
        'childrenIds': [],
        'role': 'user',
        'content': f'[Subagent {agent_name}]\n\n{content}',
        'model': (delegation.get('parent_run') or {}).get('model_id'),
        'meta': {
            'internal': True,
            'type': 'subagent',
            'delegation_id': delegation.get('id'),
            'from': 'subagent',
            'agent': agent_name,
            'status': status,
        },
        'timestamp': int(time.time()),
    }
    await Chats.upsert_message_to_chat_by_id_and_message_id(parent_chat.id, message_id, message)

    from open_webui.socket.main import sio

    await sio.emit(
        'events',
        {'chat_id': parent_chat.id, 'message_id': message_id, 'data': {'type': 'chat:reload'}},
        room=f'user:{user_id}',
    )
    return message_id


async def _append_child_message(child_chat_id: str, content: str, user_id: str) -> None:
    chat = await Chats.get_chat_by_id(child_chat_id)
    if not chat:
        return
    history = (chat.chat or {}).get('history') or {}
    messages = history.get('messages') or {}
    latest = chat.current_message_id or history.get('currentId')
    if latest not in messages:
        latest = next(reversed(messages), None) if messages else None

    message_id = str(uuid4())
    message = {
        'id': message_id,
        'parentId': latest,
        'childrenIds': [],
        'role': 'user',
        'content': content,
        'meta': {'from': 'parent'},
        'timestamp': int(time.time()),
    }
    await Chats.upsert_message_to_chat_by_id_and_message_id(child_chat_id, message_id, message)

    from open_webui.socket.main import sio

    await sio.emit(
        'events',
        {'chat_id': child_chat_id, 'message_id': message_id, 'data': {'type': 'chat:reload'}},
        room=f'user:{user_id}',
    )


async def report_to_parent(
    *,
    child_chat_id: str,
    user: UserModel,
    content: str,
    wake: bool,
    event_call,
    request: Request,
) -> str:
    content = (content or '').strip()
    if not content:
        return 'Error: message must not be empty.'

    _, delegation = await get_delegation(child_chat_id)
    if not delegation:
        return 'Error: this session is not a delegated subagent.'
    if delegation.get('status') in {'orphaned', 'cancelled'}:
        return 'Error: the delegation is closed; the parent cannot be contacted.'

    parent_chat_id = delegation.get('parent_chat_id')
    parent_chat = await Chats.get_chat_by_id(parent_chat_id)
    if not parent_chat:
        await mark_orphaned(child_chat_id, 'parent_deleted')
        return 'Error: the parent session was deleted; the delegation is closed.'

    agent_name = delegation.get('agent_name') or delegation.get('agent') or 'Subagent'
    count = int(delegation.get('contact_count') or 0)
    allowance = int(delegation.get('contact_allowance') or DEFAULT_CONTACT_ALLOWANCE)

    if wake and needs_quota_approval(count, allowance):
        approval = await request_user_input(event_call, [quota_question(agent_name, count)])
        if approval.get('status') != 'answered':
            return JSONCodec.dumps(
                {
                    'status': 'error',
                    'error': 'Contact approval requires an active browser session; use signal_completion_to_parent instead.',
                },
                ensure_ascii=False,
            )
        action, amount, correction = parse_decision(approval.get('answers'), 'quota')
        if action == 'approve':
            allowance = count + amount
        elif action == 'reject':
            await _append_child_message(
                child_chat_id,
                correction or 'The user rejected further parent contact. Continue without contacting the parent.',
                user.id,
            )
            await update_delegation(child_chat_id, contact_allowance=allowance)
            return JSONCodec.dumps(
                {'status': 'rejected', 'correction': correction},
                ensure_ascii=False,
            )
        else:
            return 'Error: unrecognised approval answer; the contact was not sent.'

    message_id = await _append_parent_message(
        parent_chat,
        delegation=delegation,
        content=content,
        status='pending' if wake else 'delivered',
        agent_name=agent_name,
        user_id=user.id,
    )

    if wake:
        await update_delegation(
            child_chat_id,
            contact_count=count + 1,
            contact_allowance=allowance,
            status='active',
        )
        run = delegation.get('parent_run') or {}

        async def wake_parent() -> None:
            try:
                await process_pending_internal_messages(request, parent_chat_id, user.id, run)
            except Exception:
                log.exception('Failed to wake parent %s for delegation %s', parent_chat_id, delegation.get('id'))

        asyncio.create_task(wake_parent())
        return JSONCodec.dumps({'status': 'delivered', 'message_id': message_id}, ensure_ascii=False)

    await update_delegation(child_chat_id, status='completed')
    return JSONCodec.dumps({'status': 'recorded', 'message_id': message_id}, ensure_ascii=False)


async def dispatch_to_child(
    *,
    parent_chat_id: str,
    delegation_id: str,
    content: str,
    request: Request,
    user: UserModel,
) -> str:
    content = (content or '').strip()
    if not content:
        return 'Error: message must not be empty.'

    child_chat_id = await resolve_child_chat_id(parent_chat_id, delegation_id)
    if not child_chat_id:
        return 'Error: delegation not found for this parent chat.'
    chat, delegation = await get_delegation(child_chat_id)
    if not delegation or not chat:
        return 'Error: delegation record is missing.'
    if delegation.get('status') in {'orphaned', 'cancelled'}:
        return 'Error: the delegation is closed.'

    child_run = delegation.get('child_run') or {}
    model_id = child_run.get('model_id')
    if not model_id:
        return 'Error: the subagent run context is missing.'

    history = (chat.chat or {}).get('history') or {}
    messages = history.get('messages') or {}
    latest = chat.current_message_id or history.get('currentId')
    if latest not in messages:
        latest = next(reversed(messages), None) if messages else None
    message_list = [
        {'role': item.get('role'), 'content': item.get('content')}
        for item in get_message_list(messages, latest)
        if item.get('role') in {'user', 'assistant'} and item.get('content')
    ]

    user_message_id = str(uuid4())
    assistant_message_id = str(uuid4())
    user_message = {
        'id': user_message_id,
        'parentId': latest,
        'childrenIds': [assistant_message_id],
        'role': 'user',
        'content': content,
        'meta': {'from': 'parent'},
        'timestamp': int(time.time()),
    }
    assistant_message = {
        'id': assistant_message_id,
        'parentId': user_message_id,
        'childrenIds': [],
        'role': 'assistant',
        'content': '',
        'done': False,
        'model': model_id,
        'timestamp': int(time.time()),
    }
    await Chats.upsert_message_to_chat_by_id_and_message_id(child_chat_id, user_message_id, user_message)
    await Chats.upsert_message_to_chat_by_id_and_message_id(child_chat_id, assistant_message_id, assistant_message)

    child_request = await _build_request(request, user.id, internal=False, is_subagent=True)
    child_chat_context = child_run.get('chat_context') or {'messages': [], 'params': {}}
    child_request.state.chat_context = child_chat_context
    child_request.state.max_tool_call_iterations = int(await Config.get('subagents.max_iterations', 30))
    form_data = {
        'model': model_id,
        'messages': [*message_list, {'role': 'user', 'content': content}],
        'stream': True,
        'chat_id': child_chat_id,
        'id': assistant_message_id,
        'parent_id': user_message_id,
        'user_message': user_message,
        'session_id': child_run.get('session_id') or f'subagent:{child_chat_id}',
        'background_tasks': {},
        'tool_ids': child_run.get('tool_ids') or [],
        'skill_ids': child_run.get('skill_ids') or [],
        'filter_ids': child_run.get('filter_ids') or [],
        'features': child_run.get('features') or {},
        'files': child_run.get('files') or [],
        'variables': child_run.get('variables') or {},
        'chat_variables': child_run.get('chat_variables'),
    }

    async def run_child() -> None:
        try:
            await request.app.state.CHAT_COMPLETION_HANDLER(child_request, form_data, user=user)
        except Exception:
            log.exception('Subagent follow-up completion failed for %s', child_chat_id)

    await create_task(request.app.state.redis, run_child(), id=child_chat_id)
    await update_delegation(child_chat_id, status='active')
    return JSONCodec.dumps({'status': 'dispatched', 'chat_id': child_chat_id}, ensure_ascii=False)
