from django.utils import timezone

from .models import Enrollment, Payment, Subscription


def student_has_access(user, course) -> bool:
    """Single source of truth: is `user` entitled to `course`'s content right
    now? Every gated view must call this -- never infer access from template
    state, since a subscription can expire after an Enrollment was created.

    Deliberately does NOT special-case course.is_free: a free course still
    requires the student to actually enroll (one click, no payment) before
    its lectures unlock -- it isn't automatically public to anyone who shows
    up. That one click is exactly what creates the Enrollment row this
    checks for."""
    if not user.is_authenticated:
        return False
    if Enrollment.objects.filter(student=user, course=course).exists():
        return True
    if Payment.objects.filter(student=user, course=course, status=Payment.Status.SUCCEEDED).exists():
        return True
    return Subscription.objects.filter(
        student=user, status=Subscription.Status.ACTIVE, expires_at__gt=timezone.now(),
    ).exists()


def get_or_create_enrollment(user, course):
    """Returns the student's Enrollment for `course` if one already exists or
    if their subscription entitles them to one, auto-creating it on first
    view so an active subscriber gets frictionless access to every course
    without an explicit per-course purchase click. Free courses are NOT
    auto-enrolled here -- that stays an explicit action in enroll_course().
    Returns None if not entitled."""
    enrollment = Enrollment.objects.filter(student=user, course=course).first()
    if enrollment:
        return enrollment
    if not user.is_authenticated:
        return None
    if Payment.objects.filter(student=user, course=course, status=Payment.Status.SUCCEEDED).exists():
        # The Paymob webhook creates this Enrollment already; reaching here
        # would mean that step failed silently, so don't paper over it.
        return None
    if Subscription.objects.filter(
        student=user, status=Subscription.Status.ACTIVE, expires_at__gt=timezone.now(),
    ).exists():
        return Enrollment.objects.create(student=user, course=course, via_subscription=True)
    return None
