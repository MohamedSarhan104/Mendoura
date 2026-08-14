from pathlib import Path

import polib
from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Compiles every locale/<lang>/LC_MESSAGES/django.po into its .mo file, in "
        "pure Python via polib -- unlike Django's own compilemessages, this needs no "
        "system gettext/msgfmt binary, so it's safe to run unconditionally as a build "
        "step on a platform that may not have gettext installed. Cheap and "
        "deterministic (no network calls), so -- unlike translate_po_entries, the "
        "actual machine-translation step -- it belongs in an automated build, as a "
        "safety net for a .po committed without its .mo compiled/committed alongside "
        "it, not just something run by hand before a commit."
    )

    def handle(self, *args, **options):
        locale_dir = Path(settings.BASE_DIR) / 'locale'
        po_paths = sorted(locale_dir.glob('*/LC_MESSAGES/django.po'))
        if not po_paths:
            self.stdout.write(self.style.WARNING(f'No .po files found under {locale_dir}.'))
            return

        for po_path in po_paths:
            po = polib.pofile(str(po_path))
            mo_path = po_path.with_suffix('.mo')
            po.save_as_mofile(str(mo_path))
            self.stdout.write(f'Compiled {po_path} -> {mo_path}')

        self.stdout.write(self.style.SUCCESS(f'Compiled {len(po_paths)} .po file(s).'))
