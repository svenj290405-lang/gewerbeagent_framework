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
// v38: Status-Screen (Verbindungen + Cron-Heartbeats + Mail-Pipeline +
// Werkstatt), Test-Buttons in Verbindungen (Microsoft-Test-Mail, Kalender-/
// Drive-/Lexware-Ping), Hilfe & Tour mit Feature-Übersicht.
// v39: Einrichtungs-Tour für neue Nutzer (Overlay) — erklärt die drei
// Bereiche + was Q erledigt und führt den Inhaber durch Google/Outlook/
// Lexware verbinden; startet automatisch beim ersten Start (Server-Flag
// onboarding_done), erneut über „Mehr → Einrichtung starten".
// v44: Erster Druck auf den Globus holt einmalig die Browser-Mikrofon-
// Freigabe ein (Berechtigungs-Dialog), bevor die Halte-Aufnahme startet.
// v45: Q behält den Gesprächsverlauf (Mehrfach-Rückfragen) — kein erneutes
// Nachfragen nach schon Gesagtem mehr.
// v46: Q-geführtes Onboarding im Chat (ersetzt das Overlay-Tutorial) — Q
// erklärt sich und führt mit klickbaren Verbinden-Karten durch Google/
// Outlook/Lexware/Push.
// v47: Q kann mehr — Termin verschieben + Drive-Ordner/Notiz anlegen
// (neue Assistent-Tools + Schnellaktionen).
// v48: Drive-Links im Chat sind jetzt echte klickbare Hyperlinks (linkify +
// „in Drive öffnen ↗"); URLs in Q-Antworten generell anklickbar.
// v49: Dashboard-Umbau — Tab „Aktuelles" → „Aktionen", Kachel-Grid mit
// Handlungs-Badges (offene Rechnungen/Rückrufe nach oben sortiert) + Anrufe-
// Kachel, ausklappbares Q-Briefing, aufgeräumter „Mehr"-Screen
// (Schnellzugriff + Einstellungen), Globus-Aufnahme-Animation (Spin + Hint).
// v50: 📎-Upload im Q-Chat fragt bei Bildern nach: 🎨 Visualisieren (Foto +
// Beschreibung → Gemini-Rendering direkt im Chat) oder 📄 als Beleg ablegen.
// PDFs gehen wie bisher direkt in den Beleg-Upload.
// v51: 📎-Bild-Upload zeigt jetzt eine Vorschau + fragt mit allen
// freigeschalteten Optionen (🎨 Visualisieren / 📄 Beleg / 📁 Im Kunden-Archiv
// speichern). Archiv fragt nach dem Kundennamen.
// v52: Bild-Upload ohne Buttons — Q entscheidet per Chat. Bild anhängen + (optional)
// Anweisung tippen → Q routet selbst (visualisieren / im Kundenarchiv ablegen /
// als Beleg) oder fragt nach, was damit passieren soll. Bild bleibt über die
// Rückfrage angehängt. Statt Frontend-Stichwörter entscheidet Gemini.
// v53: Fix — „mach daraus ..."-Wünsche werden zuverlässig als Visualisierung
// erkannt (klarerer Tool-/System-Prompt, Gemini lehnt nicht mehr fälschlich
// als „kann keine Bilder bearbeiten" ab); aktueller Text steht nicht mehr
// doppelt im Kontext der Rückfrage-Runde.
// v54: Fix — angehängtes Bild „klebt" jetzt über Tab-Wechsel hinweg (vorher
// nullte navigate() qPendingFile → Folge-Befehl nach der Rückfrage landete im
// Text-Pfad und Gemini lehnte mangels Bild/Tool ab). Vorschau wird beim
// Wiederbetreten des Assistenten wiederhergestellt; Bild-Router macht 1 Retry
// bei 429 (Vertex-Kontingent-Burst).
// v55: Beta-Design ist jetzt das Standard-Design auf /app — Shell laedt
// app-beta.css + beta.js statt app.css (index.html entsprechend umgestellt;
// /app/beta bleibt als identische Vorschau bestehen).
// v56: Assistent ist Home/Wurzel (Zurueck-Stack wird dort geleert);
// Kopfzeile zeigt nur noch das Logo, die Website-URL liegt unsichtbar
// dahinter (Domain-Text nur noch als Fallback ohne Logo).
// v57: Q-Overlay gleitet hinter der Tabbar hervor statt vor den Reitern
// hoch; Drag-Handle durch Kopfzeile mit X ersetzt; Q-Tab funktioniert
// jetzt auch auf Sub-Ansichten wie dem Kundenprofil (DOM-Check statt
// App.current).
// v58: Kundenprofil: "Zum Archiv hinzufügen" ist keine eigene Karte mehr,
// sondern ein Dialog hinter dem + im Kopf der Ablage-Karte.
// v59: Ablage-Karte zeigt die drei Kacheln (Bilder/PDFs/Notizen) auch ohne
// Drive-Ordner einheitlich mit 0 statt eines Hinweistexts.
// v60: Einstellungen > Verbindungen: Dienste gestapelt (Titel, Status,
// Buttons untereinander) — vorher quetschten die Buttons auf Handy-Breite
// den Titel auf Null und die Buchstaben brachen einzeln um.
// v61: Kopfzeile bleibt gleich hoch, wenn Zurueck-Pfeil/Glocke erscheinen
// (Buttons ragen ins Header-Padding statt die Zeile zu strecken).
// v62: Fix zu v61 — hidden-Attribut der Header-Buttons griff nicht mehr
// (display:inline-flex schlug es), der Pfeil stand dauerhaft da.
// v63: Security — esc() maskiert jetzt auch " und ' (Attribut-Kontext-XSS
// ueber Fremddaten wie Kundennamen geschlossen).
// v64: Q kann E-Mails schreiben — Entwurfs-Karte im Chat (Empfaenger,
// Betreff, Text, Anhaenge) mit Freigabe vor dem Versand.
// v65: Auftraege ueberarbeitet — Fortschritts-Regler zurueck am Schritt
// "Arbeit laeuft", Auftrag antippbar mit voller Detailansicht + abhakbarer
// Fortschrittszeile, Liste "Abgeschlossene Auftraege" (Drive-Archiv je
// Auftrag) und Prozess-Editor als Aktivitaetsdiagramm mit Drag & Drop.
// v66: Auftraege lassen sich von Hand anlegen (Kunde + Positionen +
// Startschritt) — fuer Arbeit, die nie durch die Angebots-Pipeline lief.
// v67: Aufnahmen sind zum Kundengespraech geworden — Kundendaten oben,
// Diktat/Notiz/Foto/Visualisierung an einem Ort, am Ende die Kundenmail.
// v68: Kundengespraech hat ein Ende — „fertig, beim Kunden einpflegen"
// (legt den Kunden an, Protokoll + Bilder in den Drive-Kundenordner) oder
// verwerfen; oben in der Liste stehen die geplanten Termine aus dem Kalender.
// v69: Arbeitsstunden am Fortschrittsregler — jeder bucht seine Stunden auf
// den Auftrag, am Auftrag steht danach, wer wie lange dran war
// (Nachkalkulation, keine Anwesenheitserfassung).
// v70: Auftragshistorie — alle fertiggestellten Auftraege (abgerechnete UND
// abgebrochene) an einem Ort; abgebrochene sind dafuer aus der laufenden
// Liste raus, dort steht nur noch, woran wirklich gearbeitet wird.
// v71: Einstellungen → Automatisierung — pro Funktion einstellbar, ob Q
// manuell (gar nicht), assistiert (fragt vorher) oder automatisch (macht
// direkt) handelt. Gilt fuer den Q-Chat sowie Telefon und Mail-Eingang.
// v72: Sprechen bei Q wie bei WhatsApp — ein Tipp auf Globus/Mikro startet die
// Aufnahme und laesst sie laufen (Leiste mit Laufzeit + Live-Pegel, verwerfen
// oder senden per Knopf); Gedrueckthalten sendet weiterhin beim Loslassen.
// v73: Q-Overlay oeffnet halb hoch statt als schmaler Streifen und hat
// dieselbe Sprachaufnahme wie der Assistent (Leiste mit Laufzeit + Pegel);
// automatisch ausgefuehrte Aktionen werden im Overlay auch angezeigt.
// v74: Automatisierungs-Stufen werden erst mit „Speichern" wirksam
// (Speicherleiste, Verwerfen, Warnung beim Verlassen) statt sofort beim Tippen.
// v75: EIN Buchhaltungs-Bereich statt der getrennten Kacheln „Angebote" und
// „Rechnungen" — mit Kennzahlen (offen/ueberfaellig/bezahlt), offenen Posten,
// Angeboten zum Nachfassen, den Belegen (waren vorher praktisch unsichtbar)
// und Lexware-Deeplinks auf jeder Zeile.
// v76: Ausgaben-Abschnitt (Eingangsrechnungen aus Lexware — die Seite, die
// bisher komplett fehlte) und das Zahlungsziel kommt aus Lexware statt aus
// einer Schaetzung; ist keins hinterlegt, sagt die Anzeige das auch.
// v77: Beleg-Foto wird gelesen — Q schlaegt Haendler, Datum, Betrag,
// Steuersatz und Buchungskategorie vor (Kategorien aus dem echten
// Lexware-Konto), alles korrigierbar, gebucht wird erst auf Tipp.
// v78: Zahlungserinnerung + Angebot nachfassen direkt aus der Buchhaltung —
// Q schreibt den Entwurf (drei Tonfaelle), alles aenderbar, senden auf Tipp.
// v79: Objekt erkennen — Foto vom Geraet/Typenschild/Bauteil, Q bestimmt das
// Modell, schlaegt in der Hersteller-Doku nach (Google-Suche als Werkzeug) und
// antwortet mit Quellen; im Kauf-Modus mit Bezugsquellen + "als Material merken".
// v82: Zustellbarkeit — Antworten tragen jetzt In-Reply-To/References und
// haengen beim Kunden IM Thread statt lose daneben; der Mail-Footer zeigt
// die Adresse des Postfachs, aus dem wirklich gesendet wird (vorher konnte
// dort eine abweichende Adresse stehen: Spam-Merkmal, und eine Antwort
// dorthin sah der Inbox-Poller nie). Einstellungen > Verbindungen warnt,
// wenn ein privates Freemail-Postfach verbunden ist.
// (v80/v81 waren die Objekt-Suche — die bleibt vorerst draussen, siehe
// git stash "objekt-suche WIP 2026-08-13"; darum springt die Nummer.)
// v83: „Aktionen" ohne Schnellstart-Knoepfe im Kopf — „🎤 Gespräch" und
// „+ Rückruf" waren doppelt gemoppelt (beides steht in der jeweiligen
// Kachel, dort mit der vollen Liste dahinter).
// v84: Anfrage-Formular als eigener Arbeitsbereich — eigene Kachel in
// „Aktionen", oben die echte Kundenansicht als Live-Vorschau (serverseitig
// mit derselben Funktion gerendert, die der Kunde bekommt), darunter eine
// Q-Zeile: sagen, was anders sein soll, Vorschlag in der Vorschau sehen,
// übernehmen oder verwerfen. Der Eintrag unter „Mehr" ist dafür raus.
// v85: Fix zu v84 — im Formular-Screen riefen „Feld hinzufügen", „Löschen"
// und jede Eingabe eine Hilfsfunktion auf, die sich selbst aufrief
// (Endlosschleife). Die Liste baute sich danach nicht neu auf, und
// ungespeicherte Änderungen wurden nicht als solche vermerkt.
// v86: Der Formular-Screen oeffnet jetzt den Typ, den die Kunden wirklich
// bekommen (Branche entscheidet, „auto") statt immer „Allgemein" — ein
// Tischlerbetrieb bearbeitete sonst ein Formular, das nie rausgeht. Der
// scharfe Typ ist mit ✓ markiert, beim anderen steht es dabei.
// v87: Der Vorschau-Link im Formular-Screen war praktisch unlesbar — er lag
// in einem schmalen readonly-Feld mit `var(--bg2,#f8f8f8)` als Hintergrund,
// und --bg2 gibt es im Stylesheet gar nicht: im Dunkelmodus helles Grau mit
// fast weisser Schrift. Jetzt umbrechender Monospace-Text auf --bg mit
// --text, dazu „Kopieren" und „Öffnen ↗" nebeneinander.
// v88: Vorschlags-Chips („Termin eintragen" usw.) unter der Q-Kugel entfernt —
// redundant zum ✨-Menue am Eingabefeld.
// v89: Q-Overlay kann Aktionen jetzt selbst bestätigen (Ausführen/Abbrechen)
// und Mail-Entwürfe direkt redigieren + senden — vorher nur Verweis-Link
// „Im Assistenten bestätigen".
const CACHE = "ga-app-v89";
const SHELL = [
  "/app",
  "/app/static/app-beta.css",
  "/app/static/app.js",
  "/app/static/beta.js",
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
