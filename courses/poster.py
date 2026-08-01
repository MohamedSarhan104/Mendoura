"""Course video-player poster compositing -- a thin wrapper around Pillow,
same pattern as certificates.build_certificate_pdf / bunny.create_video, so
it's easy to find and to test in isolation from the model layer that calls
it.

Always produces a 1280x720 (16:9) image: the instructor's uploaded
Course.thumbnail cover-cropped to fit, or a branded gradient placeholder if
none was uploaded, with a dark readability gradient and the course title +
instructor name composited on top in the real Inter font (bundled under
static/fonts/ -- Google Fonts isn't reachable from every environment this
runs in, and Pillow can't use a browser @font-face declaration).
"""
import io
import os
import textwrap

from django.conf import settings
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1280, 720

BRAND_CYAN = (34, 211, 238)      # #22d3ee
BRAND_PURPLE = (124, 58, 237)    # #7c3aed
WHITE = (255, 255, 255)
INSTRUCTOR_NAME_COLOR = (196, 214, 255)  # soft cyan-lavender tint

FONTS_DIR = os.path.join(settings.BASE_DIR, 'static', 'fonts')
LOGO_PATH = os.path.join(settings.BASE_DIR, 'static', 'img', 'logo.png')

PADDING = 64
TITLE_SIZE = 54
NAME_SIZE = 28
TITLE_MAX_LINES = 2
LINE_SPACING = 1.15


def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(os.path.join(FONTS_DIR, f'Inter-{weight}.woff'), size)


def _diagonal_gradient(size, color1, color2) -> Image.Image:
    """A smooth top-left-to-bottom-right two-color gradient. Computed at a
    tiny resolution (a handful of pixels per axis -- cheap enough for a
    plain Python loop) and upscaled with Pillow's (C-accelerated) resize,
    which smooths it back out -- avoids a slow pure-Python loop over every
    pixel of the full-size canvas."""
    width, height = size
    small_w, small_h = 64, 36
    small = Image.new('RGB', (small_w, small_h))
    for y in range(small_h):
        for x in range(small_w):
            t = (x / (small_w - 1) + y / (small_h - 1)) / 2
            small.putpixel((x, y), tuple(round(color1[i] + (color2[i] - color1[i]) * t) for i in range(3)))
    return small.resize((width, height), Image.BICUBIC)


def _bottom_readability_gradient(size) -> Image.Image:
    """Transparent at the top, fading to ~80% black at the bottom -- the
    standard video-thumbnail treatment that keeps overlaid text readable
    against any background photo."""
    width, height = size
    gradient = Image.new('L', (1, height), 0)
    fade_start = int(height * 0.35)  # top 35% stays fully transparent
    for y in range(height):
        if y < fade_start:
            alpha = 0
        else:
            t = (y - fade_start) / (height - fade_start)
            alpha = int(205 * t)
        gradient.putpixel((0, y), alpha)
    alpha_mask = gradient.resize((width, height))
    overlay = Image.new('RGBA', size, (0, 0, 0, 255))
    overlay.putalpha(alpha_mask)
    return overlay


def _cover_crop(image: Image.Image, size) -> Image.Image:
    """Resize+center-crop to exactly `size`, same behavior as CSS
    object-fit: cover -- so an instructor's upload of any aspect ratio
    fills the 16:9 frame without distortion."""
    target_w, target_h = size
    src_w, src_h = image.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w, new_h = round(src_w * scale), round(src_h * scale)
    resized = image.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def _truncate_line(draw, text, font, max_width):
    if draw.textlength(text, font=font) <= max_width:
        return text
    truncated = text
    while truncated and draw.textlength(truncated + '…', font=font) > max_width:
        truncated = truncated[:-1].rstrip()
    return truncated + '…' if truncated else text


def _wrap_text(draw, text, font, max_width, max_lines):
    """Greedy word-wrap to at most max_lines, truncating the last line
    with an ellipsis if the title still doesn't fit."""
    words = text.split()
    lines = []
    current = ''
    for word in words:
        candidate = f'{current} {word}'.strip()
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)

    if len(lines) > max_lines:
        lines = lines[:max_lines]
    if len(' '.join(words)) > len(' '.join(lines)) and len(lines) == max_lines:
        # Force truncation even though the plain line already fits on its
        # own -- it needs to shrink further to make room for the ellipsis
        # that signals more text was cut off.
        last = lines[-1]
        while last and draw.textlength(last + '…', font=font) > max_width:
            last = last[:-1].rstrip()
        lines[-1] = last + '…'
    return lines


def build_poster_image(course) -> bytes:
    """Renders the 1280x720 poster and returns JPEG bytes -- nothing is
    written to disk here; the caller (Course.generate_poster) decides
    whether/where to persist it."""
    if course.thumbnail:
        course.thumbnail.open('rb')
        try:
            source = Image.open(course.thumbnail).convert('RGB')
        finally:
            course.thumbnail.close()
        background = _cover_crop(source, (WIDTH, HEIGHT))
    else:
        background = _diagonal_gradient((WIDTH, HEIGHT), BRAND_CYAN, BRAND_PURPLE)
        if os.path.exists(LOGO_PATH):
            logo = Image.open(LOGO_PATH).convert('RGBA')
            logo_h = 160
            logo_w = round(logo_h * logo.width / logo.height)
            logo = logo.resize((logo_w, logo_h), Image.LANCZOS)
            background.paste(
                logo, ((WIDTH - logo_w) // 2, (HEIGHT - logo_h) // 2 - 40), logo)

    canvas = background.convert('RGBA')
    canvas.alpha_composite(_bottom_readability_gradient((WIDTH, HEIGHT)))

    draw = ImageDraw.Draw(canvas)
    instructor_name = course.instructor.get_full_name() or course.instructor.username

    title_font = _font('ExtraBold', TITLE_SIZE)
    name_font = _font('SemiBold', NAME_SIZE)
    max_text_width = WIDTH - 2 * PADDING

    title_lines = _wrap_text(draw, course.title, title_font, max_text_width, TITLE_MAX_LINES)
    line_height = int(TITLE_SIZE * LINE_SPACING)
    title_block_height = line_height * len(title_lines)

    name_y = HEIGHT - PADDING - NAME_SIZE
    title_block_top = name_y - 16 - title_block_height

    y = title_block_top
    for line in title_lines:
        draw.text((PADDING, y), line, font=title_font, fill=WHITE)
        y += line_height

    instructor_name = _truncate_line(draw, instructor_name, name_font, max_text_width)
    draw.text((PADDING, name_y), instructor_name, font=name_font, fill=INSTRUCTOR_NAME_COLOR)

    buffer = io.BytesIO()
    canvas.convert('RGB').save(buffer, format='JPEG', quality=88)
    return buffer.getvalue()
