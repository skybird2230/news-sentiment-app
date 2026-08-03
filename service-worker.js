// Minimal service worker -- mainly here so browsers treat this as an
// installable PWA. It does a simple network-first strategy; no offline
// caching of live data since signals need to be fresh.
self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  event.respondWith(
    fetch(event.request).catch(() => new Response(
      'Offline - please reconnect to view the latest signals.',
      { headers: { 'Content-Type': 'text/plain' } }
    ))
  );
});
