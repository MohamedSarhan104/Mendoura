"""Bunny Stream integration.

Two secrets, two jobs:
  * BUNNY_API_KEY signs *uploads* -- it creates the video record and produces
    the short-lived signature the browser uses to push bytes straight to Bunny.
    It never reaches the client.
  * BUNNY_TOKEN_KEY signs *playback* -- every embed URL carries an expiring
    token so a copied link stops working and can't be reshared.

The one HTTP call (create_video) is isolated so tests can mock it; everything
else is pure hashing with no network.
"""
import hashlib
import logging
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

VIDEO_API_BASE = 'https://video.bunnycdn.com'
EMBED_BASE = 'https://iframe.mediadelivery.net/embed'
TUS_ENDPOINT = 'https://video.bunnycdn.com/tusupload'

# TEMPORARY: diagnosing "Could not start the upload" in production (the
# create_bunny_video view's generic error message on any BunnyError/
# RequestException, previously logged nowhere). Every line tagged
# [BUNNY_UPLOAD_DEBUG] so it's easy to grep out of Render's logs. Remove
# this whole block (and the logging calls in create_video below) once the
# root cause is confirmed and fixed -- it's noisy by design, not something
# to ship long-term. Never logs BUNNY_API_KEY itself.
_DEBUG_TAG = '[BUNNY_UPLOAD_DEBUG]'

# TEMPORARY: diagnosing lectures stuck showing "still processing" long after
# Bunny actually finished encoding. bunny_status is normally updated by
# Bunny's webhook (bunny_webhook in views.py), but webhook delivery isn't
# guaranteed -- a Render free-tier dyno asleep at delivery time, a dropped
# request, etc. -- and nothing ever retried it, so a lecture could stay
# stuck forever on whatever status the webhook last (or never) delivered.
# Tagged [BUNNY_STATUS_DEBUG] so it's easy to grep out of Render's logs.
_STATUS_DEBUG_TAG = '[BUNNY_STATUS_DEBUG]'


class BunnyError(Exception):
    pass


def is_configured() -> bool:
    return bool(settings.BUNNY_LIBRARY_ID and settings.BUNNY_API_KEY)


