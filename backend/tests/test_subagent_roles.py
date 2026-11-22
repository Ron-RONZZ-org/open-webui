import pytest

from open_webui.utils.subagent_roles import (
    AGENT_TAG,
    DEFAULT_CONTACT_ALLOWANCE,
    agent_tool_ids,
    is_agent_model,
    needs_quota_approval,
    parse_decision,
    quota_question,
    summon_question,
    tag_names,
)


def test_tag_names_accepts_strings_and_dicts():
    assert tag_names(['Veramiko-Agent', {'name': 'Other'}]) == [AGENT_TAG, 'other']


def test_agent_model_requires_custom_model_and_tag():
    assert is_agent_model({'tags': [{'name': AGENT_TAG}]}, 'deepseek-flash')
    assert not is_agent_model({'tags': [{'name': AGENT_TAG}]}, None)
    assert not is_agent_model({'tags': [{'name': 'other'}]}, 'deepseek-flash')
    assert not is_agent_model(None, 'deepseek-flash')


def test_agent_tool_ids_reads_meta():
    assert agent_tool_ids({'toolIds': ['server:mcp:a', None, 'server:mcp:b']}) == [
        'server:mcp:a',
        'server:mcp:b',
    ]
    assert agent_tool_ids(None) == []


def test_quota_allows_first_allowance_contacts():
    assert DEFAULT_CONTACT_ALLOWANCE == 5
    assert not needs_quota_approval(4, 5)
    assert needs_quota_approval(5, 5)  # the 6th contact asks


@pytest.mark.parametrize(
    'answer, expected',
    [
        ({'value': 'approve'}, ('approve', 5, '')),
        ({'value': 'approve:5'}, ('approve', 5, '')),
        ({'value': 'approve:custom', 'text': '12'}, ('approve', 12, '')),
        ({'value': 'approve:custom', 'text': '0'}, ('invalid', 0, '0')),
        ({'value': 'approve:custom', 'text': 'x'}, ('invalid', 0, 'x')),
        ({'value': 'reject', 'text': 'Use the 2019 series.'}, ('reject', 0, 'Use the 2019 series.')),
        ({}, ('reject', 0, '')),
    ],
)
def test_parse_decision(answer, expected):
    assert parse_decision({'quota': answer}, 'quota') == expected


def test_quota_question_has_exact_options_and_placeholder():
    question = quota_question('Veramiko Statistician', 5)
    assert [option['value'] for option in question['options']] == [
        'approve:5',
        'approve:custom',
        'reject',
    ]
    placeholders = {block['placeholder'] for block in question['free_text']}
    assert 'Tell me what to do differently:' in placeholders
    assert 'Number of additional contacts' in placeholders


def test_summon_question_has_reject_correction():
    question = summon_question('Veramiko Statistician', 'Extract table 1')
    assert [option['value'] for option in question['options']] == ['approve', 'reject']
    assert question['free_text'][0]['placeholder'] == 'Tell me what to do differently:'
