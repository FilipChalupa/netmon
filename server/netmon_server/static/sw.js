/* netmon service worker — offline-first app shell, last-known data fallback.

   Strategy: static shell is precached (cache-first); pages and API responses
   are network-first with the last successful response as fallback. Cached
   fallbacks carry an x-netmon-cached-at header so the page can show an
   "offline, data from HH:MM" banner. Registered as /sw.js?v=<version> —
   a version bump makes the browser install a fresh worker and drop old
   caches. Writes (POST/DELETE) always go to the network.
*/

const VERSION = new URL(self.location.href).searchParams.get('v') || 'dev';
const CACHE = 'netmon-' + VERSION;
const SHELL = [
  '/', '/status', '/help',
  '/static/app.js', '/static/chart.umd.js',
  '/static/manifest.webmanifest', '/static/icon-192.png', '/static/icon-512.png',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(SHELL).catch(() => null))  // partial precache is fine
      .then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim()));
});

async function store(cache, req, res) {
  const headers = new Headers(res.headers);
  headers.set('x-netmon-cached-at', String(Date.now()));
  const body = await res.blob();
  await cache.put(req, new Response(body, {
    status: res.status, statusText: res.statusText, headers,
  }));
}

async function networkFirst(req) {
  const cache = await caches.open(CACHE);
  try {
    const res = await fetch(req);
    if (res.ok) await store(cache, req, res.clone());
    return res;
  } catch (err) {
    const hit = await cache.match(req);
    if (hit) return hit;
    throw err;
  }
}

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/mcp')) return;   // live protocol, never cached
  // network-first even for /static/: a cache-first shell would pin stale JS
  // across same-version deploys; on a LAN the latency cost is negligible
  e.respondWith(networkFirst(e.request));
});
