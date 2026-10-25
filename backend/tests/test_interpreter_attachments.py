"""Unit tests for staging conversation attachments into the sandbox inbox."""

import asyncio
import base64
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_webui.utils import interpreter_attachments as attachments


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, 'SESSIONS_DIR', tmp_path / 'sessions')
    return tmp_path


def file_item(path, name):
    return {'file': {'path': str(path), 'name': name}}


def chat_file_item(file_id, name='bildo.png', url=None):
    """The entry a chat message stores for an upload: id and url, never a path."""
    return {
        'type': 'file',
        'file': {
            'id': file_id,
            'user_id': 'user-1',
            'hash': None,
            'filename': name,
            'data': {},
            'meta': {'name': name, 'content_type': 'image/png', 'size': 11},
            'created_at': 0,
            'updated_at': 0,
        },
        'id': file_id,
        'url': file_id if url is None else url,
        'name': name,
        'status': 'uploaded',
        'size': 11,
        'content_type': 'image/png',
    }


def file_lookup(records):
    """A stand-in for Files.get_file_by_id_and_user_id."""

    async def lookup(file_id):
        return records.get(file_id)

    return lookup


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


def test_a_chat_upload_is_resolved_from_its_file_record(sandbox):
    stored = sandbox / 'uploads' / '471da28c_bildo.png'
    stored.parent.mkdir()
    stored.write_bytes(b'image-bytes')
    lookup = file_lookup({'471da28c': SimpleNamespace(path=str(stored), filename='bildo.png')})

    resolved = asyncio.run(attachments.resolve_attachment_paths([chat_file_item('471da28c')], lookup))
    manifest, warnings = attachments.stage_attachments('chat-1', resolved)

    assert warnings == []
    assert manifest == [{'name': 'bildo.png', 'path': str(sandbox / 'sessions' / 'chat-1' / 'inbox' / 'bildo.png')}]
    assert (sandbox / 'sessions' / 'chat-1' / 'inbox' / 'bildo.png').read_bytes() == b'image-bytes'


def test_the_file_id_is_read_from_every_entry_shape():
    assert attachments._file_id({'id': 'abc-1', 'url': 'abc-1'}) == 'abc-1'
    assert attachments._file_id({'file': {'id': 'abc-2'}}) == 'abc-2'
    assert attachments._file_id({'url': '/api/v1/files/abc-3/content'}) == 'abc-3'
    assert attachments._file_id({'url': 'https://example.org/a.png'}) is None
    assert attachments._file_id({'url': 'data:image/png;base64,AAAA'}) is None


def test_an_unresolved_upload_id_is_reported(sandbox):
    resolved = asyncio.run(attachments.resolve_attachment_paths([chat_file_item('gone')], file_lookup({})))
    manifest, warnings = attachments.stage_attachments('chat-1', resolved)

    assert manifest == []
    assert len(warnings) == 1
    assert 'bildo.png: no local path' in warnings[0]


def test_a_failing_lookup_does_not_abort_staging(sandbox):
    async def lookup(file_id):
        raise RuntimeError('database unavailable')

    resolved = asyncio.run(attachments.resolve_attachment_paths([chat_file_item('abc-1')], lookup))
    manifest, warnings = attachments.stage_attachments('chat-1', resolved)

    assert manifest == []
    assert 'no local path' in warnings[0]


def test_an_entry_that_already_has_a_path_is_not_resolved(sandbox):
    async def lookup(file_id):
        raise AssertionError('a staged path must not be looked up again')

    source = sandbox / 'source.png'
    source.write_bytes(b'image-bytes')

    resolved = asyncio.run(attachments.resolve_attachment_paths([file_item(source, 'chart.png')], lookup))

    assert resolved == [file_item(source, 'chart.png')]


def test_an_inline_data_url_is_staged(sandbox):
    payload = base64.b64encode(b'png-bytes').decode()
    item = {'type': 'image', 'url': f'data:image/png;base64,{payload}', 'name': 'shot'}

    manifest, warnings = attachments.stage_attachments('chat-1', [item])

    assert warnings == []
    assert manifest[0]['name'] == 'shot.png'
    assert Path(manifest[0]['path']).read_bytes() == b'png-bytes'
