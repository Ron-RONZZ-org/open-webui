"""Which built-in chat-file tools a model is offered.

``list_chat_files`` returns upload metadata — the file id, name and the
server-side store path — not the file content. A model that already reads file
content (``file_context``) still needs that path to hand an upload to an
external tool server, so the metadata tool is exposed whenever the chat has
files and the user may upload. The content-reading tools stay limited to models
without file context, where they are the only way to read an upload.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FileToolScope:
    """The chat-file tools to expose for one request."""

    list_chat_files: bool
    read_content: bool


def file_tool_scope(
    *,
    files_enabled: bool,
    file_upload: bool,
    chat_has_files: bool,
    user_can_upload: bool,
    file_context: bool,
) -> FileToolScope:
    """Decide which built-in file tools the request gets.

    ``list_chat_files`` is metadata discovery and is always exposed once the
    request may upload; ``query_chat_files``/``grep_chat_files``/``view_file``
    read content and remain gated on the model not having file context.
    """
    if not (files_enabled and file_upload and chat_has_files and user_can_upload):
        return FileToolScope(list_chat_files=False, read_content=False)
    return FileToolScope(list_chat_files=True, read_content=not file_context)
