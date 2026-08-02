from django.db.models import Prefetch

from .models import Track, User


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


def pending_instructor_requests(request):
    """Feeds the red badge on the Admin Panel nav link with the count of
    instructor signups still awaiting approve/reject -- only queried for
    logged-in superusers, since nobody else can see or act on the Admin
    Panel anyway, so it'd just be a wasted query on every other page view."""
    if not (request.user.is_authenticated and request.user.is_superuser):
        return {}
    count = User.objects.filter(is_instructor=True, is_approved=False).count()
    return {'pending_instructor_requests_count': count}
