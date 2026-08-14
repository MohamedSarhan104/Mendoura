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

# settings.LANGUAGES uses Django's own canonical locale codes (matching
# django.conf.locale.LANG_INFO, so LANGUAGE_BIDI/get_available_languages/
# the locale/<code>/ directory convention all agree), which occasionally
# differ from deep-translator's GoogleTranslator target codes -- confirmed
# by direct testing against the installed library. Simplified Chinese is
# the one case in this project's language list: Django's code is
# 'zh-hans', GoogleTranslator only accepts 'zh-CN'. Passing 'zh-hans'
# straight through raises LanguageNotSupportedException on every single
# call. Add an entry here for any future language where the two diverge.
_GOOGLETRANSLATOR_TARGET_OVERRIDES = {
    'zh-hans': 'zh-CN',
}


def _google_translator_target(django_language_code: str) -> str:
    return _GOOGLETRANSLATOR_TARGET_OVERRIDES.get(django_language_code, django_language_code)


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


def _translate_once(stripped: str, api_target: str, target_language: str) -> str:
    """A single raw GoogleTranslator.translate() attempt. Raises
    TranslationError on any failure (network error, bad response, or an
    empty result) -- never lets a raw exception from the underlying HTTP
    library escape."""
    logger.info(
        '%s calling GoogleTranslator: source=en target=%s (api_target=%s) text=%r',
        _DEBUG_TAG, target_language, api_target, _truncate(stripped))
    started = time.monotonic()
    try:
        translated = GoogleTranslator(source='en', target=api_target).translate(stripped)
    except Exception as exc:
        elapsed = time.monotonic() - started
        logger.exception(
            '%s GoogleTranslator FAILED after %.2fs: source=en target=%s (api_target=%s) text=%r '
            'exception_type=%s exception_message=%s',
            _DEBUG_TAG, elapsed, target_language, api_target, _truncate(stripped),
            type(exc).__name__, str(exc))
        # deep-translator only wraps its *own* known failure modes (rate
        # limiting, bad language code, ...) in deep_translator.exceptions;
        # a raw connection/proxy/timeout error from the underlying HTTP
        # library propagates as-is. Catch broadly here for the same reason
        # AutoTranslatedFieldsMixin catches TranslationError around this
        # call: translation is best-effort and must never crash a save.
        raise TranslationError(str(exc)) from exc
    finally:
        # Throttle between consecutive real network calls -- multiplies
        # with every additional configured language and every line/cell of
        # Markdown content, and the free/unofficial GoogleTranslator
        # endpoint is known to rate-limit under burst volume. Zero during
        # tests (see settings.AUTO_TRANSLATE_REQUEST_DELAY_SECONDS) so the
        # suite never pays this cost. In the `finally` so a failed call is
        # also spaced out from whatever's attempted next, not just a
        # successful one.
        delay = getattr(settings, 'AUTO_TRANSLATE_REQUEST_DELAY_SECONDS', 0)
        if delay:
            time.sleep(delay)

    elapsed = time.monotonic() - started
    if translated is None:
        logger.warning(
            '%s GoogleTranslator returned None after %.2fs: source=en target=%s (api_target=%s) text=%r',
            _DEBUG_TAG, elapsed, target_language, api_target, _truncate(stripped))
        raise TranslationError(f'GoogleTranslator returned nothing for target language {target_language!r}.')

    logger.info(
        '%s GoogleTranslator SUCCEEDED after %.2fs: source=en target=%s (api_target=%s) output=%r',
        _DEBUG_TAG, elapsed, target_language, api_target, _truncate(translated))
    return translated


def _translate_text(text: str, target_language: str) -> str:
    """Translates one line/cell, retrying a transient failure a few times
    with a short backoff before giving up. Matters a lot for a long,
    multi-line field (e.g. a Markdown table with a dozen cells) even after
    translate_fields()'s per-(field, language) isolation: without a retry,
    the odds that AT LEAST ONE of a field's many individual line/cell
    calls hits a transient blip (and so discards that field's ENTIRE
    translation into that language) climb fast with how much content the
    field holds -- exactly what made a short heading translate reliably
    while a long table-containing body kept failing outright. Retries are
    zero under tests (see settings.AUTO_TRANSLATE_MAX_RETRIES/
    AUTO_TRANSLATE_RETRY_BACKOFF_SECONDS) so the suite doesn't pay for
    them."""
    stripped = text.strip()
    if not stripped:
        return text
    leading_ws = text[:len(text) - len(text.lstrip())]
    trailing_ws = text[len(text.rstrip()):]
    api_target = _google_translator_target(target_language)
    max_retries = getattr(settings, 'AUTO_TRANSLATE_MAX_RETRIES', 0)
    backoff = getattr(settings, 'AUTO_TRANSLATE_RETRY_BACKOFF_SECONDS', 0)

    for attempt in range(max_retries + 1):
        try:
            translated = _translate_once(stripped, api_target, target_language)
            return f'{leading_ws}{translated}{trailing_ws}'
        except TranslationError:
            if attempt >= max_retries:
                raise
            wait = backoff * (attempt + 1)
            logger.warning(
                '%s retrying target=%s (attempt %s of %s) in %.1fs...',
                _DEBUG_TAG, target_language, attempt + 2, max_retries + 1, wait)
            if wait:
                time.sleep(wait)


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
    Returns {'name': {'ar': '...', 'fr': '...', 'es': '...'}, 'description': {...}}
    -- only the (field, language) pairs that actually succeeded.

    Each line/cell is its own GoogleTranslator request, and each (field,
    language) pair is attempted independently: a failure translating one
    field into one language (rate limiting, a timeout, a bad response, ...)
    is logged and simply left out of the result, rather than raising and
    discarding every other field/language that already succeeded in this
    same call. This used to be all-or-nothing -- one flaky call anywhere
    in the whole batch discarded EVERY language's progress, including ones
    already done. That was silently starving whichever language happened
    to be attempted last (see settings.LANGUAGES' ordering) of ever
    catching up: AutoTranslatedFieldsMixin only retries languages still
    missing from the cached translations on the next save, and a batch
    that always fails part-way through can never leave a missing language
    fully translated even after many retries. A field's OWN single-language
    translation still either fully succeeds or is left out entirely --
    never half-translated, half-English within one language."""
    if not fields or not target_languages:
        return {}

    result = {}
    for field, text in fields.items():
        result[field] = {}
        for lang in target_languages:
            try:
                result[field][lang] = translate_markdown(text, lang)
            except TranslationError:
                logger.warning(
                    "%s translate_fields: field=%r target=%s failed -- leaving it out of this "
                    "save's result so it stays incomplete and gets retried on the next save "
                    "(other languages/fields in this same batch are unaffected).",
                    _DEBUG_TAG, field, lang)
    return result
