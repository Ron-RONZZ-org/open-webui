"""Stage a conversation's attachments for the server-side code interpreter.

The sandbox is a separate container with its own mounted workspace, so it cannot
read the OWUI upload store. The backend copies only the files on the current user
message into the conversation's inbox and passes a manifest to the kernel, which
keeps generated code away from every other chat's uploads.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import shutil
from pathlib import Path
from urllib.parse import unquote_to_bytes

logger = logging.getLogger(__name__)

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

SANDBOX_ROOT = Path(os.environ.get('OPEN_WEBUI_SANDBOX_DIR', '/mnt/sandbox'))
SESSIONS_DIR = SANDBOX_ROOT / 'sessions'

_UNSAFE_NAME_RE = re.compile(r'[^A-Za-z0-9._-]')
_FILE_CONTENT_URL_RE = re.compile(r'/api/v1/files/([^/]+)/content/?$')
_DATA_URL_RE = re.compile(r'^data:(?P<mime>[^;,]*)(?P<params>(?:;[^,]*)?),(?P<payload>.*)$', re.DOTALL)

_EXTENSION_BY_MIME = {
    'image/png': '.png',
    'image/jpeg': '.jpg',
    'image/webp': '.webp',
    'image/gif': '.gif',
}


def _safe_component(value: str) -> str:
    """A single path component, or '' when nothing safe remains."""
    name = _UNSAFE_NAME_RE.sub('_', os.path.basename(value or '')).strip('.')
    return name


def session_inbox(chat_id: str | None) -> Path | None:
    """The inbox for one conversation, or None when the chat id is unusable."""
    safe = _safe_component(chat_id or '')
    if not safe:
        return None
    return SESSIONS_DIR / safe / 'inbox'


def _file_item(item: dict) -> dict:
    """The nested ``file`` record of a chat file entry, when present."""
    file_item = item.get('file')
    return file_item if isinstance(file_item, dict) else {}


def _local_path(item: dict) -> str | None:
    """The server-side path of a file entry, when the entry carries one."""
    source = _file_item(item).get('path') or item.get('path')
    return source if isinstance(source, str) and source else None


def _display_name(item: dict, source: str | None = None) -> str:
    """The name a staged copy should carry."""
    file_item = _file_item(item)
    name = file_item.get('name') or file_item.get('filename') or item.get('name')
    if not name and source:
        name = os.path.basename(source)
    return name or 'attachment'


def _file_id(item: dict) -> str | None:
    """The upload id of a chat file entry.

    A chat upload carries the id as ``id`` and repeats it as the bare ``url``;
    other entries use the content route. A remote URL or an inline ``data:`` URL
    is not an upload this server can look up.
    """
    file_item = _file_item(item)
    for candidate in (item.get('id'), file_item.get('id'), item.get('url'), file_item.get('url')):
        if not isinstance(candidate, str):
            continue
        value = candidate.strip()
        if not value:
            continue
        match = _FILE_CONTENT_URL_RE.search(value)
        if match:
            return match.group(1)
        if value.startswith(('data:', 'http://', 'https://', '/')):
            continue
        return value
    return None


def _is_data_url(item: dict) -> bool:
    url = item.get('url') or _file_item(item).get('url')
    return isinstance(url, str) and url.startswith('data:')


def _inline_bytes(item: dict) -> tuple[bytes, str] | None:
    """Decode an inline ``data:`` URL attachment into (bytes, name)."""
    url = item.get('url') or _file_item(item).get('url')
    if not isinstance(url, str) or not url.startswith('data:'):
        return None
    match = _DATA_URL_RE.match(url)
    if match is None:
        return None
    try:
        if ';base64' in match.group('params'):
            data = base64.b64decode(match.group('payload'), validate=False)
        else:
            data = unquote_to_bytes(match.group('payload'))
    except (TypeError, ValueError):
        return None
    name = _display_name(item)
    if not Path(name).suffix:
        name = f'{name}{_EXTENSION_BY_MIME.get(match.group("mime").lower(), "")}'
    return data, name


async def resolve_attachment_paths(items: list[dict], lookup) -> list[dict]:
    """Fill in the local path of chat attachments from their file records.

    A chat message's file entry carries the upload id but not the server path:
    the path lives on the file record that ``lookup`` returns for an id. Entries
    that already carry a path, an inline ``data:`` URL, or a remote URL are
    returned unchanged; staging reports the ones it cannot read.
    """
    resolved: list[dict] = []
    for item in items:
        if not isinstance(item, dict) or _local_path(item) or _is_data_url(item):
            resolved.append(item)
            continue
        file_id = _file_id(item)
        record = None
        if file_id:
            try:
                record = await lookup(file_id)
            except Exception as err:
                # Non-fatal: the file stays unresolved and staging reports it, so
                # one bad record never aborts a code execution.
                logger.warning('could not resolve attachment %s: %s', file_id, err)
        path = getattr(record, 'path', None)
        if not path:
            resolved.append(item)
            continue
        item = {**item, 'path': path}
        file_item = _file_item(item)
        if not (file_item.get('name') or file_item.get('filename') or item.get('name')):
            filename = getattr(record, 'filename', None)
            if filename:
                item['name'] = filename
        resolved.append(item)
    return resolved


def _free_name(inbox: Path, name: str) -> Path:
    """Pick a name that does not overwrite a different file already staged."""
    candidate = inbox / name
    stem, suffix = candidate.stem, candidate.suffix
    index = 2
    while candidate.exists():
        candidate = inbox / f'{stem}-{index}{suffix}'
        index += 1
    return candidate


def stage_attachments(chat_id: str | None, items: list[dict]) -> tuple[list[dict], list[str]]:
    """Copy the conversation's attachments into its inbox.

    Returns the manifest the kernel sees (``name`` and absolute ``path``) and the
    warnings for anything that could not be staged. Re-staging the same file is
    idempotent: a file already present with the same size is reused. A file the
    client sent inline as a ``data:`` URL is written out of its decoded bytes.
    """
    inbox = session_inbox(chat_id)
    if inbox is None:
        if items:
            return [], ['Attachments were skipped because this chat has no usable id.']
        return [], []

    manifest: list[dict] = []
    warnings: list[str] = []
    total_bytes = 0

    for item in items:
        if not isinstance(item, dict):
            continue

        source = _local_path(item)
        inline = None if source else _inline_bytes(item)
        name = _display_name(item, source)
        if source is None and inline is None:
            warnings.append(f'{name}: no local path; re-attach the file to use it.')
            continue

        if inline is not None:
            payload, name = inline
            size = len(payload)
        else:
            if not os.path.isfile(source):
                warnings.append(f'{name}: the uploaded file is no longer available.')
                continue
            try:
                size = os.path.getsize(source)
            except OSError as err:
                warnings.append(f'{name}: could not read the file ({err}).')
                continue

        if total_bytes + size > MAX_ATTACHMENT_BYTES:
            warnings.append(f'{name}: skipped because the attachments exceed {MAX_ATTACHMENT_BYTES} bytes.')
            continue

        safe_name = _safe_component(name)
        if not safe_name:
            warnings.append('An attachment was skipped because its name was unusable.')
            continue

        target = inbox / safe_name
        try:
            inbox.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.stat().st_size == size:
                    manifest.append({'name': target.name, 'path': str(target)})
                    total_bytes += size
                    continue
                target = _free_name(inbox, safe_name)
            if inline is not None:
                target.write_bytes(payload)
            else:
                shutil.copyfile(source, target)
        except OSError as err:
            logger.warning('could not stage attachment %s: %s', source or name, err)
            warnings.append(f'{name}: could not be staged ({err}).')
            continue

        total_bytes += size
        manifest.append({'name': target.name, 'path': str(target)})

    return manifest, warnings
