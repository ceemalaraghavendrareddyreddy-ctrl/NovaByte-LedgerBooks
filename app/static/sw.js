// LedgerBooks service worker.
//
// Deliberately conservative for a financial app: this caches only the static
// APP SHELL (CSS, icons) — never HTML pages with account balances, invoices,
// or any live figure. Caching those and serving them offline would mean
// silently showing possibly-wrong money numbers with no indication they're
// stale, which is a real risk in accounting software, not a UX nicety to
// skip. Page navigations always go to the network; offline only ever shows
// an explicit "you're offline" page, never cached financial data.
const CACHE_NAME = "ledgerbooks-shell-v1";
const SHELL_ASSETS = [
  "/static/css/style.css",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/offline.html",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Page navigations: always try the network first (never serve a cached
  // page with figures that might be stale). Only fall back to the explicit
  // offline page when the network genuinely fails.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).catch(() => caches.match("/static/offline.html"))
    );
    return;
  }

  // Static shell assets only: cache-first for speed, with a network fallback
  // that also updates the cache.
  if (url.origin === self.location.origin && SHELL_ASSETS.some((p) => url.pathname === p)) {
    event.respondWith(
      caches.match(req).then((cached) => {
        const fetchPromise = fetch(req).then((res) => {
          caches.open(CACHE_NAME).then((cache) => cache.put(req, res.clone()));
          return res;
        });
        return cached || fetchPromise;
      })
    );
    return;
  }

  // Everything else (API calls, uploads, third-party fonts/CDN scripts):
  // pass straight through to the network, no caching.
});
