from django.db.models import Prefetch

from .models import Track


def tracks_menu(request):
    """Feeds the navbar's Tracks mega-menu on every page: top-level tracks with
    their active children prefetched, so the dropdown never issues a query per
    hover."""
    active_children = Track.objects.filter(is_active=True).order_by('order', 'name')
    parents = (
        Track.objects.filter(parent__isnull=True, is_active=True)
        .prefetch_related(Prefetch('children', queryset=active_children))
        .order_by('order', 'name')
    )
    return {'tracks_menu': parents}