def create_video(title: str) -> str:
    """Create an empty video in the library and return its GUID. The browser
    then uploads the actual bytes to this GUID with a signed TUS request."""
    if not is_configured():
        logger.warning(
            '%s create_video called while not configured -- BUNNY_LIBRARY_ID set=%s BUNNY_API_KEY set=%s',
            _DEBUG_TAG, bool(settings.BUNNY_LIBRARY_ID), bool(settings.BUNNY_API_KEY))
        raise BunnyError('Bunny Stream is not configured.')

    url = f'{VIDEO_API_BASE}/library/{settings.BUNNY_LIBRARY_ID}/videos'
    logger.info(
        '%s calling Bunny create-video API: url=%s library_id=%s title=%r',
        _DEBUG_TAG, url, settings.BUNNY_LIBRARY_ID, title[:255] or 'Untitled')
    started = time.monotonic()
    try:
        response = requests.post(
            url,
            json={'title': title[:255] or 'Untitled'},
            headers={
                'AccessKey': settings.BUNNY_API_KEY,
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        elapsed = time.monotonic() - started
        logger.exception(
            '%s Bunny create-video request FAILED after %.2fs: url=%s library_id=%s '
            'exception_type=%s exception_message=%s',
            _DEBUG_TAG, elapsed, url, settings.BUNNY_LIBRARY_ID, type(exc).__name__, str(exc))
        raise

    elapsed = time.monotonic() - started
    if not response.ok:
        logger.error(
            '%s Bunny create-video API returned an error after %.2fs: url=%s library_id=%s '
            'status=%s body=%s',
            _DEBUG_TAG, elapsed, url, settings.BUNNY_LIBRARY_ID, response.status_code,
            response.text[:2000])
        response.raise_for_status()

    guid = response.json().get('guid')
    if not guid:
        logger.error(
            '%s Bunny create-video API returned 2xx with no guid after %.2fs: url=%s '
            'status=%s body=%s',
            _DEBUG_TAG, elapsed, url, response.status_code, response.text[:2000])
        raise BunnyError('Bunny did not return a video GUID.')

    logger.info(
        '%s Bunny create-video SUCCEEDED after %.2fs: library_id=%s guid=%s',
        _DEBUG_TAG, elapsed, settings.BUNNY_LIBRARY_ID, guid)
    return guid


def get_video_info(video_id: str) -> dict:
    """Ask Bunny directly for this video's current encoding status (the same
    integer bunny_webhook receives) and its encoded duration in seconds
    ('length' -- 0 until Bunny finishes processing). Used as a fallback where
    the webhook delivery might have been missed, so the instructor-facing
    status can't get permanently stuck, and to backfill
    Lecture.duration_seconds once Bunny actually knows the video's length."""
    url = f'{VIDEO_API_BASE}/library/{settings.BUNNY_LIBRARY_ID}/videos/{video_id}'
    logger.info(
        '%s calling Bunny get-video-status API: url=%s library_id=%s video_id=%s',
        _STATUS_DEBUG_TAG, url, settings.BUNNY_LIBRARY_ID, video_id)
    started = time.monotonic()
    try:
        response = requests.get(
            url,
            headers={'AccessKey': settings.BUNNY_API_KEY, 'Accept': 'application/json'},
            timeout=15,
        )
    except requests.RequestException as exc:
        elapsed = time.monotonic() - started
        logger.exception(
            '%s Bunny get-video-status request FAILED after %.2fs: url=%s video_id=%s '
            'exception_type=%s exception_message=%s',
            _STATUS_DEBUG_TAG, elapsed, url, video_id, type(exc).__name__, str(exc))
        raise

    elapsed = time.monotonic() - started
    if not response.ok:
        logger.error(
            '%s Bunny get-video-status API returned an error after %.2fs: url=%s video_id=%s '
            'status=%s body=%s',
            _STATUS_DEBUG_TAG, elapsed, url, video_id, response.status_code, response.text[:2000])
        response.raise_for_status()

    body = response.json()
    status = body.get('status')
    if status is None:
        logger.error(
            '%s Bunny get-video-status API returned 2xx with no status field after %.2fs: '
            'url=%s video_id=%s body=%s',
            _STATUS_DEBUG_TAG, elapsed, url, video_id, response.text[:2000])
        raise BunnyError('Bunny did not return a video status.')

    length = body.get('length') or 0
    logger.info(
        '%s Bunny get-video-status SUCCEEDED after %.2fs: video_id=%s status=%s length=%s',
        _STATUS_DEBUG_TAG, elapsed, video_id, status, length)
    return {'status': int(status), 'length': int(length)}


def get_video_status(video_id: str) -> int:
    """Back-compat wrapper around get_video_info() for callers that only need
    the status integer."""
    return get_video_info(video_id)['status']


def _upload_signature(video_id: str, expiration: int) -> str:
    # Bunny's presigned-upload scheme: sha256(libraryId + apiKey + expiration + videoId).
    raw = f'{settings.BUNNY_LIBRARY_ID}{settings.BUNNY_API_KEY}{expiration}{video_id}'
    return hashlib.sha256(raw.encode()).hexdigest()


def upload_credentials(video_id: str) -> dict:
    """Everything the browser's TUS client needs to upload directly to Bunny,
    scoped to this one video and expiring shortly. Deliberately excludes the
    raw API key."""
    expiration = int(time.time()) + 60 * 60  # 1 hour to complete the upload
    return {
        'endpoint': TUS_ENDPOINT,
        'library_id': str(settings.BUNNY_LIBRARY_ID),
        'video_id': video_id,
        'expiration': expiration,
        'signature': _upload_signature(video_id, expiration),
    }


def embed_url(video_id: str) -> str:
    """The player iframe src. When a token key is configured the URL is signed
    and time-limited; otherwise it degrades to the plain embed (still gated by
    Bunny's referrer allow-list and our own access control on the page)."""
    base = f'{EMBED_BASE}/{settings.BUNNY_LIBRARY_ID}/{video_id}'
    if not settings.BUNNY_TOKEN_KEY:
        return base
    expiration = int(time.time()) + settings.BUNNY_EMBED_TOKEN_TTL
    token = hashlib.sha256(
        f'{settings.BUNNY_TOKEN_KEY}{video_id}{expiration}'.encode()).hexdigest()
    return f'{base}?token={token}&expires={expiration}'
