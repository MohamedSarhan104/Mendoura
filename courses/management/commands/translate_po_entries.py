import re

import polib
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from courses import auto_translate

# fr/es are the two "real" supported UI-chrome languages beyond the
# human-reviewed en/ar pair (see the project's .po policy: en/ar stay
# human-review-only, fr/es get machine-translated static UI strings same as
# DB content already does, and the remaining 9 languages keep falling back
# to English for UI chrome). Kept as an explicit default rather than
# derived from settings.LANGUAGES so this command never silently starts
# machine-translating a language the policy hasn't actually signed off on.
DEFAULT_LANGUAGES = ['fr', 'es']

# Anything with a Python format placeholder (%(name)s, %s, %d, ...) needs
# the placeholder itself preserved byte-for-byte, and a plural entry needs
# msgid_plural/msgstr[n] handled instead of a single msgstr -- both are
# skipped rather than risking GoogleTranslator mangling a placeholder or
# this command guessing at plural forms it has no data for. HTML markup is
# skipped for the same reason translate_markdown() never sends Markdown
# table/bullet syntax through translation: GoogleTranslator only
# understands plain text, and tags in the msgid (e.g. "<a href=...>...</a>"
# from an old blocktrans) risk coming back reordered or broken.
_PLACEHOLDER_RE = re.compile(r'%\(|%[sd]')
_HTML_RE = re.compile(r'<[a-zA-Z/][^>]*>')


def _is_translatable(entry: polib.POEntry) -> bool:
    if entry.obsolete or entry.msgid_plural:
        return False
    if _PLACEHOLDER_RE.search(entry.msgid):
        return False
    if _HTML_RE.search(entry.msgid):
        return False
    return True


class Command(BaseCommand):
    help = (
        "Machine-translate blank msgstr entries in locale/<lang>/LC_MESSAGES/django.po "
        "using the same GoogleTranslator wrapper as DB-content translation "
        "(courses.auto_translate). Per project policy this is only meant to run for "
        "fr/es -- en has no .po file (it's the source language) and ar is "
        "human-reviewed only, never machine-overwritten by this command. Plural "
        "entries, entries with Python format placeholders (%(name)s, %s, %d), and "
        "entries containing HTML markup are skipped and left for manual translation, "
        "since a mistranslated placeholder or reordered tag would break rendering "
        "rather than just reading awkwardly."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--language', action='append', dest='languages', default=None,
            help=f'Target language code, e.g. --language fr. Repeatable. '
                 f'Defaults to {DEFAULT_LANGUAGES} if omitted.')
        parser.add_argument(
            '--force', action='store_true',
            help='Re-translate entries that already have a msgstr too, not just blank ones.')

    def handle(self, *args, **options):
        languages = options['languages'] or DEFAULT_LANGUAGES
        if not auto_translate.is_configured():
            raise CommandError('AUTO_TRANSLATE_ENABLED is off -- nothing to do.')

        for lang in languages:
            self._translate_language(lang, options['force'])

    def _translate_language(self, lang, force):
        po_path = settings.BASE_DIR / 'locale' / lang / 'LC_MESSAGES' / 'django.po'
        if not po_path.exists():
            raise CommandError(f'{po_path} does not exist -- run makemessages first.')

        po = polib.pofile(str(po_path))
        translated = 0
        skipped_plural = 0
        skipped_placeholder = 0
        skipped_html = 0

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
                entry.msgstr = auto_translate._translate_text(entry.msgid, lang)
                translated += 1
            except auto_translate.TranslationError as exc:
                self.stdout.write(self.style.WARNING(
                    f'  [{lang}] {entry.msgid!r}: translation failed ({exc}) -- left as is.'))

        po.save(str(po_path))
        self.stdout.write(self.style.SUCCESS(
            f'[{lang}] translated {translated} entries, saved to {po_path}.'))
        if skipped_plural or skipped_placeholder or skipped_html:
            self.stdout.write(
                f'[{lang}] skipped (needs manual translation): '
                f'{skipped_plural} plural, {skipped_placeholder} with format placeholders, '
                f'{skipped_html} with HTML markup.')
