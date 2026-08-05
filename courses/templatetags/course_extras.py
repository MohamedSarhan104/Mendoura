from django import template

register = template.Library()


@register.filter
def duration_display(seconds):
    """"4m 6s" from a lecture's duration_seconds. Returns '' for a falsy/zero
    value -- duration_seconds isn't populated by any upload path yet (no
    form field, no Bunny-metadata backfill), so most real lectures are 0;
    showing nothing reads better than a misleading "0m 0s"."""
    if not seconds:
        return ''
    seconds = int(seconds)
    minutes, secs = divmod(seconds, 60)
    if minutes and secs:
        return f'{minutes}m {secs}s'
    if minutes:
        return f'{minutes}m'
    return f'{secs}s'
