// Served at the site root (not /static/) so its default scope is "/" --
// a service worker's max scope is the directory it's served from, and
// nothing under /static/ would ever see requests for the rest of the site.
const CACHE_NAME = 'mendoura-shell-v1';
const SHELL_ASSETS = [
  '/static/img/logo.png',
  '/static/img/favicon-32.png',
  '/offline/',
];
// Bunny Stream video (embed + API) and Cloudinary uploads have their own
// cache semantics and can be large/streaming -- never intercept or cache
// them here, just let the browser handle those requests untouched.
const BYPASS_HOSTS = ['res.cloudinary.com', 'video.bunnycdn.com', 'iframe.mediadelivery.net', 'bunnycdn.com'];

function isBypassHost(hostname) {
  return BYPASS_HOSTS.some((host) => hostname === host || hostname.endsWith('.' + host));
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_ASSETS))
      .catch(() => {}) // offline-during-install shouldn't block activation
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (isBypassHost(url.hostname)) return;

  if (request.mode === 'navigate') {
    // Page loads always go to the network -- caching dynamic, per-user HTML
    // (enrollment state, dashboards, admin panels) would risk showing stale
    // or wrong-account content later. Only fall back to a static offline
    // page when there's truly no connection.
    event.respondWith(fetch(request).catch(() => caches.match('/offline/')));
    return;
  }

  if (url.origin === self.location.origin && url.pathname.startsWith('/static/')) {
    // Static assets are safe to cache aggressively -- collectstatic/WhiteNoise
    // versions them, so stale-while-revalidate is a safe, fast default.
    event.respondWith(
      caches.match(request).then((cached) => {
        const network = fetch(request)
          .then((response) => {
            caches.open(CACHE_NAME).then((cache) => cache.put(request, response.clone()));
            return response;
          })
          .catch(() => cached);
        return cached || network;
      })
    );
  }
});
