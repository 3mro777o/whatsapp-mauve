/* WhatsApp Archive service worker: caches only the app shell (page, fonts, icons).
   Data (/data), media (/Media, /thumbs, /user) and the API are never cached.
   Never cached either: the login page (X-WA-Login), redirects and errors (no PIN session -> 302/401). */
const SHELL = "wa-shell-v4";
const PRECACHE = ["/", "/manifest.webmanifest", "/icons/icon-192.png", "/icons/icon-512.png"];
const cacheable = r => r.ok && !r.redirected && !r.headers.get("X-WA-Login");
self.addEventListener("install", e => {
  // redirect:"manual": a logged-out fetch (302 to /login) fails here instead of being stored under "/"
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(PRECACHE.map(u => new Request(u, { redirect: "manual" })))).catch(() => {}).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  const p = url.pathname;
  if (req.mode === "navigate" || p === "/" || p === "/index.html") {
    // network-first so updates to the viewer arrive immediately; cached shell only when offline
    e.respondWith(fetch(req).then(r => {
      if (cacheable(r)) { const cp = r.clone(); caches.open(SHELL).then(c => c.put("/", cp)); }
      return r;
    }).catch(() => caches.match("/")));
    return;
  }
  if (p.startsWith("/fonts/") || p.startsWith("/icons/") || p === "/manifest.webmanifest") {
    e.respondWith(caches.match(req).then(hit => hit || fetch(req).then(r => {
      if (cacheable(r)) { const cp = r.clone(); caches.open(SHELL).then(c => c.put(req, cp)); }
      return r;
    })));
  }
  // everything else (data, media, api): straight to the network, no caching
});
