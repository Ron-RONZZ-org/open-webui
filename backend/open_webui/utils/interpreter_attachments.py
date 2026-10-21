"""Stage a conversation's attachments for the server-side code interpreter.

The sandbox is a separate container with its own mounted workspace, so it cannot
read the OWUI upload store. The backend copies only the files on the current user
message into the conversation's inbox and passes a manifest to the kernel, which
keeps generated code away from every other chat's uploads.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

SANDBOX_ROOT = Path(os.environ.get('OPEN_WEBUI_SANDBOX_DIR', '/mnt/sandbox'))
SESSIONS_DIR = SANDBOX_ROOT / 'sessions'

_UNSAFE_NAME_RE = re.compile(r'[^A-Za-z0-9._-]')


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


def _source_path(item: dict) -> tuple[str | None, str]:
    """Return (local path, display name) for an OWUI file item."""
    file_item = item.get('file') if isinstance(item.get('file'), dict) else item
    source = file_item.get('path') or item.get('path')
    name = file_item.get('name') or file_item.get('filename') or (os.path.basename(source) if source else '')
    if not isinstance(source, str) or source == '':
        return None, name or 'attachment'
    return source, name or os.path.basename(source)


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
    idempotent: a file already present with the same size is reused.
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
        source, name = _source_path(item)
        if source is None:
            warnings.append(f'{name}: no local path; re-attach the file to use it.')
            continue
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
            shutil.copyfile(source, target)
        except OSError as err:
            logger.warning('could not stage attachment %s: %s', source, err)
            warnings.append(f'{name}: could not be staged ({err}).')
            continue

        total_bytes += size
        manifest.append({'name': target.name, 'path': str(target)})

    return manifest, warnings
