from django import template

register = template.Library()


@register.filter
def duration_display(seconds):
    """Human-friendly duration -- "1h 4m", "4m 6s", "45s" -- from a count of
    seconds: a single lecture's duration_seconds, or a course's total across
    every lecture. Returns '' for a falsy/zero value -- duration_seconds is
    only backfilled from Bunny lazily (see _sync_bunny_status in views.py),
    so an older or freshly-uploaded lecture can still legitimately be 0;
    showing nothing reads better than a misleading "0m 0s"."""
    if not seconds:
        return ''
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f'{hours}h {minutes}m' if minutes else f'{hours}h'
    if minutes and secs:
        return f'{minutes}m {secs}s'
    if minutes:
        return f'{minutes}m'
    return f'{secs}s'
