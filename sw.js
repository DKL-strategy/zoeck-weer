// Service worker: de app-schil (pagina, manifest, iconen) uit cache als het netwerk wegvalt;
// data.json/radar.json altijd eerst van het netwerk, met de laatst bekende versie als reserve.
const VERSION = "weer-v3";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./icon-192.png", "./icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  const isData = /\/(data|radar|radar-grid)\.json/.test(url.pathname);
  const isShell = url.origin === location.origin && !isData;
  if (isData || isShell) {
    // network-first: vers als het kan, cache als reserve
    e.respondWith(fetch(e.request).then(r => {
      const copy = r.clone(); caches.open(VERSION).then(c => c.put(isData ? url.pathname.replace(/\?.*$/, "") : e.request, copy)).catch(() => {});
      return r;
    }).catch(() => caches.match(isData ? url.pathname : e.request, { ignoreSearch: true }).then(m => m || (e.request.mode === "navigate" ? caches.match("./index.html") : Response.error()))));
  }
  // externe API's (Open-Meteo, Nominatim, GitHub Pages) gaan gewoon naar het netwerk
});
