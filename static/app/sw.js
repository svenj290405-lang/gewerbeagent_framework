/* Gewerbeagent PWA Service-Worker.
 *
 * Aufgaben:
 *  - Push-Events anzeigen (Payload ist bewusst inhaltslos/minimal — keine
 *    Endkunden-PII; Details laedt die App nach Login vom EU-Server).
 *  - Klick auf die Benachrichtigung -> App oeffnen/fokussieren auf die
 *    mitgelieferte URL.
 *  - Minimaler Offline-Shell-Cache (App-Rahmen laedt auch ohne Netz; die
 *    eigentlichen Daten kommen immer frisch vom Server).
 */
// v9: Belege erfassen (Foto/PDF → Lexware-Voucher-Upload), zusaetzlich zum
// Sprach-Diktat (v8).
const CACHE = "ga-app-v9";
const SHELL = [
  "/app",
  "/app/static/app.css",
  "/app/static/app.js",
  "/app/static/icon.svg",
  "/app/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  // API/Login nie aus dem Cache; nur GETs behandeln.
  if (req.method !== "GET" || req.url.includes("/app/api/") || req.url.includes("/app/login")) {
    return;
  }
  // Network-first: immer die frische Version holen (verhindert, dass ein
  // alter Cache kaputtes JS/HTML festhaelt), Cache nur als Offline-Fallback.
  event.respondWith(
    fetch(req)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(req))
  );
});

self.addEventListener("push", (event) => {
  let data = { title: "Gewerbeagent", body: "Neue Benachrichtigung", url: "/app" };
  try {
    if (event.data) data = Object.assign(data, event.data.json());
  } catch (e) { /* inhaltsloser Push -> Defaults */ }
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: "/app/static/icon.svg",
      badge: "/app/static/icon.svg",
      tag: data.tag || "ga",
      data: { url: data.url || "/app" },
      requireInteraction: false,
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/app";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if (client.url.includes("/app") && "focus" in client) {
          client.navigate(url);
          return client.focus();
        }
      }
      return self.clients.openWindow(url);
    })
  );
});
