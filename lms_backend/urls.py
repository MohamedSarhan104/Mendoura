from django.contrib import admin
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from courses import views as courses_views


# TEMPORARY -- remove when off the free Render tier. No DB hit, just proves
# the process is alive, so an external cron (cron-job.org / UptimeRobot)
# pinging this every 10-14 min keeps the free instance from sleeping.
def healthz(request):
    return HttpResponse('ok')


# PWA manifest + service worker. Both are rendered as templates (not plain
# static files) so the manifest can build correct absolute icon URLs via
# {% static %} and the service worker can be served from the site root --
# a service worker's scope is capped at the directory it's served from, and
# under /static/ it could never control pages outside that prefix.
def manifest_json(request):
    return render(request, 'manifest.json', content_type='application/manifest+json')


def service_worker(request):
    return render(request, 'service-worker.js', content_type='text/javascript')


def offline(request):
    return render(request, 'offline.html', content_type='text/html')


# Google Play Digital Asset Links -- verifies the Trusted Web Activity
# (com.mendoura.twa) is allowed to open Mendoura's own links full-screen,
# without the browser URL bar. Must be served at exactly this well-known
# path with no redirects, hence its own view rather than a static file.
ASSETLINKS = [{
    'relation': ['delegate_permission/common.handle_all_urls'],
    'target': {
        'namespace': 'android_app',
        'package_name': 'com.mendoura.twa',
        'sha256_cert_fingerprints': [
            '9B:68:56:66:B6:4B:E9:88:71:AE:52:89:C8:B3:28:BF:FA:42:9F:95:3E:CA:B9:70:36:BE:29:8D:79:D9:7A:75',
        ],
    },
}]


def assetlinks_json(request):
    return JsonResponse(ASSETLINKS, safe=False)


urlpatterns = [
    path('admin/', admin.site.urls),
    path('healthz/', healthz, name='healthz'),
    path('manifest.json', manifest_json, name='manifest_json'),
    path('service-worker.js', service_worker, name='service_worker'),
    path('offline/', offline, name='offline'),
    path('.well-known/assetlinks.json', assetlinks_json, name='assetlinks_json'),
    path('webhooks/paymob/', courses_views.paymob_webhook, name='paymob_webhook'),
    path('webhooks/bunny/', courses_views.bunny_webhook, name='bunny_webhook'),
    path('i18n/', include('django.conf.urls.i18n')),  # provides the 'set_language' POST endpoint
    path('', include('courses.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
