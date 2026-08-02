"""Transactional email sending -- a thin wrapper around Django's mail API,
same pattern as bunny.create_video / certificates.build_certificate_pdf, so
it's easy to find and to test in isolation from the model layer that calls
it.

Uses the EMAIL_BACKEND already configured in settings.py (Zoho SMTP in
production, falling back to the console backend whenever
EMAIL_HOST_PASSWORD isn't set) -- no new email provider is introduced here.
"""
import functools
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse

from . import certificates

logger = logging.getLogger(__name__)

SITE_DOMAIN = 'https://mendoura.com'


# Every send_* function (and _send() itself) returns one of these instead
# of None -- (True, None) only when the message was actually handed off
# and confirmed delivered by the backend with no exception, (False, reason)
# for everything else (no recipient, template/build error, or a real send
# failure). Callers that only care about "did this block me" can ignore
# the tuple; callers that report success to a human (the admin test-email
# tool) must check it instead of assuming success just because nothing
# raised past this module.
EmailResult = tuple  # (bool, str | None) -- not worth a NamedTuple for two fields


def _never_raises(func):
    """Email delivery is a side effect, not a precondition, for every
    trigger this module handles -- _send() already guards the final SMTP
    call, but a bug in template rendering, reverse(), or context building
    (everything each send_* function does before calling _send()) would
    otherwise propagate as an unhandled 500 for the caller, who by this
    point has already committed the real work (signup, password reset,
    certificate issuance) that the email is just confirming. Turns that
    into the same (False, reason) result _send() itself returns on
    failure, instead of a bare None that looks identical to success."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            logger.error('Failed to build/send email via %s', func.__name__, exc_info=True)
            return False, str(exc) or type(exc).__name__
    return wrapper


def humanize_duration(seconds: int) -> str:
    """Turns a settings-style duration in seconds into the "expires in X"
    wording used in the password reset email -- one source of truth
    (settings.PASSWORD_RESET_TIMEOUT) instead of a hardcoded number
    copy-pasted into the template text."""
    days = seconds // 86400
    if days >= 1:
        return f'{days} day{"s" if days != 1 else ""}'
    hours = seconds // 3600
    if hours >= 1:
        return f'{hours} hour{"s" if hours != 1 else ""}'
    minutes = max(seconds // 60, 1)
    return f'{minutes} minute{"s" if minutes != 1 else ""}'


def _send(*, subject, text_body, html_body, to_email, attachments=None) -> EmailResult:
    if not to_email:
        return False, 'No recipient email address.'
    message = EmailMultiAlternatives(
        subject=subject, body=text_body, from_email=settings.DEFAULT_FROM_EMAIL, to=[to_email])
    message.attach_alternative(html_body, 'text/html')
    for filename, content, mimetype in (attachments or []):
        message.attach(filename, content, mimetype)
    try:
        # fail_silently=False is Django's own default, but explicit here:
        # this is exactly the flag that would silently turn a real SMTP
        # failure into "0 delivered, no exception, nothing to catch" if it
        # were ever flipped -- never let that happen quietly again.
        sent_count = message.send(fail_silently=False)
    except Exception as exc:
        # Does not re-raise -- email delivery is a side effect, not a
        # precondition, for every trigger this module handles (signup,
        # password reset, certificate issuance all already succeeded by the
        # time we get here). Logged so a persistently-broken SMTP config
        # doesn't silently look identical to "nothing triggered it yet",
        # and returned so a caller that DOES need to know (the admin
        # test-email tool) can tell a real send apart from a fake one.
        logger.warning('Failed to send "%s" email to %s', subject, to_email, exc_info=True)
        return False, str(exc) or type(exc).__name__
    if not sent_count:
        # send() didn't raise, but delivered 0 messages -- e.g. a custom
        # backend that swallows failures internally. Don't call that a
        # success just because nothing was thrown.
        logger.warning('Email "%s" to %s: backend reported 0 messages sent.', subject, to_email)
        return False, 'The email backend reported 0 messages delivered.'
    return True, None


@_never_raises
def send_welcome_email(user, *, to_email=None) -> EmailResult:
    """Sent once, right after a new Student or Instructor account is
    created (before admin approval -- this just confirms the signup, it
    doesn't imply the account is already usable).

    to_email overrides the recipient without changing whose name/content
    is used -- only used by the admin "send a test email" tool so an admin
    can preview real copy at an arbitrary inbox."""
    to_email = to_email or user.email
    if not to_email:
        return False, 'This account has no email address on file.'

    name = user.get_full_name() or user.username
    context = {
        'student_name': name,
        'link_to_courses': f'{SITE_DOMAIN}{reverse("track_list")}',
    }
    html_body = render_to_string('emails/welcome_email.html', context)
    text_body = (
        f'Hi {name},\n\n'
        f'Welcome to Mendoura — we\'re glad you\'re here.\n\n'
        f'You now have access to expert-led courses across Tech, Languages, Marketing, '
        f'Business, and Design. Whether you\'re here to build a new skill or grow your '
        f'career, we\'re excited to be part of that journey.\n\n'
        f'Ready to get started? Browse our course tracks and pick your first course:\n'
        f'{context["link_to_courses"]}\n\n'
        f'Need help? Reach out to us anytime at support@mendoura.com — we\'re happy to assist.\n\n'
        f'Happy learning,\n'
        f'The Mendoura Team'
    )
    return _send(subject='Welcome to Mendoura! 🎉', text_body=text_body, html_body=html_body, to_email=to_email)


@_never_raises
def send_instructor_application_received_email(user, *, to_email=None) -> EmailResult:
    """Sent once, right at Instructor registration -- the lighter
    "we got it, hang tight" counterpart to send_instructor_welcome_email,
    which fires later at approval. Doesn't promise dashboard access, so
    it's safe to send before the account is approved.

    to_email overrides the recipient, same escape hatch as
    send_welcome_email -- used only by the admin test-email tool."""
    to_email = to_email or user.email
    if not to_email:
        return False, 'This account has no email address on file.'

    name = user.get_full_name() or user.username
    context = {'instructor_name': name}
    html_body = render_to_string('emails/instructor_application_received_email.html', context)
    text_body = (
        f'Hi {name},\n\n'
        f'Thanks for applying to become an instructor on Mendoura! We\'ve received your '
        f'application and our team is reviewing it.\n\n'
        f'We\'ll be in touch soon — once approved, you\'ll get full access to your instructor '
        f'dashboard and can start building your first course.\n\n'
        f'Questions in the meantime? Reach out to support@mendoura.com.\n\n'
        f'The Mendoura Team'
    )
    return _send(
        subject='We\'ve received your Mendoura instructor application',
        text_body=text_body, html_body=html_body, to_email=to_email,
    )


@_never_raises
def send_instructor_application_notification(user, *, to_email=None) -> EmailResult:
    """Internal notification, sent alongside
    send_instructor_application_received_email, so a new application
    doesn't sit unnoticed until someone happens to check the admin
    dashboard. Links to the admin pending-approvals page rather than a
    one-click approve action: the real approve_user endpoint is a
    POST-only, CSRF-protected, login-required form, and a plain email
    link can't submit that -- a one-click GET-based approve would also
    risk being silently triggered by corporate email security scanners
    or link-preview bots that auto-fetch every URL in an email.

    to_email overrides the recipient (default:
    settings.INSTRUCTOR_APPLICATION_NOTIFICATION_EMAIL) -- used only by
    the admin test-email tool to preview at an arbitrary inbox."""
    name = user.get_full_name() or user.username
    context = {
        'instructor_name': name,
        'username': user.username,
        'email': user.email or '—',
        'phone_number': user.phone_number or '—',
        'country': user.country or '—',
        'payoneer_account': user.payoneer_account or '—',
        'signup_date': user.date_joined,
        'admin_review_link': f'{SITE_DOMAIN}{reverse("admin_users")}',
    }
    html_body = render_to_string('emails/instructor_application_notification.html', context)
    text_body = (
        f'New Instructor application on Mendoura\n\n'
        f'Name: {name}\n'
        f'Username: {context["username"]}\n'
        f'Email: {context["email"]}\n'
        f'Phone: {context["phone_number"]}\n'
        f'Country: {context["country"]}\n'
        f'Payoneer account: {context["payoneer_account"]}\n'
        f'Applied: {user.date_joined.strftime("%B %d, %Y %H:%M UTC")}\n\n'
        f'Review and approve or reject this application:\n'
        f'{context["admin_review_link"]}'
    )
    return _send(
        subject=f'New Instructor application: {name}',
        text_body=text_body, html_body=html_body,
        to_email=to_email or settings.INSTRUCTOR_APPLICATION_NOTIFICATION_EMAIL,
    )


@_never_raises
def send_instructor_welcome_email(user, *, to_email=None) -> EmailResult:
    """Sent once, when an Instructor account is approved -- not at
    registration. The copy promises dashboard access ("You now have
    access to your instructor dashboard"), which is only true once
    approved; the generic Student welcome email fires at registration
    instead because its content (browsing the public course catalog)
    doesn't require an approved account.

    to_email overrides the recipient, same escape hatch as
    send_welcome_email -- used only by the admin test-email tool."""
    to_email = to_email or user.email
    if not to_email:
        return False, 'This account has no email address on file.'

    name = user.get_full_name() or user.username
    context = {
        'instructor_name': name,
        'instructor_dashboard_link': f'{SITE_DOMAIN}{reverse("instructor_dashboard")}',
    }
    html_body = render_to_string('emails/instructor_welcome_email.html', context)
    text_body = (
        f'Hi {name},\n\n'
        f'Welcome to Mendoura! We\'re excited to have you on board as an instructor.\n\n'
        f'You now have access to your instructor dashboard, where you can create courses, '
        f'track your compensation, and reach students across Egypt and around the world.\n\n'
        f'Ready to get started? Here\'s what\'s next:\n'
        f'1. Complete your instructor profile\n'
        f'2. Create your first course\n'
        f'3. Submit it for review\n\n'
        f'Go to your dashboard: {context["instructor_dashboard_link"]}\n\n'
        f'Questions about compensation, content guidelines, or anything else? Reach out to us '
        f'at support@mendoura.com — we\'re here to help.\n\n'
        f'Welcome aboard,\n'
        f'The Mendoura Team'
    )
    return _send(
        subject='Welcome to Mendoura — Let\'s build your first course 🎓',
        text_body=text_body, html_body=html_body, to_email=to_email,
    )


@_never_raises
def send_instructor_rejection_email(user, *, to_email=None) -> EmailResult:
    """Sent once, when an admin rejects a pending Instructor application
    (reject_user in views.py) -- the rejection counterpart to
    send_instructor_welcome_email above. Called before the User row is
    deleted, so the caller must pass whatever it still needs (this reads
    user.get_full_name()/username/email, all still populated in memory on
    an instance that's about to be deleted but hasn't been yet).

    to_email overrides the recipient, same escape hatch as
    send_welcome_email -- used only by the admin test-email tool."""
    to_email = to_email or user.email
    if not to_email:
        return False, 'This account has no email address on file.'

    name = user.get_full_name() or user.username
    context = {'instructor_name': name}
    html_body = render_to_string('emails/instructor_rejection_email.html', context)
    text_body = (
        f'Hi {name},\n\n'
        f'Thank you for your interest in becoming an instructor on Mendoura and for taking '
        f'the time to apply.\n\n'
        f'After reviewing your application, we\'re not able to move forward with it at this '
        f'time. This isn\'t necessarily a reflection of your expertise — we evaluate every '
        f'application against our current catalog needs and platform guidelines.\n\n'
        f'If you\'d like to apply again in the future, you\'re welcome to submit a new '
        f'application at any time.\n\n'
        f'Questions about this decision? Reach out to us at support@mendoura.com — we\'re '
        f'happy to talk it through.\n\n'
        f'Thank you,\n'
        f'The Mendoura Team'
    )
    return _send(
        subject='An update on your Mendoura instructor application',
        text_body=text_body, html_body=html_body, to_email=to_email,
    )


@_never_raises
def send_course_approved_email(course, *, to_email=None) -> EmailResult:
    """Sent once, when an admin approves a course from the Course Approval
    Queue (approve_course in views.py) -- approval publishes the course
    immediately (Course.status -> PUBLISHED, visible to students right
    away), so this confirms that rather than promising a further step.

    to_email overrides the recipient, same escape hatch as
    send_welcome_email -- used only by the admin test-email tool."""
    instructor = course.instructor
    to_email = to_email or instructor.email
    if not to_email:
        return False, 'This instructor has no email address on file.'

    name = instructor.get_full_name() or instructor.username
    context = {
        'instructor_name': name,
        'course_title': course.title,
        'course_link': f'{SITE_DOMAIN}{reverse("course_detail", args=[course.id])}',
    }
    html_body = render_to_string('emails/course_approved_email.html', context)
    text_body = (
        f'Hi {name},\n\n'
        f'Good news -- your course "{course.title}" has been approved and is now live on '
        f'Mendoura. Students can find and enroll in it right away.\n\n'
        f'View your course: {context["course_link"]}\n\n'
        f'Questions about what happens next? Reach out to us at support@mendoura.com.\n\n'
        f'Congratulations,\n'
        f'The Mendoura Team'
    )
    return _send(
        subject=f'"{course.title}" has been approved and is now live',
        text_body=text_body, html_body=html_body, to_email=to_email,
    )


# Shown in the rejection email whenever an admin somehow submits reject_course()
# with a blank reason (the form requires one, but this stays as a defensive
# fallback rather than ever sending a rejection email with an empty reason
# section).
DEFAULT_COURSE_REJECTION_REASON = (
    'No specific reason was provided. Please reach out to support@mendoura.com for details.'
)


@_never_raises
def send_course_rejected_email(course, *, to_email=None) -> EmailResult:
    """Sent once, when an admin rejects a course from the Course Approval
    Queue (reject_course in views.py) -- the rejection counterpart to
    send_course_approved_email above. Reads course.rejection_reason, which
    reject_course() already saves onto the course itself before calling
    this, so the instructor's own course record and this email never
    disagree about why it was rejected.

    to_email overrides the recipient, same escape hatch as
    send_welcome_email -- used only by the admin test-email tool."""
    instructor = course.instructor
    to_email = to_email or instructor.email
    if not to_email:
        return False, 'This instructor has no email address on file.'

    name = instructor.get_full_name() or instructor.username
    reason = course.rejection_reason.strip() or DEFAULT_COURSE_REJECTION_REASON
    context = {
        'instructor_name': name,
        'course_title': course.title,
        'rejection_reason': reason,
        'edit_course_link': f'{SITE_DOMAIN}{reverse("edit_course", args=[course.id])}',
    }
    html_body = render_to_string('emails/course_rejected_email.html', context)
    text_body = (
        f'Hi {name},\n\n'
        f'Your course "{course.title}" was not approved in its current form.\n\n'
        f'Reason given by our review team:\n'
        f'{reason}\n\n'
        f'You can update the course and resubmit it for review at any time:\n'
        f'{context["edit_course_link"]}\n\n'
        f'Questions about this decision? Reach out to us at support@mendoura.com -- we\'re '
        f'happy to talk it through.\n\n'
        f'Thank you,\n'
        f'The Mendoura Team'
    )
    return _send(
        subject=f'"{course.title}" needs changes before it can go live',
        text_body=text_body, html_body=html_body, to_email=to_email,
    )


@_never_raises
def send_certificate_email(certificate, *, to_email=None) -> EmailResult:
    """Emails the student their certificate PDF once it's been generated.
    Silently does nothing if the certificate has no PDF yet -- the caller
    (Certificate.generate_pdf, via Enrollment.issue_certificate_if_complete)
    always generates the PDF first, so this should never actually happen in
    practice; it's just a defensive guard against being called out of
    order.

    to_email overrides the recipient, same escape hatch as
    send_welcome_email -- used only by the admin test-email tool."""
    if not certificate.pdf_file:
        return False, 'This certificate has no generated PDF yet.'

    enrollment = certificate.enrollment
    student = enrollment.student
    course = enrollment.course

    to_email = to_email or student.email
    if not to_email:
        return False, 'This student has no email address on file.'

    student_name = student.get_full_name() or student.username
    if course.track:
        related_courses_link = f'{SITE_DOMAIN}{reverse("track_detail", args=[course.track.slug])}'
    else:
        related_courses_link = f'{SITE_DOMAIN}{reverse("track_list")}'

    context = {
        'student_name': student_name,
        'course_name': course.title,
        'certificate_link': certificates.verification_url(certificate),
        'linkedin_share_link': certificates.linkedin_share_url(certificate),
        'related_courses_link': related_courses_link,
    }
    html_body = render_to_string('emails/certificate_email.html', context)
    text_body = (
        f'Hi {student_name},\n\n'
        f'Congratulations on completing {course.title}! Your dedication and hard work have '
        f'paid off, and we\'re proud to have been part of your learning journey.\n\n'
        f'Your certificate is attached to this email, and you can also access it anytime '
        f'from your dashboard: {context["certificate_link"]}\n\n'
        f'Want to show off your achievement? Share it on LinkedIn and let your network know:\n'
        f'{context["linkedin_share_link"]}\n\n'
        f'Keep the momentum going — check out related courses to continue building your skills:\n'
        f'{context["related_courses_link"]}\n\n'
        f'Well done,\n'
        f'The Mendoura Team'
    )

    certificate.pdf_file.open('rb')
    try:
        pdf_bytes = certificate.pdf_file.read()
    finally:
        certificate.pdf_file.close()

    return _send(
        subject=f'🎓 Congratulations! You\'ve completed {course.title}',
        text_body=text_body, html_body=html_body, to_email=to_email,
        attachments=[(f'certificate-{certificate.uuid}.pdf', pdf_bytes, 'application/pdf')],
    )


STUDENT_RESET_TEMPLATES = (
    'registration/password_reset_subject.txt',
    'registration/password_reset_email.txt',
    'registration/password_reset_email.html',
)
INSTRUCTOR_RESET_TEMPLATES = (
    'registration/instructor_password_reset_subject.txt',
    'registration/instructor_password_reset_email.txt',
    'registration/instructor_password_reset_email.html',
)


@_never_raises
def send_password_reset_preview(user, request, *, as_instructor: bool) -> EmailResult:
    """Admin test-email tool only. Sends a real, working password-reset
    link for `user`'s own account (never redirectable to another address),
    but lets the admin force which template set to preview -- Student or
    Instructor -- regardless of the account's actual is_instructor flag.
    The real user-facing flow (RoleAwarePasswordResetForm in forms.py)
    always auto-selects correctly from the account's real role and never
    needs this override; this exists only so one admin account can preview
    both copies without needing a second, real Instructor test account."""
    from django.contrib.auth import get_user_model
    from django.contrib.sites.shortcuts import get_current_site
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode

    if not user.email:
        return False, 'This account has no email address on file.'

    UserModel = get_user_model()
    current_site = get_current_site(request)
    context = {
        'email': user.email,
        'domain': current_site.domain,
        'site_name': current_site.name,
        'uid': urlsafe_base64_encode(force_bytes(UserModel._meta.pk.value_to_string(user))),
        'user': user,
        'token': default_token_generator.make_token(user),
        'protocol': 'https' if request.is_secure() else 'http',
        'expiry_time': humanize_duration(settings.PASSWORD_RESET_TIMEOUT),
    }
    subject_t, text_t, html_t = INSTRUCTOR_RESET_TEMPLATES if as_instructor else STUDENT_RESET_TEMPLATES
    subject = ''.join(render_to_string(subject_t, context).splitlines())
    text_body = render_to_string(text_t, context)
    html_body = render_to_string(html_t, context)
    return _send(subject=subject, text_body=text_body, html_body=html_body, to_email=user.email)
