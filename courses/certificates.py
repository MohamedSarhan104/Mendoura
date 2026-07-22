"""Certificate PDF rendering -- a thin wrapper around reportlab.

The one PDF-building call (build_certificate_pdf) is isolated here, same
pattern as bunny.create_video / ai_coach.send_message, so it's easy to
find and to test in isolation from the view/model layer that calls it.
"""
import io
import os

from django.conf import settings
from django.urls import reverse
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import landscape, letter
from reportlab.pdfgen import canvas

BRAND_PURPLE = HexColor('#7c3aed')
INK = HexColor('#1e1b2e')
MUTED = HexColor('#6b7280')

PAGE_WIDTH, PAGE_HEIGHT = landscape(letter)
LOGO_PATH = os.path.join(settings.BASE_DIR, 'static', 'img', 'logo.png')
SITE_DOMAIN = 'https://mendoura.com'


def _verification_url(certificate) -> str:
    return f'{SITE_DOMAIN}{reverse("certificate_verify", args=[certificate.uuid])}'


def build_certificate_pdf(certificate) -> bytes:
    """Renders a landscape "Statement of Accomplishment" PDF entirely in
    memory and returns the raw bytes -- nothing is written to disk here;
    the caller decides whether/where to persist it."""
    enrollment = certificate.enrollment
    student = enrollment.student
    course = enrollment.course
    instructor = course.instructor

    student_name = student.get_full_name() or student.username
    instructor_name = instructor.get_full_name() or instructor.username

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(PAGE_WIDTH, PAGE_HEIGHT))

    # Outer + inner border frame for a clean, formal look.
    margin = 28
    c.setStrokeColor(BRAND_PURPLE)
    c.setLineWidth(2.2)
    c.rect(margin, margin, PAGE_WIDTH - 2 * margin, PAGE_HEIGHT - 2 * margin)
    inner_margin = margin + 10
    c.setLineWidth(0.75)
    c.rect(inner_margin, inner_margin, PAGE_WIDTH - 2 * inner_margin, PAGE_HEIGHT - 2 * inner_margin)

    center_x = PAGE_WIDTH / 2

    # Logo, if present -- kept small and centered above the wordmark.
    if os.path.exists(LOGO_PATH):
        logo_h = 46
        logo_w = logo_h * 105 / 153
        c.drawImage(
            LOGO_PATH, center_x - logo_w / 2, PAGE_HEIGHT - 100, width=logo_w, height=logo_h,
            mask='auto', preserveAspectRatio=True,
        )

    c.setFillColor(INK)
    c.setFont('Helvetica-Bold', 20)
    c.drawCentredString(center_x, PAGE_HEIGHT - 118, 'MENDOURA')

    c.setFillColor(BRAND_PURPLE)
    c.setFont('Helvetica-Bold', 13)
    c.drawCentredString(center_x, PAGE_HEIGHT - 145, 'MENDOURA LMS STATEMENT OF ACCOMPLISHMENT')

    c.setFillColor(MUTED)
    c.setFont('Helvetica', 12)
    c.drawCentredString(center_x, PAGE_HEIGHT - 195, 'This is to certify that')

    c.setFillColor(INK)
    c.setFont('Helvetica-Bold', 32)
    c.drawCentredString(center_x, PAGE_HEIGHT - 235, student_name)

    c.setFillColor(MUTED)
    c.setFont('Helvetica', 12)
    c.drawCentredString(center_x, PAGE_HEIGHT - 270, 'has successfully completed the course')

    c.setFillColor(BRAND_PURPLE)
    c.setFont('Helvetica-Bold', 22)
    c.drawCentredString(center_x, PAGE_HEIGHT - 305, course.title)

    # Instructor / date "signature line" columns near the bottom.
    line_y = 120
    col_width = 220
    left_x = center_x - col_width - 20
    right_x = center_x + 20

    c.setStrokeColor(MUTED)
    c.setLineWidth(0.75)
    c.line(left_x, line_y, left_x + col_width, line_y)
    c.line(right_x, line_y, right_x + col_width, line_y)

    c.setFillColor(INK)
    c.setFont('Helvetica-Bold', 11)
    c.drawCentredString(left_x + col_width / 2, line_y - 16, instructor_name)
    c.drawCentredString(right_x + col_width / 2, line_y - 16, certificate.issued_at.strftime('%B %d, %Y'))

    c.setFillColor(MUTED)
    c.setFont('Helvetica', 9)
    c.drawCentredString(left_x + col_width / 2, line_y + 6, 'Instructor')
    c.drawCentredString(right_x + col_width / 2, line_y + 6, 'Date Issued')

    # Verification footer.
    c.setFillColor(MUTED)
    c.setFont('Helvetica', 8)
    c.drawCentredString(center_x, 65, f'Certificate ID: {certificate.uuid}')
    c.drawCentredString(center_x, 52, f'Verify this certificate at {_verification_url(certificate)}')

    c.showPage()
    c.save()
    return buffer.getvalue()
