"""Whether the built-in chat-file tools are offered for a request.

All four tools are exposed together once the request may carry chat files:

* ``list_chat_files`` — upload metadata, including the server-side store path,
  which a model hands to an external tool server (e.g. the wiki writer's
  ``accessFile``), not file content.
* ``query_chat_files`` / ``grep_chat_files`` / ``view_file`` — the file content,
  through semantic search, exact text search and paginated reading.

``file_context`` is not a reason to withhold the readers. It only controls the
automatic context injection, and with ``rag.full_context`` off that injection is
top-k retrieval — an excerpt, not the document. A model told "here is the file"
would then have no way to read the rest of it, so the readers are offered
whenever the request may have chat files. The ``<attached_files>`` block that
``add_file_context`` adds carries each file's id, which is what the readers take.
"""


def file_tool_scope(
    *,
    files_enabled: bool,
    file_upload: bool,
    chat_has_files: bool,
    user_can_upload: bool,
) -> bool:
    """Return whether the request gets the built-in chat-file tools.

    The tools appear only when every gate holds: the ``files`` builtin category
    is enabled for the model, the model declares ``file_upload``, the chat
    actually carries an upload, and the user may upload files.
    """
    return bool(files_enabled and file_upload and chat_has_files and user_can_upload)
