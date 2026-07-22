from django.core.management.base import BaseCommand

from courses.models import Plan

PLANS = [
    {
        'name': 'Mendoura Monthly Pass',
        'interval': Plan.Interval.MONTHLY,
        'price_egp': 300,
        'price_usd': 10,
        'duration_days': 30,
    },
    {
        'name': 'Mendoura Annual Pass',
        'interval': Plan.Interval.ANNUAL,
        'price_egp': 2000,
        'price_usd': 65,
        'duration_days': 365,
    },
]


class Command(BaseCommand):
    help = 'Sync the Mendoura subscription plans (idempotent, safe to re-run).'

    def handle(self, *args, **options):
        for data in PLANS:
            plan, created = Plan.objects.update_or_create(
                name=data['name'],
                defaults={**data, 'is_active': True},
            )
            self.stdout.write(
                self.style.SUCCESS(f'Created plan: {plan.name}') if created
                else f'Synced: {plan.name}'
            )
