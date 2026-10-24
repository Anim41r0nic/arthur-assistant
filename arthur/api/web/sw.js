// El «service worker» de la web de ARTHUR (fase S6): el trocito que el navegador mantiene aunque cierres la web.
// Enseña el aviso y, si lo tocas, abre ARTHUR, el panel, la ficha de un cliente o un vídeo de Studio cuando el núcleo
// indicó ese destino.
// No guarda nada ni pide nada al servidor. El texto se enseña como texto: una notificación no interpreta HTML.
"use strict";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

// Las únicas «partes tras #» permitidas son las que pone el núcleo: la ficha de un cliente (#c12) en los avisos de
// contactos, un vídeo de Studio en su canal (#v12-frutinovelas) y el Repaso de Apuntes (#repaso). Los perfiles, los de
// push/destination.py.
const CLIENT_HASH = /^#(?:c[1-9][0-9]{0,8}|v[1-9][0-9]{0,8}-(?:estoicismo_es|estoicismo_en|frutinovelas)|repaso)$/;

function safeDestination(value) {
  if (typeof value !== "string" || value.length > 512 || value.trim() !== value) return null;
  try {
    const url = new URL(value);
    if (url.username || url.password || url.search || url.pathname !== "/") return null;
    if (url.hash && !CLIENT_HASH.test(url.hash)) return null;
    // «…#» suelto: new URL deja hash vacío pero lo conserva en href; no es una ficha ni un vídeo, se rechaza.
    if (!url.hash && value.includes("#")) return null;
    if (url.protocol === "https:") return url.href;
    if (url.protocol !== "http:") return null;
    const ip = url.hostname.split(".").map(Number);
    const tailscaleIp = ip.length === 4 && ip.every(n => Number.isInteger(n) && n >= 0 && n <= 255)
      && ip[0] === 100 && ip[1] >= 64 && ip[1] <= 127;
    return tailscaleIp || url.hostname.endsWith(".ts.net") ? url.href : null;
  } catch { return null; }
}

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { /* aviso sin formato: se enseña uno genérico */ }
  const title = typeof data.title === "string" && data.title ? data.title : "ARTHUR";
  const body = typeof data.body === "string" ? data.body : "Tienes un aviso.";
  const destination = safeDestination(data.url);
  // Siempre se enseña algo: el iPhone retira el permiso a las webs que reciben avisos y no los muestran.
  event.waitUntil(self.registration.showNotification(title, {
    body,
    icon: "/icon-180.png",
    badge: "/icon-180.png",
    tag: `arthur-${Date.now()}`,  // cada aviso el suyo: dos recordatorios seguidos no se pisan
    data: { url: destination },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const destination = safeDestination(event.notification.data && event.notification.data.url);
    if (destination) return self.clients.openWindow(destination);
    const open = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of open) {
      if (new URL(client.url).origin === self.location.origin && "focus" in client) return client.focus();
    }
    return self.clients.openWindow("/");
  })());
});
