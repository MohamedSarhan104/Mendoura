"""Database content translation -- a thin wrapper around the Anthropic
Messages API used to auto-populate per-language fields when an admin/
instructor saves a record in the source language (English).

The one network call (translate_fields) is isolated here so tests can mock
it, same pattern as bunny.create_video / ai_coach.send_message.
"""
import json

import anthropic
from django.conf import settings

MODEL = 'claude-opus-4-8'

SYSTEM_PROMPT = (
    "You are a professional localization engine for an online learning "
    "platform. Translate the given field values from English into each "
    "requested language. Preserve tone and meaning; keep translations "
    "concise, natural, and appropriate for a course catalog. Respond with "
    "ONLY a single JSON object, no markdown fences and no commentary."
)


class TranslationError(Exception):
    pass


def is_configured() -> bool:
    return bool(settings.AI_API_KEY)


def translate_fields(fields: dict[str, str], target_languages: list[str]) -> dict[str, dict[str, str]]:
    """fields: {'name': 'Web Development', 'description': '...'} in English.
    target_languages: ISO 639-1 codes to translate into, e.g. ['ar', 'fr', 'es'].
    Returns {'name': {'ar': '...', 'fr': '...', 'es': '...'}, 'description': {...}}
    -- any field/language the model fails to return is simply absent from
    the result, left for the caller to treat as not-yet-translated."""
    if not is_configured():
        raise TranslationError('Database translation is not configured yet.')
    if not fields or not target_languages:
        return {}

    prompt = (
        f"Language codes to translate into: {', '.join(target_languages)}.\n\n"
        f"Fields (English source):\n{json.dumps(fields, ensure_ascii=False)}\n\n"
        "Return a JSON object shaped exactly like this, with every field name "
        "and language code as keys:\n"
        f'{{"<field>": {{"<language code>": "<translation>", ...}}, ...}}'
    )

    client = anthropic.Anthropic(api_key=settings.AI_API_KEY)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{'role': 'user', 'content': prompt}],
        )
    except anthropic.APIError as exc:
        raise TranslationError(str(exc)) from exc

    text = ''.join(block.text for block in response.content if block.type == 'text').strip()
    # Models occasionally wrap JSON in a markdown fence despite instructions.
    if text.startswith('```'):
        text = text.strip('`')
        text = text[4:] if text.lower().startswith('json') else text

    try:
        parsed = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise TranslationError(f'Could not parse translation response: {exc}') from exc

    if not isinstance(parsed, dict):
        raise TranslationError('Translation response was not a JSON object.')
    return parsed
