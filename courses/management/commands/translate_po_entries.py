import re
import time

import polib
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils.translation import to_locale

from courses import auto_translate, gemini_translate

# Every settings.LANGUAGES entry except en (the source -- it has no .po
# file at all) and ar (human-review-only, per the project's .po policy --
# never machine-overwritten by this command). Kept as an explicit list
# rather than derived live from settings.LANGUAGES so this command never
# silently starts machine-translating a language the policy hasn't
# actually signed off on the moment someone adds a new entry there.
DEFAULT_LANGUAGES = ['fr', 'es', 'de', 'it', 'pt', 'tr', 'ru', 'zh-hans', 'hi', 'ur']

# Anything with a Python format placeholder (%(name)s, %s, %d, ...) needs
# the placeholder itself preserved byte-for-byte, and a plural entry needs
# msgid_plural/msgstr[n] handled instead of a single msgstr -- both are
# skipped rather than risking a mistranslation mangling a placeholder or
# this command guessing at plural forms it has no data for. HTML markup is
# skipped for the same reason translate_markdown() never sends Markdown
# table/bullet syntax through translation: neither backend is asked to
# parse markup, and tags in the msgid (e.g. "<a href=...>...</a>" from an
# old blocktrans) risk coming back reordered or broken.
_PLACEHOLDER_RE = re.compile(r'%\(|%[sd]')
_HTML_RE = re.compile(r'<[a-zA-Z/][^>]*>')

# How often to save the .po file mid-run, in translated entries -- so a
# quota cutoff (or any other interruption) partway through a language
# keeps whatever was already translated instead of losing an entire
# language's progress to one failure at the very end.
_SAVE_EVERY = 20

# Gemini's free tier is commonly reported around 30 RPM for
# gemini-3.1-flash-lite (see the GEMINI_RATE_LIMIT_* comments in
# settings.py) -- 2.5s between calls is ~24 RPM, comfortable headroom
# below that ceiling for a long unattended bulk run. GoogleTranslator
# already throttles itself inside auto_translate._translate_text() (see
# AUTO_TRANSLATE_REQUEST_DELAY_SECONDS), so the google backend needs no
# extra sleep here on top of that.
_BACKEND_DEFAULT_SLEEP = {'google': 0.0, 'gemini': 2.5}


def _is_translatable(entry: polib.POEntry) -> bool:
    if entry.obsolete or entry.msgid_plural:
        return False
    if _PLACEHOLDER_RE.search(entry.msgid):
        return False
    if _HTML_RE.search(entry.msgid):
        return False
    return True


class QuotaStop(Exception):
    """Signals the whole run (not just the current language) should stop
    now -- raised on a Gemini 429, caught in handle() to save progress and
    print a clear "here's what's left" summary instead of dying mid-way
    with a bare traceback."""


