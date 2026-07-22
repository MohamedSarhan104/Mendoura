from collections import defaultdict
from decimal import ROUND_DOWN, Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from courses.models import (
    Course, InstructorWallet, RevenueDistribution, SubscriptionPeriod, WalletTransaction, WatchEvent,
)
from courses.money import SUBSCRIPTION_INSTRUCTOR_SHARE, calculate_split

# A view under this many seconds doesn't count -- otherwise an instructor
# could farm revenue by getting people to open and immediately close a lecture.
MINIMUM_VIEW_SECONDS = 30


class Command(BaseCommand):
    help = (
        'Distribute subscription revenue for every SubscriptionPeriod that has '
        'ended, pro-rata by watch-time across the courses the subscriber '
        'actually watched during that period. Idempotent -- safe to re-run '
        '(a crash or timeout WILL cause a re-run; already-distributed periods '
        'are skipped, never re-paid).'
    )

    def handle(self, *args, **options):
        due_periods = SubscriptionPeriod.objects.filter(
            status=SubscriptionPeriod.Status.OPEN, period_end__lte=timezone.now(),
        ).select_related('subscription')

        distributed_count = 0
        for period in due_periods:
            if self._distribute_one(period.id):
                distributed_count += 1

        self.stdout.write(self.style.SUCCESS(
            f'Distributed {distributed_count} subscription period(s).'))

    @transaction.atomic
    def _distribute_one(self, period_id) -> bool:
        # select_for_update + a fresh status check inside the transaction is
        # what makes a concurrent or repeated run of this command safe: two
        # processes racing on the same period will serialize here, and
        # whichever loses the race sees status != OPEN and does nothing.
        period = SubscriptionPeriod.objects.select_for_update().get(id=period_id)
        if period.status != SubscriptionPeriod.Status.OPEN:
            return False

        student = period.subscription.student

        course_seconds = self._aggregate_watch_seconds(student, period)

        total_seconds = sum(course_seconds.values())
        if total_seconds == 0:
            period.status = SubscriptionPeriod.Status.DISTRIBUTED
            period.distributed_at = timezone.now()
            period.save()
            return True

        # Stable ordering is what makes "the last course gets the exact
        # remainder" land on the same course every time this is computed --
        # if the ordering ever drifted between runs, so would the remainder.
        courses = list(
            Course.objects.filter(id__in=course_seconds.keys())
            .select_related('instructor').order_by('id')
        )

        allocated = Decimal('0.00')
        for i, course in enumerate(courses):
            seconds = course_seconds[course.id]
            share = Decimal(seconds) / Decimal(total_seconds)

            if i == len(courses) - 1:
                attributed = period.amount_paid - allocated  # exact remainder
            else:
                attributed = (period.amount_paid * share).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
                allocated += attributed

            # Flat subscription split -- NOT the course's own production_type
            # rule. That rule only governs direct one-off sales.
            instructor_amount, platform_amount = calculate_split(attributed, SUBSCRIPTION_INSTRUCTOR_SHARE)

            wallet, _ = InstructorWallet.objects.get_or_create(instructor=course.instructor)
            wallet = InstructorWallet.objects.select_for_update().get(pk=wallet.pk)
            wallet.available_balance += instructor_amount
            wallet.total_earnings += instructor_amount
            wallet.save()

            minutes = seconds // 60
            wallet_txn = WalletTransaction.objects.create(
                wallet=wallet, type=WalletTransaction.Type.SALE_CREDIT,
                amount=instructor_amount, balance_after=wallet.available_balance,
                note=(
                    f'Subscription revenue: {minutes} min watched on "{course.title}" '
                    f'({SUBSCRIPTION_INSTRUCTOR_SHARE}% subscription pool)'
                ),
            )
            RevenueDistribution.objects.create(
                period=period, course=course, instructor=course.instructor,
                seconds_watched=seconds, share_of_period=share.quantize(Decimal('0.000001')),
                attributed_amount=attributed, instructor_share_pct=SUBSCRIPTION_INSTRUCTOR_SHARE,
                instructor_amount=instructor_amount, platform_amount=platform_amount,
                wallet_transaction=wallet_txn,
            )

        period.status = SubscriptionPeriod.Status.DISTRIBUTED
        period.distributed_at = timezone.now()
        period.save()
        return True

    def _aggregate_watch_seconds(self, student, period):
        """Seconds watched per course during the period, with anti-fraud
        rules applied: the student's own courses are excluded (self-dealing),
        each lecture is capped at 2x its own duration (stops loop-farming a
        short lecture), and any lecture watched under MINIMUM_VIEW_SECONDS
        doesn't count at all."""
        per_lecture = (
            WatchEvent.objects.filter(
                student=student, occurred_at__gte=period.period_start, occurred_at__lt=period.period_end,
            )
            .exclude(course__instructor=student)
            .values('lecture_id', 'course_id', 'lecture__duration_seconds')
            .annotate(total_seconds=Sum('seconds_watched'))
        )

        course_seconds = defaultdict(int)
        for row in per_lecture:
            seconds = row['total_seconds']
            duration = row['lecture__duration_seconds']
            if duration:
                seconds = min(seconds, duration * 2)
            if seconds < MINIMUM_VIEW_SECONDS:
                continue
            course_seconds[row['course_id']] += seconds
        return course_seconds
