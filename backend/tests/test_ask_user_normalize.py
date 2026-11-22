import pytest

from open_webui.utils.ask_user import normalize_ask_user_request, request_user_input


def _question(**overrides):
    base = {
        'id': 'q1',
        'header': 'Header',
        'question': 'Question?',
        'options': [
            {'label': 'Approve 5 more', 'description': 'Continue.', 'value': 'approve:5'},
            {'label': 'Approve custom', 'description': 'Choose.', 'value': 'approve:custom'},
            {'label': 'Reject', 'description': 'Stop.', 'value': 'reject'},
        ],
    }
    base.update(overrides)
    return base


def test_option_value_defaults_to_label_and_is_kept():
    payload = normalize_ask_user_request(
        {
            'questions': [
                {
                    'id': 'q',
                    'question': 'Pick?',
                    'options': [
                        {'label': 'Yes', 'description': 'Do it.'},
                        {'label': 'No', 'description': 'Do not.'},
                    ],
                }
            ]
        }
    )
    options = payload['questions'][0]['options']
    assert [option['value'] for option in options] == ['Yes', 'No']


def test_free_text_blocks_normalise():
    payload = normalize_ask_user_request(
        {
            'questions': [
                _question(
                    free_text=[
                        {
                            'when': ['approve:custom'],
                            'input_type': 'number',
                            'placeholder': 'Number of additional contacts',
                            'min': 1,
                            'max': 100,
                        },
                        {
                            'when': ['reject'],
                            'input_type': 'text',
                            'multiline': True,
                            'placeholder': 'Tell me what to do differently:',
                        },
                    ]
                )
            ]
        }
    )
    blocks = payload['questions'][0]['free_text']
    assert blocks[0]['input_type'] == 'number'
    assert blocks[0]['min'] == 1 and blocks[0]['max'] == 100
    assert blocks[1]['multiline'] is True
    assert blocks[1]['placeholder'] == 'Tell me what to do differently:'


def test_reject_text_shorthand():
    payload = normalize_ask_user_request(
        {
            'questions': [
                _question(
                    reject_text={
                        'placeholder': 'Tell me what to do differently:',
                        'multiline': True,
                    }
                )
            ]
        }
    )
    block = payload['questions'][0]['free_text'][0]
    assert block['when'] == ['reject']
    assert block['placeholder'] == 'Tell me what to do differently:'


def test_free_text_when_must_reference_an_option_value():
    with pytest.raises(ValueError, match='option value'):
        normalize_ask_user_request({'questions': [_question(free_text=[{'when': ['missing'], 'placeholder': 'x'}])]})


def test_duplicate_option_values_are_rejected():
    with pytest.raises(ValueError, match='Duplicate option value'):
        normalize_ask_user_request(
            {
                'questions': [
                    _question(
                        options=[
                            {'label': 'A', 'description': 'a', 'value': 'same'},
                            {'label': 'B', 'description': 'b', 'value': 'same'},
                        ]
                    )
                ]
            }
        )


@pytest.mark.asyncio
async def test_request_user_input_without_event_call():
    result = await request_user_input(None, [_question()])
    assert result['status'] == 'error'
    assert 'browser session' in result['error']


@pytest.mark.asyncio
async def test_request_user_input_returns_answers():
    async def event_call(event):
        assert event['type'] == 'request:user_input'
        return {'status': 'answered', 'answers': {'q1': {'value': 'approve:5', 'text': ''}}}

    result = await request_user_input(event_call, [_question()])
    assert result['status'] == 'answered'
    assert result['answers']['q1']['value'] == 'approve:5'
