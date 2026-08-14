from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from courses import auto_translate
from courses.models import LegalDocument, LegalSection, Track

MODEL_CHOICES = {
    'track': Track,
    'legaldocument': LegalDocument,
    'legalsection': LegalSection,
}


class Command(BaseCommand):
    help = (
        "Force-retranslate Track/LegalDocument/LegalSection content into one or more "
        "languages, without needing to touch the English source text. "
        "AutoTranslatedFieldsMixin (courses/models.py) only retries a language "
        "automatically when its source text changes or the language is entirely "
        "missing from the cached translations -- this is the explicit 'try again "
        "right now' escape hatch for a language that's been stuck (e.g. after a "
        "past GoogleTranslator failure, or right after adding a new language to "
        "settings.LANGUAGES). By default only fills in languages missing from the "
        "cached translations; pass --force to redo ones that already have a value too."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--language', action='append', dest='languages', required=True,
            help="Target language code to (re)translate, e.g. --language fr. Repeatable. "
                 "Pass --language all once to mean every non-English language in "
                 "settings.LANGUAGES, instead of listing each one out.")
        parser.add_argument(
            '--model', choices=[*MODEL_CHOICES, 'all'], default='all',
            help='Restrict to one model (track/legaldocument/legalsection), or all (default).')
        parser.add_argument(
            '--force', action='store_true',
            help='Re-translate languages that already have a cached value too, not just missing ones.')

    def handle(self, *args, **options):
        valid_codes = {code for code, _label in settings.LANGUAGES}
        if options['languages'] == ['all']:
            languages = sorted(valid_codes - {'en'})
        else:
            languages = options['languages']
            for lang in languages:
                if lang not in valid_codes:
                    raise CommandError(f'{lang!r} is not in settings.LANGUAGES ({sorted(valid_codes)}).')

        if not auto_translate.is_configured():
            raise CommandError('AUTO_TRANSLATE_ENABLED is off -- nothing to do.')

        models = [MODEL_CHOICES[options['model']]] if options['model'] != 'all' else list(MODEL_CHOICES.values())
        force = options['force']
        total_updated = 0
        for model in models:
            for instance in model.objects.all():
                if self._retranslate_instance(instance, languages, force):
                    total_updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'Done -- retranslated {", ".join(languages)} on {total_updated} record(s).'))

    def _retranslate_instance(self, instance, languages, force):
        """Directly writes the new translations via a queryset .update(),
        deliberately bypassing instance.save() -- every AutoTranslatedFieldsMixin
        subclass's save() unconditionally calls _autotranslate() again, which
        would immediately re-attempt every OTHER still-incomplete language too
        (e.g. one this command wasn't asked to touch), defeating the point of
        asking for just `languages`."""
        pending_by_field = {}
        for field in instance.TRANSLATABLE_FIELDS:
            source_value = (getattr(instance, field, '') or '').strip()
            if not source_value:
                continue
            translations = getattr(instance, f'{field}_translations') or {}
            needed = languages if force else [lang for lang in languages if lang not in translations]
            if needed:
                pending_by_field[field] = (source_value, needed)

        if not pending_by_field:
            return False

        update_kwargs = {}
        for field, (source_value, needed_langs) in pending_by_field.items():
            results = auto_translate.translate_fields({field: source_value}, needed_langs)
            new_translations = results.get(field) or {}
            missing = [lang for lang in needed_langs if lang not in new_translations]
            if missing:
                self.stdout.write(self.style.WARNING(
                    f'  {instance!r} field={field!r}: {missing} still failed -- left as is, retry later.'))
            if not new_translations:
                continue
            translations = dict(getattr(instance, f'{field}_translations') or {})
            translations.update(new_translations)
            translations['__source__'] = source_value
            update_kwargs[f'{field}_translations'] = translations
            self.stdout.write(f'  {instance!r} field={field!r}: updated {sorted(new_translations)}.')

        if not update_kwargs:
            return False
        type(instance).objects.filter(pk=instance.pk).update(**update_kwargs)
        return True
