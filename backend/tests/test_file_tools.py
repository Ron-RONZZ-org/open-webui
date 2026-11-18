"""Tests for the built-in chat-file tool selection."""

import pytest

from open_webui.utils.file_tools import file_tool_scope


def _scope(**overrides) -> bool:
    values = {
        'files_enabled': True,
        'file_upload': True,
        'chat_has_files': True,
        'user_can_upload': True,
    }
    values.update(overrides)
    return file_tool_scope(**values)


def test_request_with_chat_files_gets_every_file_tool():
    assert _scope() is True


@pytest.mark.parametrize('gate', ['files_enabled', 'file_upload', 'chat_has_files', 'user_can_upload'])
def test_any_failed_gate_hides_every_file_tool(gate):
    assert _scope(**{gate: False}) is False
