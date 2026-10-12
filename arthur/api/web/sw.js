// El «service worker» de la web de ARTHUR (fase S6): el trocito que el navegador mantiene aunque cierres la web.
// Solo hace dos cosas: enseñar el aviso que llega (ya descifrado por el navegador) y, si lo tocas, abrir ARTHUR.
// No guarda nada ni pide nada al servidor. El texto se enseña como texto: una notificación no interpreta HTML.
"use strict";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { /* aviso sin formato: se enseña uno genérico */ }
  const title = typeof data.title === "string" && data.title ? data.title : "ARTHUR";
  const body = typeof data.body === "string" ? data.body : "Tienes un aviso.";
  // Siempre se enseña algo: el iPhone retira el permiso a las webs que reciben avisos y no los muestran.
  event.waitUntil(self.registration.showNotification(title, {
    body,
    icon: "/icon-180.png",
    badge: "/icon-180.png",
    tag: `arthur-${Date.now()}`,  // cada aviso el suyo: dos recordatorios seguidos no se pisan
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const open = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of open) {
      if (new URL(client.url).origin === self.location.origin && "focus" in client) return client.focus();
    }
    return self.clients.openWindow("/");
  })());
});
