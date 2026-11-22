"""Delegation state stored in chat metadata.

The fork currently carries several Alembic heads, so a new table would require a
migration merge unrelated to this feature. The delegation record therefore lives
in the child chat's ``meta.delegation``; the parent chat keeps a
``meta.delegations`` map from delegation id to child chat id.
"""

from __future__ import annotations

import time

from sqlalchemy.orm.attributes import flag_modified

from open_webui.models.chats import ChatModel, Chats


async def update_chat_meta(chat_id: str, updater) -> ChatModel | None:
    async with Chats._chat_transaction(chat_id) as (session, chat_item):
        if chat_item is None:
            return None
        meta = dict(chat_item.meta or {})
        updater(meta)
        chat_item.meta = meta
        chat_item.updated_at = int(time.time())
        flag_modified(chat_item, 'meta')
        return ChatModel.model_validate(chat_item)


async def register_delegation(parent_chat_id: str, delegation_id: str, child_chat_id: str) -> None:
    def update(meta: dict) -> None:
        meta.setdefault('delegations', {})[delegation_id] = child_chat_id

    await update_chat_meta(parent_chat_id, update)


async def get_delegation(child_chat_id: str) -> tuple[ChatModel | None, dict | None]:
    chat = await Chats.get_chat_by_id(child_chat_id)
    if not chat:
        return None, None
    delegation = (chat.meta or {}).get('delegation')
    return chat, delegation if isinstance(delegation, dict) else None


async def update_delegation(child_chat_id: str, **fields) -> None:
    def update(meta: dict) -> None:
        delegation = dict(meta.get('delegation') or {})
        delegation.update(fields)
        delegation['updated_at'] = int(time.time())
        meta['delegation'] = delegation

    await update_chat_meta(child_chat_id, update)


async def resolve_child_chat_id(parent_chat_id: str, delegation_id: str) -> str | None:
    chat = await Chats.get_chat_by_id(parent_chat_id)
    if not chat:
        return None
    delegations = (chat.meta or {}).get('delegations') or {}
    child_chat_id = delegations.get(delegation_id)
    return str(child_chat_id) if child_chat_id else None


async def mark_orphaned(child_chat_id: str, orphaned_by: str) -> None:
    await update_delegation(
        child_chat_id,
        status='orphaned',
        orphaned_by=orphaned_by,
        closed_at=int(time.time()),
    )
