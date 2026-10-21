"""Unit tests for staging conversation attachments into the sandbox inbox."""

import os

import pytest

from open_webui.utils import interpreter_attachments as attachments


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, 'SESSIONS_DIR', tmp_path / 'sessions')
    return tmp_path


def file_item(path, name):
    return {'file': {'path': str(path), 'name': name}}


def test_stages_attachments_and_returns_a_manifest(sandbox):
    source = sandbox / 'source.png'
    source.write_bytes(b'image-bytes')

    manifest, warnings = attachments.stage_attachments('chat-1', [file_item(source, 'chart.png')])

    assert warnings == []
    assert manifest == [{'name': 'chart.png', 'path': str(sandbox / 'sessions' / 'chat-1' / 'inbox' / 'chart.png')}]
    assert (sandbox / 'sessions' / 'chat-1' / 'inbox' / 'chart.png').read_bytes() == b'image-bytes'


def test_restaging_the_same_file_is_idempotent(sandbox):
    source = sandbox / 'source.png'
    source.write_bytes(b'image-bytes')

    first, _ = attachments.stage_attachments('chat-1', [file_item(source, 'chart.png')])
    second, warnings = attachments.stage_attachments('chat-1', [file_item(source, 'chart.png')])

    assert warnings == []
    assert first == second


def test_a_different_file_with_the_same_name_gets_a_suffix(sandbox):
    first_source = sandbox / 'first.png'
    first_source.write_bytes(b'first')
    second_source = sandbox / 'second.png'
    second_source.write_bytes(b'second')

    attachments.stage_attachments('chat-1', [file_item(first_source, 'chart.png')])
    manifest, warnings = attachments.stage_attachments('chat-1', [file_item(second_source, 'chart.png')])

    assert warnings == []
    assert manifest[0]['name'] == 'chart-2.png'


def test_a_dot_segment_cannot_escape_the_inbox(sandbox):
    source = sandbox / 'secret.txt'
    source.write_bytes(b'secret')

    manifest, warnings = attachments.stage_attachments('chat-1', [file_item(source, '../../secret.txt')])

    assert warnings == []
    staged = manifest[0]['path']
    assert os.path.dirname(staged).endswith(os.path.join('chat-1', 'inbox'))
    assert os.path.basename(staged) == 'secret.txt'


def test_a_missing_source_is_reported(sandbox):
    manifest, warnings = attachments.stage_attachments('chat-1', [file_item(sandbox / 'gone.png', 'gone.png')])

    assert manifest == []
    assert len(warnings) == 1
    assert 'no longer available' in warnings[0]


def test_an_item_without_a_local_path_is_reported(sandbox):
    manifest, warnings = attachments.stage_attachments(
        'chat-1', [{'file': {'name': 'remote.png', 'url': 'https://example.org/remote.png'}}]
    )

    assert manifest == []
    assert len(warnings) == 1
    assert 'no local path' in warnings[0]


def test_the_total_size_cap_is_enforced(sandbox, monkeypatch):
    monkeypatch.setattr(attachments, 'MAX_ATTACHMENT_BYTES', 5)
    first = sandbox / 'first.png'
    first.write_bytes(b'1234')
    second = sandbox / 'second.png'
    second.write_bytes(b'1234')

    manifest, warnings = attachments.stage_attachments(
        'chat-1', [file_item(first, 'a.png'), file_item(second, 'b.png')]
    )

    assert [entry['name'] for entry in manifest] == ['a.png']
    assert len(warnings) == 1
    assert 'exceed' in warnings[0]


def test_an_unusable_chat_id_skips_staging(sandbox):
    source = sandbox / 'source.png'
    source.write_bytes(b'image-bytes')

    manifest, warnings = attachments.stage_attachments(None, [file_item(source, 'chart.png')])

    assert manifest == []
    assert len(warnings) == 1
    assert 'no usable id' in warnings[0]


def test_non_dict_items_are_ignored(sandbox):
    assert attachments.stage_attachments('chat-1', ['not-a-file', 42]) == ([], [])
