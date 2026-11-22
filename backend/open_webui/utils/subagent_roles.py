"""Pure helpers for role-based sub-agent delegation.

This module deliberately imports nothing from the application so the unit suite
(which stubs ``open_webui``) can exercise it with the standard library only.
"""

from __future__ import annotations

AGENT_TAG = 'veramiko-agent'
DEFAULT_CONTACT_ALLOWANCE = 5
MAX_CONTACT_ALLOWANCE = 100


def tag_names(tags) -> list[str]:
    """Normalise the several tag shapes OWUI accepts into lower-case names."""
    names: list[str] = []
    for tag in tags or []:
        name = tag.get('name') if isinstance(tag, dict) else tag
        if name is None:
            continue
        text = str(name).strip().lower()
        if text:
            names.append(text)
    return names


def is_agent_model(meta: dict | None, base_model_id: str | None) -> bool:
    """A delegatable role is a custom model tagged ``veramiko-agent``."""
    if not base_model_id:
        return False
    return AGENT_TAG in tag_names((meta or {}).get('tags'))


def agent_tool_ids(meta: dict | None) -> list[str]:
    return [str(item) for item in ((meta or {}).get('toolIds') or []) if item]


def needs_quota_approval(contact_count: int, contact_allowance: int) -> bool:
    """The first ``allowance`` contacts pass; the next one asks the user."""
    return int(contact_count) >= int(contact_allowance)


def parse_decision(answers: dict | None, question_id: str) -> tuple[str, int, str]:
    """Parse an ``ask_user`` answer into ``(action, amount, text)``.

    ``action`` is ``approve``, ``reject`` or ``invalid``. ``amount`` is the
    number of additional contacts approved (0 for reject/invalid).
    """
    answer = (answers or {}).get(question_id) or {}
    value = str(answer.get('value') or answer.get('label') or '').strip().lower()
    text = str(answer.get('text') or '').strip()

    if value in {'approve', 'approve:5'}:
        return 'approve', 5, ''
    if value in {'approve:custom', 'approve_custom'}:
        try:
            amount = int(text)
        except (TypeError, ValueError):
            return 'invalid', 0, text
        if 1 <= amount <= MAX_CONTACT_ALLOWANCE:
            return 'approve', amount, ''
        return 'invalid', 0, text
    if value == 'reject' or not value:
        return 'reject', 0, text
    return 'invalid', 0, text


def _option(label: str, description: str, value: str) -> dict:
    return {'label': label, 'description': description, 'value': value}


def summon_question(agent_name: str, task: str) -> dict:
    preview = task.strip().replace('\n', ' ')
    if len(preview) > 200:
        preview = f'{preview[:197]}…'
    return {
        'id': 'summon',
        'header': 'Summon subagent',
        'question': f'Summon {agent_name} for this task? {preview}',
        'options': [
            _option('Approve', 'Create a visible session and start the task.', 'approve'),
            _option('Reject', 'Do not create the session.', 'reject'),
        ],
        'free_text': [
            {
                'when': ['reject'],
                'input_type': 'text',
                'multiline': True,
                'placeholder': 'Tell me what to do differently:',
                'required': False,
            }
        ],
    }


def quota_question(agent_name: str, contact_count: int) -> dict:
    return {
        'id': 'quota',
        'header': 'Subagent contact limit',
        'question': f'{agent_name} wants to contact the parent again ({contact_count + 1}th time). Allow?',
        'options': [
            _option('Approve 5 more', 'Continue the current exchange.', 'approve:5'),
            _option('Approve a custom number more', 'Choose how many additional contacts.', 'approve:custom'),
            _option('Reject', 'Send a correction to the subagent instead.', 'reject'),
        ],
        'free_text': [
            {
                'when': ['approve:custom'],
                'input_type': 'number',
                'placeholder': 'Number of additional contacts',
                'min': 1,
                'max': MAX_CONTACT_ALLOWANCE,
                'required': True,
            },
            {
                'when': ['reject'],
                'input_type': 'text',
                'multiline': True,
                'placeholder': 'Tell me what to do differently:',
                'required': False,
            },
        ],
    }
