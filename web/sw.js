// Offline support: cache the app shell and serve it when the network is unavailable.
// The version lives in version.js; bumping it there rebuilds the cache.
importScripts('version.js');
const VERSION = `fnb48-${self.APP_VERSION}`;
const SHELL = [
  './',
  './index.html',
  './fnb48.js',
  './version.js',
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
// cache: 'no-cache' makes every fetch revalidate with the server. Without it, the browser's HTTP cache
// answers for as long as the server allows (GitHub Pages sends max-age=600), so a launch could run code
// up to 10 minutes stale, or mix files from two versions. Unchanged files come back as cheap 304s.
self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET' || new URL(e.request.url).origin !== location.origin) return;
  e.respondWith((async () => {
    const cache = await caches.open(VERSION);
    try {
      // A navigation Request can't be cloned with options, so build a fresh one from its URL. Keep its
      // redirect mode: a navigation must not be answered with an already-followed redirect.
      const req = new Request(e.request.url, {
        cache: 'no-cache', redirect: e.request.redirect, signal: AbortSignal.timeout(3000),
      });
      const res = await fetch(req);
      if (res.ok) cache.put(e.request, res.clone());
      return res;
    } catch {
      return (await cache.match(e.request, { ignoreSearch: true })) ?? Response.error();
    }
  })());
});
