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

logger = logging.getLogger(__name__)

SITE_DOMAIN = 'https://mendoura.com'


def _verification_url(certificate) -> str:
    return f'{SITE_DOMAIN}{reverse("certificate_verify_short", args=[certificate.uuid])}'


def send_certificate_email(certificate) -> None:
    """Emails the student their certificate PDF once it's been generated.
    Silently does nothing if the certificate has no PDF yet -- the caller
    (Certificate.generate_pdf, via Enrollment.issue_certificate_if_complete)
    always generates the PDF first, so this should never actually happen in
    practice; it's just a defensive guard against being called out of
    order."""
    if not certificate.pdf_file:
        return

    enrollment = certificate.enrollment
    student = enrollment.student
    course = enrollment.course

    if not student.email:
        return

    student_name = student.get_full_name() or student.username
    context = {
        'student_name': student_name,
        'course_title': course.title,
        'verification_url': _verification_url(certificate),
    }
    html_body = render_to_string('emails/certificate_email.html', context)
    text_body = (
        f'Congratulations, {student_name}!\n\n'
        f'You\'ve successfully completed "{course.title}" on Mendoura. '
        f'Your certificate is attached to this email.\n\n'
        f'Verify or share it anytime at: {context["verification_url"]}\n\n'
        f'Consider adding it to your LinkedIn profile to showcase your new skills.'
    )

    message = EmailMultiAlternatives(
        subject=f'You did it! Your Mendoura certificate for "{course.title}"',
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[student.email],
    )
    message.attach_alternative(html_body, 'text/html')
    certificate.pdf_file.open('rb')
    try:
        message.attach(
            f'certificate-{certificate.uuid}.pdf', certificate.pdf_file.read(), 'application/pdf')
    finally:
        certificate.pdf_file.close()

    try:
        message.send()
    except Exception:
        # Never blocks certificate issuance -- the PDF is already saved and
        # downloadable from the dashboard regardless of whether the email
        # goes out. Logged so a persistently-broken SMTP config doesn't
        # silently look identical to "no certificates issued yet".
        logger.warning(
            'Failed to send certificate email for certificate uuid=%s to %s',
            certificate.uuid, student.email, exc_info=True)
