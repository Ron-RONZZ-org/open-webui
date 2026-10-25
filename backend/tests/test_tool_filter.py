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


# The shape middleware.py actually builds for an MCP connection: the dict key and
# the spec name are both prefixed with the connection id, and the server's own
# name travels in raw_name. A filter pattern is written against that raw name.
def _real_mcp(server_id: str, raw_name: str) -> tuple[str, dict]:
    tool_name = f'{server_id}_{raw_name}'
    return tool_name, {'type': 'mcp', 'spec': {'name': tool_name}, 'raw_name': raw_name}


MEDIAWIKI_TOOLS = [
    'get-page',
    'get-pages',
    'get-page-history',
    'get-recent-changes',
    'search-page',
    'search-page-by-prefix',
    'parse-wikitext',
    'compare-pages',
    'get-file-info',
    'get-file-content',
    'render-svg',
    'get-file',
    'get-file-data',
    'get-revision',
    'get-site-info',
    'get-category-members',
    'get-links-here',
    'image-search',
    'whoami',
    'create-page',
    'update-page',
    'move-page',
    'delete-page',
    'undelete-page',
    'protect-page',
    'upload-file',
    'upload-file-from-url',
    'update-file',
    'update-file-from-url',
    'wikibase-search-entities',
    'wikibase-get-entity',
    'wikibase-query',
    'wikibase-edit-entity',
    'wikibase-add-statement',
    'wikibase-setsitelink',
    'embeddable-add-special-content',
    'embeddable-add-citation-source',
    'embeddable-add-semantic-entity',
    'embeddable-get-embed-content',
    'embeddable-describe-entity-type',
    'citation-get-citation',
    'add-wiki',
]

UML_TOOLS = ['list_diagram_types', 'generate_uml_batch', 'generate_uml', 'validate_uml', 'render_diagram']

# The Veramiko Illustrator's meta.toolFilter, verbatim.
ILLUSTRATOR_FILTER = [
    'render_diagram',
    'generate_uml',
    'generate_uml_batch',
    'list_diagram_types',
    'validate_uml',
    'render-svg',
    'upload-file',
    'update-file',
    'get-file-info',
    'get-file-content',
    'get-page',
    'search-page-by-prefix',
    'embeddable-describe-entity-type',
    'wikibase-search-entities',
    'image-search',
    'upload-file-from-url',
]


def test_the_illustrator_filter_keeps_its_sixteen_tools_and_every_builtin():
    tools = dict(_real_mcp('mediawiki-mcp', name) for name in MEDIAWIKI_TOOLS)
    tools.update(dict(_real_mcp('uml-mcp', name) for name in UML_TOOLS))
    tools['execute_code'] = {'type': 'builtin', 'spec': {'name': 'execute_code'}}
    tools['search_chats'] = {'type': 'builtin', 'spec': {'name': 'search_chats'}}

    kept = apply_tool_filter(tools, ILLUSTRATOR_FILTER)

    server_tools = {name for name, tool in kept.items() if tool['type'] == 'mcp'}
    assert len(server_tools) == 16
    assert 'mediawiki-mcp_upload-file' in server_tools
    assert 'mediawiki-mcp_render-svg' in server_tools
    assert 'uml-mcp_render_diagram' in server_tools
    assert 'mediawiki-mcp_update-page' not in server_tools
    assert 'mediawiki-mcp_delete-page' not in server_tools
    assert 'mediawiki-mcp_get-pages' not in server_tools
    assert {'execute_code', 'search_chats'} <= set(kept)


def test_a_prefixed_spec_name_without_a_raw_name_is_dropped():
    """The pre-2026-10-25 shape: the raw name was overwritten, so nothing matched."""
    tools = {'mediawiki-mcp_upload-file': {'type': 'mcp', 'spec': {'name': 'mediawiki-mcp_upload-file'}}}
    assert apply_tool_filter(tools, ['upload-file']) == {}


def test_a_wildcard_pattern_still_matches_the_resolved_name():
    tools = dict([_real_mcp('uml-mcp', 'render_diagram')])
    assert set(apply_tool_filter(tools, ['uml-mcp_*'])) == set(tools)
