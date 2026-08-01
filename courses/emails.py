"""Transactional email sending -- a thin wrapper around Django's mail API,
same pattern as bunny.create_video / certificates.build_certificate_pdf, so
it's easy to find and to test in isolation from the model layer that calls
it.

Uses the EMAIL_BACKEND already configured in settings.py (Zoho SMTP in
production, falling back to the console backend whenever
EMAIL_HOST_PASSWORD isn't set) -- no new email provider is introduced here.
"""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse

from . import certificates

logger = logging.getLogger(__name__)

SITE_DOMAIN = 'https://mendoura.com'


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


def _send(*, subject, text_body, html_body, to_email, attachments=None) -> None:
    if not to_email:
        return
    message = EmailMultiAlternatives(
        subject=subject, body=text_body, from_email=settings.DEFAULT_FROM_EMAIL, to=[to_email])
    message.attach_alternative(html_body, 'text/html')
    for filename, content, mimetype in (attachments or []):
        message.attach(filename, content, mimetype)
    try:
        message.send()
    except Exception:
        # Never blocks the caller -- email delivery is a side effect, not a
        # precondition, for every trigger this module handles (signup,
        # password reset, certificate issuance all already succeeded by the
        # time we get here). Logged so a persistently-broken SMTP config
        # doesn't silently look identical to "nothing triggered it yet".
        logger.warning('Failed to send "%s" email to %s', subject, to_email, exc_info=True)


def send_welcome_email(user, *, to_email=None) -> None:
    """Sent once, right after a new Student or Instructor account is
    created (before admin approval -- this just confirms the signup, it
    doesn't imply the account is already usable).

    to_email overrides the recipient without changing whose name/content
    is used -- only used by the admin "send a test email" tool so an admin
    can preview real copy at an arbitrary inbox."""
    to_email = to_email or user.email
    if not to_email:
        return

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
    _send(subject='Welcome to Mendoura! 🎉', text_body=text_body, html_body=html_body, to_email=to_email)


def send_certificate_email(certificate, *, to_email=None) -> None:
    """Emails the student their certificate PDF once it's been generated.
    Silently does nothing if the certificate has no PDF yet -- the caller
    (Certificate.generate_pdf, via Enrollment.issue_certificate_if_complete)
    always generates the PDF first, so this should never actually happen in
    practice; it's just a defensive guard against being called out of
    order.

    to_email overrides the recipient, same escape hatch as
    send_welcome_email -- used only by the admin test-email tool."""
    if not certificate.pdf_file:
        return

    enrollment = certificate.enrollment
    student = enrollment.student
    course = enrollment.course

    to_email = to_email or student.email
    if not to_email:
        return

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

    _send(
        subject=f'🎓 Congratulations! You\'ve completed {course.title}',
        text_body=text_body, html_body=html_body, to_email=to_email,
        attachments=[(f'certificate-{certificate.uuid}.pdf', pdf_bytes, 'application/pdf')],
    )
