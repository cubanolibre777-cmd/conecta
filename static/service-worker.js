// Service worker mínimo: solo lo necesario para que Chrome considere la web
// instalable como app. No cachea nada todavía (eso se puede añadir después
// para que funcione sin conexión).

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  // Por ahora, deja pasar todas las peticiones directo a la red.
  event.respondWith(fetch(event.request));
});
