"""One-off bulk .po translation via Google's Gemini API -- an alternative
backend for translate_po_entries, used specifically because this project's
default GoogleTranslator path (courses/auto_translate.py, deep-translator's
free wrapper around translate.google.com) is unreachable from some
environments (e.g. this one) that can otherwise reach Gemini's API host
fine, since it's a different domain (generativelanguage.googleapis.com).

Not used for anything else -- DB-content translation (Track/Course/legal
docs) stays on GoogleTranslator via auto_translate.py; this only exists for
the .po static-UI-string backfill. Same "isolate the one network call"
pattern as ai_coach.py/bunny.py/certificates.py, so it's easy to find and
to mock in tests.
"""
import logging

from django.conf import settings
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

logger = logging.getLogger(__name__)

MODEL = getattr(settings, 'GEMINI_MODEL', 'gemini-3.1-flash-lite')

# Keyed by the same codes settings.LANGUAGES/Django use, not GoogleTranslator's
# -- Gemini takes a plain-English instruction, not a language code, so it
# needs the actual language name spelled out instead of a 2-letter code.
LANGUAGE_NAMES = {
    'fr': 'French', 'es': 'Spanish', 'de': 'German', 'it': 'Italian',
    'pt': 'Portuguese', 'tr': 'Turkish', 'ru': 'Russian',
    'zh-hans': 'Simplified Chinese', 'hi': 'Hindi', 'ur': 'Urdu',
}


class TranslationError(Exception):
    pass


class QuotaExceededError(TranslationError):
    """Raised specifically for a 429 -- distinct from every other failure
    so a bulk-run caller can stop the whole run cleanly (saving whatever's
    already been translated) instead of treating it as one more skippable
    per-entry failure."""


def is_configured() -> bool:
    return bool(settings.GEMINI_API_KEY)


def _prompt(text: str, language_name: str) -> str:
    return (
        f"Translate this user-interface string to {language_name}. "
        "Return ONLY the translated text -- no quotes, no explanation, no "
        "markdown formatting, nothing else. Preserve any placeholders exactly "
        "as they appear, byte-for-byte, in their original position and form "
        "(examples of placeholders to preserve untranslated: %(name)s, %s, %d, "
        "{0}, {name}). If the string contains HTML tags, preserve those "
        "exactly too and translate only the human-readable text between them.\n\n"
        f"String to translate:\n{text}"
    )


def translate_text(text: str, target_language: str) -> str:
    """target_language is a Django LANGUAGES code (e.g. 'fr', 'zh-hans').
    Raises QuotaExceededError on a 429, TranslationError on anything else
    that fails -- callers decide how to handle each (see
    translate_po_entries' --backend gemini handling)."""
    if not is_configured():
        raise TranslationError('GEMINI_API_KEY is not set.')

    language_name = LANGUAGE_NAMES.get(target_language, target_language)
    client = genai.Client(api_key=settings.GEMINI_API_KEY)
    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=[genai_types.Content(
                role='user', parts=[genai_types.Part.from_text(text=_prompt(text, language_name))])],
            config=genai_types.GenerateContentConfig(temperature=0.2, max_output_tokens=1024),
        )
    except genai_errors.APIError as exc:
        logger.error(
            '[PO_TRANSLATE] Gemini API call failed: code=%s status=%s message=%s target=%s text=%r',
            exc.code, exc.status, exc.message, target_language, text[:200], exc_info=True)
        if exc.code == 429:
            raise QuotaExceededError(str(exc)) from exc
        raise TranslationError(str(exc)) from exc

    translated = (response.text or '').strip()
    # Gemini occasionally wraps a short answer in a markdown code fence or
    # matching quotes despite the prompt's explicit "no quotes" instruction
    # -- strip one matching pair from each end rather than leave it in the
    # compiled catalog.
    if len(translated) >= 2 and translated[0] == translated[-1] and translated[0] in '"\'':
        translated = translated[1:-1].strip()
    if translated.startswith('```') and translated.endswith('```'):
        translated = translated.strip('`').strip()

    if not translated:
        raise TranslationError(f'Gemini returned an empty translation for target={target_language!r}.')
    return translated
