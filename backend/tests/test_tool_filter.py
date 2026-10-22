"""Tests for the model-scoped tool allowlist (meta.toolFilter)."""

from open_webui.utils.tool_filter import apply_tool_filter, tool_matches_filter


def _mcp(name: str, spec: str | None = None) -> dict:
    return {'type': 'mcp', 'spec': {'name': spec or name}}


def _external(name: str) -> dict:
    return {'type': 'external', 'spec': {'name': name}}


def test_no_filter_keeps_every_tool():
    tools = {'a': _mcp('a'), 'b': _external('b')}
    assert apply_tool_filter(tools, None) == tools
    assert apply_tool_filter(tools, []) == tools
    assert apply_tool_filter(tools, ['', None]) == tools


def test_matches_raw_name_behind_connection_prefix():
    assert tool_matches_filter(['upload-file'], 'mediawiki-mcp_upload-file', 'upload-file')
    assert tool_matches_filter(['uml-mcp_*'], 'uml-mcp_render_diagram', 'render_diagram')
    assert not tool_matches_filter(['upload-file'], 'uml-mcp_generate_uml', 'generate_uml')


def test_keeps_matching_tool_server_tools():
    tools = {
        'mediawiki-mcp_upload-file': _mcp('upload-file'),
        'mediawiki-mcp_delete-page': _mcp('delete-page'),
        'uml-mcp_render_diagram': _mcp('render_diagram'),
    }
    kept = apply_tool_filter(tools, ['upload-file', 'render_diagram'])
    assert set(kept) == {'mediawiki-mcp_upload-file', 'uml-mcp_render_diagram'}


def test_drops_non_matching_external_tools():
    tools = {'local_tool': _external('local_tool'), 'upload-file': _external('upload-file')}
    assert set(apply_tool_filter(tools, ['upload-file'])) == {'upload-file'}


def test_builtins_are_never_filtered():
    tools = {
        'execute_code': {'type': 'builtin', 'spec': {'name': 'execute_code'}},
        'mediawiki-mcp_upload-file': _mcp('upload-file'),
    }
    kept = apply_tool_filter(tools, ['upload-file'])
    assert 'execute_code' in kept
    assert 'mediawiki-mcp_upload-file' in kept


def test_wildcard_patterns():
    tools = {'mediawiki-mcp_upload-file': _mcp('upload-file')}
    assert set(apply_tool_filter(tools, ['mediawiki-*'])) == set(tools)
    assert apply_tool_filter(tools, ['mediawiki-*', 'nomatch']) == tools
    assert apply_tool_filter(tools, ['nomatch']) == {}


def test_filter_does_not_mutate_input():
    tools = {'a': _mcp('a')}
    apply_tool_filter(tools, ['b'])
    assert set(tools) == {'a'}
