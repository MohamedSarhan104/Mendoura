from decouple import config
from django.core.management.base import BaseCommand

from courses.models import User


class Command(BaseCommand):
    help = (
        "Create or update the platform's admin/superuser account from environment "
        "variables. No-op if DJANGO_SUPERUSER_USERNAME/PASSWORD aren't set, so it's "
        'safe to leave in every deploy without accidentally creating an account '
        'with a blank or default password.'
    )

    def handle(self, *args, **options):
        username = config('DJANGO_SUPERUSER_USERNAME', default='')
        password = config('DJANGO_SUPERUSER_PASSWORD', default='')
        email = config('DJANGO_SUPERUSER_EMAIL', default='')

        if not username or not password:
            self.stdout.write(
                'DJANGO_SUPERUSER_USERNAME / DJANGO_SUPERUSER_PASSWORD not set -- '
                'skipping admin account setup.'
            )
            return

        user, created = User.objects.get_or_create(username=username, defaults={'email': email})
        if email:
            user.email = email
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()

        self.stdout.write(self.style.SUCCESS(
            f'{"Created" if created else "Updated"} admin account: {username}'
        ))
