// Service worker mínimo: solo necesario para que el navegador ofrezca "Instalar app".
// No cachea nada (esta web necesita datos en vivo), así que siempre va a la red.
self.addEventListener("install", e => self.skipWaiting());
self.addEventListener("activate", e => self.clients.claim());
self.addEventListener("fetch", () => {});