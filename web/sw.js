// Offline support: cache the app shell and serve it when the network is unavailable.
// Bump VERSION when the list of files changes so old caches are dropped.
const VERSION = 'fnb48-v19';
const SHELL = [
  './',
  './index.html',
  './fnb48.js',
  './manifest.webmanifest',
  './icons/icon.svg',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/apple-touch-icon.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

// Network-first: always run the current code when the server is reachable, fall back to the cache
// when offline (or the network takes longer than 3 s).
self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET' || new URL(e.request.url).origin !== location.origin) return;
  e.respondWith((async () => {
    const cache = await caches.open(VERSION);
    try {
      const res = await fetch(e.request, { signal: AbortSignal.timeout(3000) });
      if (res.ok) cache.put(e.request, res.clone());
      return res;
    } catch {
      return (await cache.match(e.request, { ignoreSearch: true })) ?? Response.error();
    }
  })());
});
