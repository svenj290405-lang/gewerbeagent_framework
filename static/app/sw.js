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
// v19: Mitarbeiter-Aktivierung (Einladungs-Link) + Team-Aktivität (30 Tage)
// + App-verbunden-Anzeige korrekt, aufbauend auf v18.
// v20: Verbindungen im Einstellungen-Screen (Google/Microsoft OAuth +
// Lexware-API-Key direkt aus der App).
// v21: Anfrage-Formular-Editor (Felder hinzufügen/bearbeiten/sortieren,
// Typ + Pflicht + Optionen, Reset auf Standard) im Mehr-Hub.
// v22: echte PNG-Icons (192/512 + maskable + apple-touch) — saubere
// Home-Screen-Installation auf iOS/Android.
// v23: Kunden-Archiv-Upload (Foto/PDF/Notiz in Drive-Ordner) im Kunden-Profil.
// v24: „Zahlungen prüfen"-Button im Büro (Lexware-Bezahlstatus-Abgleich).
// v25: Q-Chat ist Startscreen; leerer Chat zeigt mittig den Netzwerk-Globus
// (Sphere wie auf der Website, Three.js via CDN), Intro-Text entfernt.
// v26: Visualisierung-Fenster aus dem Mehr-Hub entfernt; Q hat ein
// Funktions-Dropdown (Termin/Rückruf/Material/Wissen/Kunde/Angebot/Rechnung/
// Visualisierung) als Quick-Aktionen.
// v27: "Start" -> "Aktuelles" (Rückrufe + Beratungs-Leads annehmen/ablehnen +
// Auftrags-Pipeline mit 0-100%-Regler); Briefings nach "Termine"; 100% ->
// Rechnung in Q vorbereiten (editierbares Anschreiben) + senden.
// v28: Tabs "Anrufe" + "Anfragen" entfernt — in "Aktuelles" zusammengeführt
// (Offene Anfragen tappbar, Rückrufe mit Erledigt, Diktat + Rückruf-Buttons).
// v29: ⚡-Funktionsmenü startet jetzt den Flow direkt — Sphere animiert
// ("Q übernimmt") + Gemini fragt selbst nach, statt Seed-Text einzutippen.
// v30: (Cache-Bump)
// v31: Selbstheilung — App aktualisiert sich automatisch (controllerchange ->
// reload) + "App zurücksetzen"-Link auf der Login-Seite (/app/login?reset=1),
// damit niemand mehr auf einer alten, gecachten Version hängenbleibt.
// v32: Q-Sphere abgesichert — bei WebGL-Kontextverlust/Render-Fehler sauberer
// Fallback auf das SVG-Ring-Muster statt Tab-Absturz; neue Composer-Icons.
// v33: WebGL-Kontext-Leck behoben — Sphere gibt beim Abbau den GL-Kontext hart
// frei (forceContextLoss) + baut nicht doppelt auf; behebt „zerschossene"
// Darstellung nach mehreren Funktionsaufrufen/Tab-Wechseln.
// v34: Q-Globus komplett auf reine CSS-3D-Animation umgestellt — KEIN WebGL/
// Three.js/CDN mehr. Beendet alle GPU-/Kontext-Abstürze, läuft überall identisch
// und ohne externen Aufruf.
// v35: Q-Globus zurück auf den schönen WebGL-Netzwerk-Globus (Drahtgitter +
// Partikel + Energiebögen) wie früher — aber MIT den Stabilitäts-Fixes
// (forceContextLoss, Kontextverlust-/FPS-Fallback, kein Doppel-Mount) und
// Three.js LOKAL gehostet (/app/static/vendor, kein CDN). Der CSS-Globus sah
// „zerschossen" aus (sich kreuzende Großkreise statt sauberer Kugel).
// v36: Chat-first-Umbau — nur noch 3 Tabs (Assistent · Aktuelles · Mehr);
// Termine/Büro raus, ihre Anzeigen jetzt als Abschnitte in „Aktuelles";
// Q kann Ansichten per Chat öffnen ("zeig mir die Rechnungen"); schöner
// „Q"-Schriftzug unter dem Globus.
// v37: „Q"-Schriftzug unter dem Globus wieder entfernt (nur noch der Globus).
const CACHE = "ga-app-v37";
const SHELL = [
  "/app",
  "/app/static/app.css",
  "/app/static/app.js",
  "/app/static/icon.svg",
  "/app/static/icon-192.png",
  "/app/static/icon-512.png",
  "/app/static/icon-maskable-512.png",
  "/app/static/apple-touch-icon.png",
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
      icon: "/app/static/icon-192.png",
      badge: "/app/static/icon-192.png",
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
