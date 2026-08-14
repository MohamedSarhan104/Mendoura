from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from courses import emails
from courses.models import Certificate


class Command(BaseCommand):
    help = (
        "Manually (re)send a certificate's email, for a certificate that was issued "
        "but whose email silently failed to send (e.g. the storage-read-back bug fixed "
        "alongside this command -- a certificate can end up with a real PDF on record "
        "but no email ever delivered, since @_never_raises swallowed the read failure). "
        "send_certificate_email now falls back to an in-memory copy of the PDF if the "
        "stored one can't be read, so this alone is enough to get a real send through."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--uuid', required=True,
            help='The certificate\'s verification UUID (shown on its certificate page/URL).')

    def handle(self, *args, **options):
        try:
            certificate = Certificate.objects.get(uuid=options['uuid'])
        except (Certificate.DoesNotExist, ValueError, ValidationError) as exc:
            raise CommandError(f'No certificate found with uuid={options["uuid"]!r}.') from exc

        sent, reason = emails.send_certificate_email(certificate)
        if sent:
            student = certificate.enrollment.student
            self.stdout.write(self.style.SUCCESS(
                f'Sent certificate email for {certificate!r} to {student.email or student.username}.'))
        else:
            raise CommandError(f'Email was not sent: {reason}')
