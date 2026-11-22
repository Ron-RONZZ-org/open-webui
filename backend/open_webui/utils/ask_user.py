from collections.abc import Callable


ASK_USER_NAME = 'ask_user'


def get_ask_user_tool_calls(tool_calls: list[dict]) -> tuple[list[dict], str | None]:
    ask_user_calls = [
        tool_call for tool_call in tool_calls if tool_call.get('function', {}).get('name') == ASK_USER_NAME
    ]
    if not ask_user_calls:
        return [], None
    if len(tool_calls) != 1:
        return (
            ask_user_calls,
            'Error: ask_user must be the only tool call, so it did not run. Call ask_user on its own.',
        )
    if len(ask_user_calls) != 1:
        return ask_user_calls, 'Error: only one ask_user call is allowed per turn.'
    return ask_user_calls, None


def _normalize_free_text(blocks: list, option_values: set[str]) -> list[dict]:
    normalized = []
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError('Each free_text block must be an object.')
        when = block.get('when')
        if not isinstance(when, list) or not 1 <= len(when) <= 3:
            raise ValueError('free_text.when requires 1-3 option values.')
        when = [str(value).strip() for value in when]
        if any(value not in option_values for value in when):
            raise ValueError('free_text.when must reference an option value.')

        input_type = str(block.get('input_type') or 'text').strip().lower()
        if input_type not in {'text', 'number'}:
            raise ValueError('free_text.input_type must be text or number.')

        item = {
            'when': when,
            'input_type': input_type,
            'placeholder': str(block.get('placeholder') or '').strip()[:240],
            'multiline': bool(block.get('multiline', False)) and input_type == 'text',
            'required': bool(block.get('required', True)),
        }
        if input_type == 'number':
            for key in ('min', 'max'):
                value = block.get(key)
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f'free_text.{key} must be a number.')
                item[key] = int(value)
        normalized.append(item)
    return normalized


def normalize_ask_user_request(arguments: dict) -> dict:
    questions = arguments.get('questions')
    if not isinstance(questions, list) or not 1 <= len(questions) <= 3:
        raise ValueError('ask_user requires 1-3 questions.')

    normalized_questions = []
    seen_ids = set()
    allow_other = bool(arguments.get('allow_other', True))
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            raise ValueError('Each question must be an object.')

        question_id = str(question.get('id') or '').strip()[:64]
        if not question_id:
            raise ValueError('Each question requires a non-empty id.')
        if question_id in seen_ids:
            raise ValueError(f'Duplicate question id: {question_id}')
        seen_ids.add(question_id)

        options = question.get('options')
        if not isinstance(options, list) or not 2 <= len(options) <= 3:
            raise ValueError('Each question requires 2-3 options.')

        normalized_options = []
        option_values = set()
        for option in options:
            if not isinstance(option, dict):
                raise ValueError('Each option must be an object.')
            label = str(option.get('label') or '').strip()[:80]
            description = str(option.get('description') or '').strip()[:240]
            if not label or not description:
                raise ValueError('Each option requires a label and description.')
            value = str(option.get('value') or label).strip()[:80]
            if value in option_values:
                raise ValueError(f'Duplicate option value: {value}')
            option_values.add(value)
            normalized_options.append({'label': label, 'description': description, 'value': value})

        question_text = str(question.get('question') or '').strip()[:500]
        if not question_text:
            raise ValueError('Each question requires question text.')

        free_text = question.get('free_text') or []
        if not isinstance(free_text, list):
            raise ValueError('free_text must be a list.')
        reject_text = question.get('reject_text')
        if reject_text is not None:
            if not isinstance(reject_text, dict):
                raise ValueError('reject_text must be an object.')
            free_text = [
                *free_text,
                {**reject_text, 'when': ['reject']},
            ]
        if len(free_text) > 3:
            raise ValueError('A question accepts at most 3 free_text blocks.')

        normalized_questions.append(
            {
                'id': question_id,
                'header': str(question.get('header') or '').strip()[:48] or f'Question {index + 1}',
                'question': question_text,
                'options': normalized_options,
                'free_text': _normalize_free_text(free_text, option_values),
                'allow_other': bool(question.get('allow_other', allow_other)),
            }
        )

    timeout_ms = arguments.get('timeout_ms', 120_000)
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 60_000 <= timeout_ms <= 240_000:
        timeout_ms = 120_000

    return {
        'questions': normalized_questions,
        'allow_other': allow_other,
        'timeout_ms': timeout_ms,
    }


async def request_user_input(
    event_call,
    questions: list[dict],
    *,
    allow_other: bool = True,
    timeout_ms: int = 120_000,
) -> dict:
    """Ask the user through the browser and return a normalised result.

    Shared by ``ask_user`` and the sub-agent approval gates. The caller supplies
    the ``__event_call__`` of the active request; it is ``None`` for headless
    runs, where the gate cannot be resolved.
    """
    try:
        payload = normalize_ask_user_request(
            {'questions': questions, 'allow_other': allow_other, 'timeout_ms': timeout_ms}
        )
    except (TypeError, ValueError) as exc:
        return {'status': 'error', 'error': str(exc)}

    if event_call is None:
        return {'status': 'error', 'error': 'User input requires an active browser session.'}

    output = await event_call({'type': 'request:user_input', 'data': payload})
    if not isinstance(output, dict):
        return {'status': 'error', 'error': 'Invalid user input response.'}
    if output.get('error'):
        return {'status': 'error', 'error': output.get('error')}
    if output.get('status') == 'cancelled':
        return {'status': 'cancelled', 'answers': {}}
    return {'status': 'answered', 'answers': output.get('answers', {})}


def stage_ask_user_tool_calls(
    tool_calls: list[dict],
    output: list[dict],
    make_output_id: Callable[[str], str],
) -> tuple[bool, str | None]:
    from open_webui.utils.json_codec import JSONCodec

    ask_user_calls, error = get_ask_user_tool_calls(tool_calls)
    if not ask_user_calls:
        return False, None

    for tool_call in ask_user_calls:
        call_id = tool_call.get('id') or make_output_id('fc')
        raw_arguments = tool_call.get('function', {}).get('arguments', '{}')
        arguments = raw_arguments

        if not error:
            try:
                parsed_arguments = JSONCodec.loads(raw_arguments or '{}')
                if not isinstance(parsed_arguments, dict):
                    raise ValueError('ask_user arguments must be an object.')
                arguments = JSONCodec.dumps(normalize_ask_user_request(parsed_arguments))
            except (JSONCodec.JSONDecodeError, TypeError, ValueError) as exc:
                error = f'Error: {exc}'

        item = {
            'type': 'function_call',
            'id': call_id or make_output_id('fc'),
            'call_id': call_id,
            'name': ASK_USER_NAME,
            'arguments': arguments,
            'status': 'completed' if error else 'pending',
        }

        existing_item = next(
            (
                existing
                for existing in output
                if existing.get('type') == 'function_call'
                and (
                    existing.get('call_id') == call_id
                    or existing.get('id') == tool_call.get('id')
                    or (
                        not existing.get('call_id')
                        and existing.get('name') == ASK_USER_NAME
                        and existing.get('status') not in {'rejected', 'failed'}
                    )
                )
            ),
            None,
        )
        if existing_item:
            existing_item.update(item)
        else:
            output.append(item)

        # Every invalid call needs its own result, or the UI waits on it forever.
        if error:
            output.append(
                {
                    'type': 'function_call_output',
                    'id': make_output_id('fco'),
                    'call_id': call_id,
                    'output': [{'type': 'input_text', 'text': error}],
                    'status': 'completed',
                }
            )

    return True, error
