"""Tests for the built-in chat-file tool selection."""

import pytest

from open_webui.utils.file_tools import FileToolScope, file_tool_scope

NO_FILE_TOOLS = FileToolScope(list_chat_files=False, read_content=False)


def _scope(**overrides) -> FileToolScope:
    values = {
        'files_enabled': True,
        'file_upload': True,
        'chat_has_files': True,
        'user_can_upload': True,
        'file_context': False,
    }
    values.update(overrides)
    return file_tool_scope(**values)


def test_model_without_file_context_gets_every_file_tool():
    assert _scope() == FileToolScope(list_chat_files=True, read_content=True)


def test_model_with_file_context_still_gets_list_chat_files():
    assert _scope(file_context=True) == FileToolScope(list_chat_files=True, read_content=False)


@pytest.mark.parametrize('gate', ['files_enabled', 'file_upload', 'chat_has_files', 'user_can_upload'])
@pytest.mark.parametrize('file_context', [True, False])
def test_any_failed_gate_hides_every_file_tool(gate, file_context):
    assert _scope(**{gate: False}, file_context=file_context) == NO_FILE_TOOLS
