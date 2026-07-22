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
import time

import requests
from django.conf import settings

VIDEO_API_BASE = 'https://video.bunnycdn.com'
EMBED_BASE = 'https://iframe.mediadelivery.net/embed'
TUS_ENDPOINT = 'https://video.bunnycdn.com/tusupload'


class BunnyError(Exception):
    pass


def is_configured() -> bool:
    return bool(settings.BUNNY_LIBRARY_ID and settings.BUNNY_API_KEY)


def create_video(title: str) -> str:
    """Create an empty video in the library and return its GUID. The browser
    then uploads the actual bytes to this GUID with a signed TUS request."""
    if not is_configured():
        raise BunnyError('Bunny Stream is not configured.')
    response = requests.post(
        f'{VIDEO_API_BASE}/library/{settings.BUNNY_LIBRARY_ID}/videos',
        json={'title': title[:255] or 'Untitled'},
        headers={
            'AccessKey': settings.BUNNY_API_KEY,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        },
        timeout=15,
    )
    response.raise_for_status()
    guid = response.json().get('guid')
    if not guid:
        raise BunnyError('Bunny did not return a video GUID.')
    return guid


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
