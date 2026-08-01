"""Database content translation -- a thin wrapper around deep-translator's
free GoogleTranslator, used to auto-populate per-language fields when an
admin/instructor saves a record in the source language (English).

No API key, no billing: this deliberately replaced an earlier Anthropic-
based implementation so the site never needs a paid key just to show
translated Track/Course/legal content. The one network call
(_translate_text) is isolated here so tests can mock it, same pattern as
bunny.create_video / ai_coach.send_message.

GoogleTranslator only understands plain text -- it has no idea what a
Markdown table or bullet list is, and feeding it a whole blob (pipes,
dashes and all) risks the exact kind of corruption this project already
hit once with the AI-based translator (a stray character landing on a
table's header line silently degrades python-markdown's table detection).
translate_fields() sidesteps that by translating line-by-line, and
cell-by-cell within a table row, so the surrounding Markdown/table syntax
is never itself sent through translation -- only the human-readable text
inside it is.
"""
import logging
import re
import time

from deep_translator import GoogleTranslator
from django.conf import settings

logger = logging.getLogger(__name__)

# TEMPORARY: diagnosing production translations silently falling back to
# English on Render (suspected IP block/rate-limit/timeout on the real
# GoogleTranslator network call). Every line tagged [TRANSLATION_DEBUG] so
# it's easy to grep out of Render's logs. Remove this whole block (and the
# logging calls in _translate_text below) once the root cause is confirmed
# and fixed -- it's noisy by design, not something to ship long-term.
_DEBUG_TAG = '[TRANSLATION_DEBUG]'


def _truncate(text: str, limit: int = 200) -> str:
    text = text or ''
    return text if len(text) <= limit else f'{text[:limit]}... (truncated, {len(text)} chars total)'


# A table's header-separator row, e.g. "|---|---|---|" or "| :-- | --- |".
# Never translated -- there's no human-readable text in it.
_TABLE_SEPARATOR_RE = re.compile(r'^\s*\|?[\s:-]+(\|[\s:-]+)+\|?\s*$')


class TranslationError(Exception):
    pass


def is_configured() -> bool:
    """No API key or billing needed -- translation is on by default
    (settings.AUTO_TRANSLATE_ENABLED). Tests that don't care about
    translation flip this off so they don't make real network calls."""
    return settings.AUTO_TRANSLATE_ENABLED


def _translate_text(text: str, target_language: str) -> str:
    stripped = text.strip()
    if not stripped:
        return text
    leading_ws = text[:len(text) - len(text.lstrip())]
    trailing_ws = text[len(text.rstrip()):]

    logger.info(
        '%s calling GoogleTranslator: source=en target=%s text=%r',
        _DEBUG_TAG, target_language, _truncate(stripped))
    started = time.monotonic()
    try:
        translated = GoogleTranslator(source='en', target=target_language).translate(stripped)
    except Exception as exc:
        elapsed = time.monotonic() - started
        logger.exception(
            '%s GoogleTranslator FAILED after %.2fs: source=en target=%s text=%r '
            'exception_type=%s exception_message=%s',
            _DEBUG_TAG, elapsed, target_language, _truncate(stripped),
            type(exc).__name__, str(exc))
        # deep-translator only wraps its *own* known failure modes (rate
        # limiting, bad language code, ...) in deep_translator.exceptions;
        # a raw connection/proxy/timeout error from the underlying HTTP
        # library propagates as-is. Catch broadly here for the same reason
        # AutoTranslatedFieldsMixin catches TranslationError around this
        # call: translation is best-effort and must never crash a save.
        raise TranslationError(str(exc)) from exc

    elapsed = time.monotonic() - started
    if translated is None:
        logger.warning(
            '%s GoogleTranslator returned None after %.2fs: source=en target=%s text=%r',
            _DEBUG_TAG, elapsed, target_language, _truncate(stripped))
        raise TranslationError(f'GoogleTranslator returned nothing for target language {target_language!r}.')

    logger.info(
        '%s GoogleTranslator SUCCEEDED after %.2fs: source=en target=%s output=%r',
        _DEBUG_TAG, elapsed, target_language, _truncate(translated))
    return f'{leading_ws}{translated}{trailing_ws}'


def _is_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith('|') and stripped.endswith('|') and stripped.count('|') >= 2


def _translate_table_row(line: str, target_language: str) -> str:
    """Translate only the text inside each cell, never the pipe characters
    themselves -- keeps the exact same number of columns after translation."""
    cells = line.split('|')
    return '|'.join(
        cell if not cell.strip() else _translate_text(cell, target_language)
        for cell in cells
    )


# A bullet list marker python-markdown recognizes: "- ", "* ", or "+ " at
# the start of a line (after any leading indentation).
_BULLET_RE = re.compile(r'^(\s*[-*+]\s+)(.*)$')


def _translate_line(line: str, target_language: str) -> str:
    """Translate one line, keeping a leading Markdown bullet marker
    (e.g. "- ") itself untranslated -- only the text after it goes through
    GoogleTranslator, so the line stays a recognizable list item."""
    bullet_match = _BULLET_RE.match(line)
    if bullet_match:
        marker, rest = bullet_match.groups()
        if not rest.strip():
            return line
        return f'{marker}{_translate_text(rest, target_language)}'
    return _translate_text(line, target_language)


def translate_markdown(text: str, target_language: str) -> str:
    """Translate a field's source text (plain text or Markdown) line by
    line, preserving every line break, table separator row, and table
    column exactly. A field with no Markdown at all (e.g. a Track name)
    just has one line, so this is equivalent to translating the whole
    field in one call."""
    lines = text.split('\n')
    translated_lines = []
    for line in lines:
        if not line.strip():
            translated_lines.append(line)
        elif _TABLE_SEPARATOR_RE.match(line):
            translated_lines.append(line)
        elif _is_table_row(line):
            translated_lines.append(_translate_table_row(line, target_language))
        else:
            translated_lines.append(_translate_line(line, target_language))
    return '\n'.join(translated_lines)


def translate_fields(fields: dict[str, str], target_languages: list[str]) -> dict[str, dict[str, str]]:
    """fields: {'name': 'Web Development', 'description': '...'} in English.
    target_languages: ISO 639-1 codes to translate into, e.g. ['ar', 'fr', 'es'].
    Returns {'name': {'ar': '...', 'fr': '...', 'es': '...'}, 'description': {...}}.

    Unlike the old AI-based version this doesn't make one combined network
    call -- each line/cell is its own GoogleTranslator request. A failure
    anywhere raises TranslationError for the whole batch (matching the old
    contract) so a caller never ends up caching a half-translated field;
    AutoTranslatedFieldsMixin will simply retry on the next save."""
    if not fields or not target_languages:
        return {}

    result = {}
    for field, text in fields.items():
        result[field] = {}
        for lang in target_languages:
            result[field][lang] = translate_markdown(text, lang)
    return result