class Command(BaseCommand):
    help = (
        "Machine-translate blank msgstr entries in locale/<lang>/LC_MESSAGES/django.po. "
        "--backend google (default) uses the same GoogleTranslator wrapper (and its "
        "retry/backoff/throttle settings) as DB-content translation -- "
        "auto_translate._translate_text(). --backend gemini uses the Gemini API "
        "instead (same GEMINI_API_KEY/GEMINI_MODEL as the AI Coach feature), for an "
        "environment where GoogleTranslator's host (translate.google.com) is "
        "unreachable but Gemini's isn't. Per project policy this covers every "
        "settings.LANGUAGES entry except en (the source -- it has no .po file) and ar "
        "(human-reviewed only, never machine-overwritten by this command). Plural "
        "entries, entries with Python format placeholders (%(name)s, %s, %d), and "
        "entries containing HTML markup are skipped and left for manual translation, "
        "since a mistranslated placeholder or reordered tag would break rendering "
        "rather than just reading awkwardly. This is a one-time/occasional bulk "
        "backfill (run by a human, not triggered per-request), so it accepts running "
        "for a long time at a conservative pace rather than needing its own separate "
        "rate-limiting infrastructure -- and saves progress every "
        f"{_SAVE_EVERY} translated entries, so a quota cutoff partway through never "
        "loses already-translated work."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--language', action='append', dest='languages', default=None,
            help='Target language code, in Django\'s own LANGUAGES form (e.g. --language '
                 'zh-hans, not the zh_Hans locale-directory spelling). Repeatable. '
                 f'Defaults to {DEFAULT_LANGUAGES} if omitted.')
        parser.add_argument(
            '--force', action='store_true',
            help='Re-translate entries that already have a msgstr too, not just blank ones.')
        parser.add_argument(
            '--backend', choices=['google', 'gemini'], default='google',
            help='Translation backend. Defaults to google (GoogleTranslator, no key needed).')
        parser.add_argument(
            '--sleep', type=float, default=None,
            help='Seconds to sleep between calls. Defaults to a backend-appropriate pace '
                 f'({_BACKEND_DEFAULT_SLEEP}) if omitted.')

    def handle(self, *args, **options):
        languages = options['languages'] or DEFAULT_LANGUAGES
        backend = options['backend']
        sleep_seconds = options['sleep']
        if sleep_seconds is None:
            sleep_seconds = _BACKEND_DEFAULT_SLEEP[backend]

        if backend == 'google' and not auto_translate.is_configured():
            raise CommandError('AUTO_TRANSLATE_ENABLED is off -- nothing to do.')
        if backend == 'gemini' and not gemini_translate.is_configured():
            raise CommandError('GEMINI_API_KEY is not set -- nothing to do.')

        stopped_early = False
        for lang in languages:
            try:
                self._translate_language(lang, options['force'], backend, sleep_seconds)
            except QuotaStop:
                stopped_early = True
                self.stdout.write(self.style.WARNING(
                    f'\nStopped early: {backend} quota exceeded while translating {lang!r}. '
                    'Progress made so far (including in this language, up to the last '
                    f'{_SAVE_EVERY}-entry save point) has already been written to disk.'))
                break

        if stopped_early:
            remaining_index = languages.index(lang)
            not_yet_started = languages[remaining_index + 1:]
            if not_yet_started:
                self.stdout.write(
                    f'Not yet started: {", ".join(not_yet_started)}. Re-run with the same '
                    f'arguments once the quota resets -- already-translated entries are '
                    f'skipped automatically (without --force), so it resumes rather than '
                    f'redoing work.')

    def _translate_one(self, msgid, lang, backend):
        if backend == 'gemini':
            try:
                return gemini_translate.translate_text(msgid, lang)
            except gemini_translate.QuotaExceededError:
                raise
            except gemini_translate.TranslationError as exc:
                raise auto_translate.TranslationError(str(exc)) from exc
        return auto_translate._translate_text(msgid, lang)

    def _translate_language(self, lang, force, backend, sleep_seconds):
        # locale/ directories follow gettext's own naming (e.g. zh_Hans),
        # which diverges from Django's own LANGUAGES code (zh-hans) for
        # exactly the languages with a script/region subtag -- to_locale()
        # is Django's own conversion between the two, same one makemessages
        # uses internally to decide which directory to write to.
        po_path = settings.BASE_DIR / 'locale' / to_locale(lang) / 'LC_MESSAGES' / 'django.po'
        if not po_path.exists():
            raise CommandError(f'{po_path} does not exist -- run makemessages first.')

        po = polib.pofile(str(po_path))
        translated = 0
        skipped_plural = 0
        skipped_placeholder = 0
        skipped_html = 0
        since_last_save = 0

        for entry in po:
            if entry.obsolete:
                continue
            if not force and entry.msgstr:
                continue
            if entry.msgid_plural:
                skipped_plural += 1
                continue
            if _PLACEHOLDER_RE.search(entry.msgid):
                skipped_placeholder += 1
                continue
            if _HTML_RE.search(entry.msgid):
                skipped_html += 1
                continue

            try:
                entry.msgstr = self._translate_one(entry.msgid, lang, backend)
                translated += 1
                since_last_save += 1
            except gemini_translate.QuotaExceededError:
                po.save(str(po_path))
                self.stdout.write(self.style.SUCCESS(
                    f'[{lang}] translated {translated} entries before hitting the quota, '
                    f'saved to {po_path}.'))
                raise QuotaStop
            except auto_translate.TranslationError as exc:
                self.stdout.write(self.style.WARNING(
                    f'  [{lang}] {entry.msgid!r}: translation failed ({exc}) -- left as is.'))

            if since_last_save >= _SAVE_EVERY:
                po.save(str(po_path))
                since_last_save = 0

            if sleep_seconds:
                time.sleep(sleep_seconds)

        po.save(str(po_path))
        self.stdout.write(self.style.SUCCESS(
            f'[{lang}] translated {translated} entries, saved to {po_path}.'))
        if skipped_plural or skipped_placeholder or skipped_html:
            self.stdout.write(
                f'[{lang}] skipped (needs manual translation): '
                f'{skipped_plural} plural, {skipped_placeholder} with format placeholders, '
                f'{skipped_html} with HTML markup.')
