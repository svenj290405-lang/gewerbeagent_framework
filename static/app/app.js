/* Gewerbeagent PWA — App-Shell-Logik (Vanilla JS, kein Build-Step).
 *
 * Verantwortlich fuer: Service-Worker-Registrierung, Session-Kontext
 * (/app/api/me), Tab-Navigation, Screen-Rendering und Web-Push-Opt-in.
 * Die einzelnen Screens holen ihre Daten frisch vom Server (EU) — es wird
 * nichts Sensibles im Client persistiert.
 */
"use strict";

const App = {
  me: null,
  view: document.getElementById("view"),
  current: "aktuelles",
  lastScreen: null,
};

// ---------- Brand-Color ----------
function applyBrandColor(color) {
  const c = (color && /^#[0-9a-fA-F]{6}$/.test(color)) ? color : "#0066cc";
  document.documentElement.style.setProperty("--primary", c);
  document.querySelector('meta[name="theme-color"]').setAttribute("content", c);
}

// ---------- Helpers ----------
async function api(path, opts = {}) {
  const headers = opts.headers || {};
  if (opts.method && opts.method !== "GET" && App.me) {
    headers["X-CSRF-Token"] = App.me.csrf;
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 303 || res.redirected) { location.href = "/app/login"; return null; }
  if (res.status === 401) { location.href = "/app/login"; return null; }
  return res;
}

function esc(s) {
  // Maskiert auch " und ' — esc() wird an vielen Stellen im Attribut-Kontext
  // (attr="${esc(x)}") mit Fremddaten (Kundenname, Merkmal, Mail ...) benutzt;
  // ohne Quote-Maskierung liesse sich aus dem Attribut ausbrechen (Stored XSS,
  // da die CSP script-src 'unsafe-inline' erlaubt).
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
// Wandelt http(s)-URLs in bereits ge-esc()-tem Text in klickbare Links um
// (öffnet in neuem Tab). Muss NACH esc() laufen — die URL kann dann &amp;
// enthalten (im href harmlos, der Browser dekodiert es zurück).
function linkify(escapedHtml) {
  return String(escapedHtml).replace(/https?:\/\/[^\s<]+/g, (u) =>
    `<a href="${u.replace(/"/g, "%22")}" target="_blank" rel="noopener noreferrer">${u}</a>`);
}
function el(html) { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; }
// Mail-Entwurf von Q (type: "email_entwurf") in eine Chat-Nachricht giessen.
// Die Karte im Assistenten redigiert direkt auf .data — abgeschickt wird
// erst mit dem, was am Ende drinsteht.
function mailDraftMsg(j) {
  return { role: "mail", frage: j.frage || "", resolved: false, sent: false, data: {
    empfaenger: j.empfaenger || "", empfaenger_name: j.empfaenger_name || "",
    betreff: j.betreff || "", text: j.text || "", kunde_name: j.kunde_name || "",
    anhaenge: Array.isArray(j.anhaenge) ? j.anhaenge.slice() : [],
    hinweis: j.hinweis || "" } };
}

// Notification-API ist nicht überall da (z.B. iOS Safari ohne Home-Screen-
// Installation) — defensiv prüfen, sonst wirft ein blanker Zugriff und die
// App bleibt beim Laden hängen.
function notifSupported() { return typeof Notification !== "undefined"; }
function notifGranted() { return notifSupported() && Notification.permission === "granted"; }

// ---------- Tabs ----------
// Chat-first: nur 3 Tabs. Q (Assistent) ist die Bedienzentrale, "Aktionen"
// bündelt ALLE Anzeigen (inkl. Termine/Angebote/Rechnungen), "Mehr" den
// Kleinkram. Termine/Büro sind keine eigenen Tabs mehr; ihre Screens bleiben
// per Q-Chat ("zeig mir die Rechnungen") erreichbar.
const TABS = [
  { key: "assistent",   label: "Assistent", ico: "🤖" },
  { key: "aktuelles",   label: "Aktionen",  ico: "📋" },
  { key: "_qoverlay",   label: "Q",         ico: `<canvas id="q-tab-sphere-canvas" width="22" height="22"></canvas>`, overlay: true },
  { key: "mehr",        label: "Mehr",      ico: "⋯" },
];

function buildTabbar() {
  const bar = document.getElementById("tabbar");
  bar.innerHTML = "";
  const feats = new Set(App.me.features || []);
  TABS.filter((t) => !t.feature || feats.has(t.feature)).forEach((t) => {
    const b = el(`<button data-tab="${t.key}"><span class="ico">${t.ico}</span>${t.label}</button>`);
    if (t.overlay) {
      b.addEventListener("click", () => toggleQOverlay());
    } else {
      b.addEventListener("click", () => navigate(t.key));
    }
    bar.appendChild(b);
  });
}

// ---------- Routing ----------
// Die App tauscht Screens per innerHTML aus; ohne History-Eintraege beendet
// die Android-Zurueck-Taste in der installierten PWA die ganze App, statt
// einen Screen zurueckzugehen. navigate() schreibt darum jeden Screen als
// Hash in die History, popstate rendert ihn wieder.
//
// Aliase: was in einer Push-URL steht (z.B. "/app#anfragen", siehe
// microsoft_inbox.py) oder was ein alter Link enthaelt, ist nicht immer der
// interne Screen-Name. Unbekannte Hashes fallen still auf den Start-Screen.
const ROUTE_ALIAS = {
  rueckrufe: "rueckrufe_page",
  rechnungen: "rechnungen_page",
  angebote: "angebote_page",
  auftraege: "auftraege_page",
  anrufe: "gespraeche",     // frueherer Name des Screens
  aufnahmen: "gespraeche",  // ebenso — jetzt "Kundengespräche"
};

function screenFromHash() {
  const raw = (location.hash || "").replace(/^#/, "").split("?")[0].trim();
  if (!raw) return null;
  const key = ROUTE_ALIAS[raw] || raw;
  return SCREENS[key] ? key : null;
}

// Der Assistent ist der Home-Bereich: wer dort landet, soll nicht mehr
// zurueck koennen — der History-Stack wird beim Ansteuern komplett geleert.
const HOME_SCREEN = "assistent";

// mode: "push" (normal), "replace" (Weiterleitung — soll keinen eigenen
// Zurueck-Schritt erzeugen) oder "none" (wir rendern GERADE eine History-
// Bewegung, die URL stimmt schon).
function navigate(key, { mode = "push" } = {}) {
  // Ungespeicherte Aenderungen: der aktuelle Screen darf den Wechsel
  // abbrechen (fragt selbst nach). Muss vor jeder anderen Aufraeumarbeit
  // laufen, sonst raeumt navigate() einen Screen ab, der bleiben soll.
  if (App.dirtyGuard) {
    let weiter = true;
    try { weiter = App.dirtyGuard(); } catch (e) {}
    if (!weiter) return;
    App.dirtyGuard = null;
  }
  if (App.view) App.view.classList.remove("has-savebar");
  if (App.qSphereStop) { try { App.qSphereStop(); } catch (e) {} App.qSphereStop = null; App.qSphereCanvas = null; }
  if (App._qPasteListener) { document.removeEventListener("paste", App._qPasteListener); App._qPasteListener = null; }
  // Laeuft noch eine Sprachaufnahme, wird sie beim Verlassen verworfen —
  // sonst bliebe das Mikrofon offen und die Leiste ohne Screen zurueck.
  if (App.recAbort) { try { App.recAbort(); } catch (e) {} App.recAbort = null; }
  App.qVoice = null;
  // Ein angehängtes Bild bleibt absichtlich „kleben" — sonst ginge ein Tab-
  // Wechsel zwischen Rückfrage und Antwort verloren und der Folge-Befehl
  // landete im Text-Pfad. clearPending() räumt es auf (Aktion/Entfernen).
  App.qIntent = null;
  App.qWorking = false;
  const known = !!SCREENS[key];
  if (!known) key = "aktuelles";
  // Home ansteuern = Stack leeren: statt einen weiteren Eintrag zu stapeln,
  // gehen wir bis zum Wurzel-Eintrag zurueck (History-API kann Eintraege
  // nicht loeschen, nur dorthin springen). Der popstate-Handler rendert
  // dann per _homeReset den Home-Screen und ersetzt den Wurzel-Eintrag.
  if (key === HOME_SCREEN && mode === "push") {
    const depth = (history.state && history.state.depth) || 0;
    if (depth > 0) {
      App._homeReset = true;
      history.go(-depth);
      return;
    }
    mode = "replace";  // schon an der Wurzel: Eintrag ersetzen statt stapeln
  }
  App.current = key;
  // Assistent-Screen erbt den Kontext der vorherigen Ansicht (z.B. offene
  // Aufnahme mit Notizen), damit Q weiß, worüber der Nutzer gerade spricht.
  if (key !== "assistent") {
    App.screenContext = { screen: key, kunde: null };
  }

  // History fuehren, BEVOR gerendert wird — ein Screen, der sich selbst
  // weiterleitet (z.B. rechnungen_page ohne lexware), ueberschreibt den
  // Eintrag dann sauber per mode:"replace".
  //
  // ``depth`` wandert im History-State mit: nur so wissen wir nach einem
  // popstate, ob es noch etwas gibt, wohin man zurueck kann (history.length
  // zaehlt auch Vorwaerts-Eintraege und taugt dafuer nicht).
  if (mode !== "none") {
    const hash = "#" + key;
    // Home ist immer die Wurzel (depth 0) — auch wenn z.B. ein Reload auf
    // einem tieferen History-Eintrag gebootet hat.
    const cur = key === HOME_SCREEN ? 0 : (history.state && history.state.depth) || 0;
    if (mode === "replace" || location.hash === hash) {
      history.replaceState({ screen: key, depth: cur }, "", hash);
    } else {
      history.pushState({ screen: key, depth: cur + 1 }, "", hash);
    }
  }
  updateBackButton();

  document.querySelectorAll(".tabbar button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === key));
  const fn = SCREENS[key];
  App.view.innerHTML = `<div class="loading">Lädt …</div>`;
  fn().catch((e) => {
    // Netz-/Laufzeitfehler beim Rendern eines Screens: nie sang- und klanglos
    // leer lassen — ein Retry-Button (rendert App.current neu) muss immer da
    // sein, sonst steckt der Nutzer im Funkloch fest.
    App.view.innerHTML = errorScreen("Konnte nicht laden. Bist du gerade offline?");
    console.error(e);
  });
}

// Zurueck-Taste / Wisch-Geste: den Screen aus dem History-Eintrag rendern,
// ohne einen neuen Eintrag zu erzeugen.
window.addEventListener("popstate", (e) => {
  if (!App.me) return;  // noch im Boot
  // Ein Home-Tap hat uns per history.go() an die Wurzel springen lassen:
  // dort den Eintrag durch Home ersetzen — der Stack ist damit leer.
  if (App._homeReset) {
    App._homeReset = false;
    navigate(HOME_SCREEN, { mode: "replace" });
    return;
  }
  // Liegt ein Modal obendrauf, meint "zurueck" das Modal — nicht den Screen
  // darunter. Wir schliessen es und stellen den History-Eintrag wieder her,
  // sonst haetten wir den Schritt verbraucht und der Screen waere gewechselt.
  const modal = document.getElementById("archiv-preview-modal");
  if (modal) {
    modal.remove();
    App._archivPreviewFile = null;
    const depth = (history.state && history.state.depth) || 0;
    history.pushState({ screen: App.current, depth: depth + 1 }, "", "#" + App.current);
    updateBackButton();
    return;
  }
  // Ungespeicherte Aenderungen: hier muss der History-Schritt zurueckgelegt
  // werden, wenn der Nutzer bleiben will — sonst zeigt die App den alten
  // Screen, waehrend die Adresse schon auf dem neuen steht.
  if (App.dirtyGuard) {
    let weiter = true;
    try { weiter = App.dirtyGuard(); } catch (er) {}
    if (!weiter) {
      const tiefe = (history.state && history.state.depth) || 0;
      history.pushState({ screen: App.current, depth: tiefe + 1 }, "", "#" + App.current);
      updateBackButton();
      return;
    }
    App.dirtyGuard = null;
  }
  toggleQOverlay(true);  // offenes Q-Overlay schliessen, sonst liegt es ueber dem Screen
  const key = (e.state && e.state.screen) || screenFromHash() || HOME_SCREEN;
  navigate(key, { mode: "none" });
});

// Die Push-Benachrichtigung navigiert eine bereits offene App per
// client.navigate("/app#anfragen") (sw.js) — das aendert nur den Hash und
// laedt nichts neu. Ohne diesen Listener passiert dann schlicht nichts.
window.addEventListener("hashchange", () => {
  if (!App.me) return;
  const key = screenFromHash();
  if (key && key !== App.current) navigate(key, { mode: "none" });
});

// Zurueck-Pfeil im Header zeigen, sobald ein Schritt zurueck existiert.
function updateBackButton() {
  const btn = document.getElementById("back-btn");
  if (!btn) return;
  const depth = (history.state && history.state.depth) || 0;
  btn.hidden = depth < 1;
}

// Kanten-Wisch-Geste: iOS liefert installierten PWAs keine eigene, also
// bauen wir sie nach — vom linken Rand nach rechts ziehen = zurueck.
// Bewusst eng gefasst (Start am Rand, klar horizontal), damit sie nicht mit
// Scrollen oder dem Q-Overlay kollidiert.
const EDGE_START_PX = 28;   // nur ein Zug, der am linken Rand beginnt
const EDGE_MIN_DX = 70;     // so weit muss gezogen werden
const EDGE_MAX_DY = 45;     // darueber ist es eine Scroll-Bewegung
function initEdgeSwipe() {
  let startX = null, startY = null;
  document.addEventListener("touchstart", (e) => {
    if (e.touches.length !== 1) { startX = null; return; }
    const t = e.touches[0];
    startX = t.clientX <= EDGE_START_PX ? t.clientX : null;
    startY = t.clientY;
  }, { passive: true });
  document.addEventListener("touchend", (e) => {
    if (startX === null) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - startX;
    const dy = Math.abs(t.clientY - startY);
    startX = null;
    if (dx < EDGE_MIN_DX || dy > EDGE_MAX_DY) return;
    if ((history.state && history.state.depth) > 0) history.back();
  }, { passive: true });
}

// ---------- Screens ----------
const SCREENS = {
  async aktuelles() {
    const feats = new Set(App.me.features || []);
    const hasMail = feats.has("mail_intake");
    const hasKal = feats.has("kalender");
    const hasLex = feats.has("lexware");
    // Eine Abfrage fuer den ganzen Geld-Teil: /app/api/buchhaltung liefert
    // Kennzahlen, Rechnungen und Angebote zusammen — vorher waren das zwei
    // Aufrufe, deren Zahlen die Kachel dann selbst zusammenzaehlen musste.
    const [akRes, termRes, anfRes, buchRes] = await Promise.all([
      api("/app/api/aktuelles"),
      hasKal ? api("/app/api/termine") : Promise.resolve(null),
      hasMail ? api("/app/api/anfragen") : Promise.resolve(null),
      hasLex ? api("/app/api/buchhaltung") : Promise.resolve(null),
    ]);
    const ak = akRes && akRes.ok ? await akRes.json() : {};
    // Der Primaer-Call traegt Beratung/Auftraege/Rueckrufe. Faellt er, waere
    // der Hub still leer ("nichts zu tun") — hier stattdessen ein sichtbarer
    // Hinweis mit Retry, damit kein offener Rueckruf uebersehen wird.
    const akFailed = akRes && !akRes.ok;
    const td = termRes && termRes.ok ? await termRes.json() : { termine: [] };
    const ad = anfRes && anfRes.ok ? await anfRes.json() : { items: [] };
    const buch = buchRes && buchRes.ok ? await buchRes.json() : null;
    const angd = { angebote: (buch && buch.angebote) || [] };
    const rechd = { rechnungen: (buch && buch.rechnungen) || [] };
    const beratung = ak.beratung || [];
    const auftraege = ak.auftraege || [];
    const rueckrufe = ak.rueckrufe || [];
    const aufnahmenCount = ak.aufnahmen_count || 0;
    const termine = td.termine || [];
    const anfragenOffen = (ad.items || []).filter((x) => !x.closed);
    const angebote = angd.angebote || [];
    const rechnungen = rechd.rechnungen || [];
    const parts = [];

    // Kopf ohne Schnellstart-Knöpfe: „Gespräch" und „Rückruf" waren doppelt —
    // beides steht schon in der eigenen Kachel (dort mit voller Liste).
    parts.push(
      `<h1 style="font-size:22px;margin:4px 4px 14px">Aktionen</h1>`);

    if (akFailed) {
      parts.push(
        `<div class="banner" style="background:#fde8e8;border-color:#f5b5b5">
           Einige Daten konnten gerade nicht geladen werden — die Liste ist evtl. unvollständig.
           <button class="btn-sm btn-ghost" onclick="navigate(App.current)">Neu laden</button>
         </div>`);
    }

    parts.push(
      `<details class="card q-briefing" open>
         <summary class="q-briefing-head" style="list-style:none;cursor:pointer">
           <span class="q-badge">Q</span><span>Dein Tag</span>
           <button class="q-briefing-refresh" id="ak-briefing-refresh" title="Neu schreiben" onclick="event.stopPropagation()">🔄</button>
         </summary>
         <p class="q-briefing-text" id="q-briefing-text"><span class="q-briefing-load">Q schreibt dein Briefing …</span></p>
       </details>`);

    if (notifSupported() && !notifGranted()) {
      parts.push(`<div class="banner">Aktiviere Benachrichtigungen, damit du neue Buchungen und Rückrufe sofort siehst. <button class="btn-sm btn-ghost" id="enable-notif-inline">Aktivieren</button></div>`);
    }

    if (beratung.length) {
      parts.push(`<div class="section-title">Neue Beratungsgespräche (${beratung.length})</div>`);
      beratung.forEach((b) => {
        parts.push(
          `<div class="card lead">
             <div><b>${esc(b.kunde)}</b>${b.termin ? `<div class="sub">📅 ${esc(b.termin)}</div>` : ""}${b.briefing ? `<div class="sub">${esc(b.briefing)}</div>` : ""}</div>
             <div class="confirm-actions" style="margin-top:10px">
               <button class="btn-sm" data-lead-ja="${esc(b.id)}">Annehmen</button>
               <button class="btn-sm btn-ghost" data-lead-nein="${esc(b.id)}">Ablehnen</button>
             </div>
           </div>`);
      });
    }

    parts.push(`<div class="section-title">Bereiche</div>`);
    const tiles = [];
    if (hasKal) tiles.push({
      ico: "📅", label: "Termine", screen: "termine",
      count: termine.length ? `${termine.length} anstehend` : "Keine anstehend",
      badge: termine.length || null, badgeClass: "",
    });
    if (hasMail) tiles.push({
      ico: "✉️", label: "Anfragen", screen: "anfragen",
      count: anfragenOffen.length ? `${anfragenOffen.length} offen` : "Keine offen",
      badge: anfragenOffen.length || null, badgeClass: "",
    });
    // Angebote, Rechnungen und Belege lagen frueher als eigene Kacheln
    // (bzw. gar nicht) herum — alles Geld steckt jetzt hinter EINER Kachel.
    // Das Abzeichen zeigt, was Geld kostet: ueberfaellige Rechnungen, sonst
    // die offenen. Zahlen kommen aus /app/api/buchhaltung (eine Quelle).
    // Geld-Kachel nur fuer wen die Buchhaltung sehen darf — sonst
    // fuehrt sie auf einen Screen, der mit 403 antwortet.
    if (hasLex && can("buchhaltung.sehen")) {
      const bk = (buch && buch.kennzahlen) || {};
      const ueberfaellig = bk.ueberfaellig_anzahl || 0;
      const offen = bk.offen_anzahl || 0;
      tiles.push({
        ico: "💰", label: "Buchhaltung", screen: "buchhaltung",
        count: offen
          ? `${fmtEur(bk.offen_eur)} offen`
          : (rechnungen.length || angebote.length ? "Alles bezahlt" : "Keine Belege"),
        badge: ueberfaellig || offen || null,
        badgeClass: ueberfaellig ? "" : "warn",
      });
    }
    tiles.push({
      ico: "🛠️", label: "Aufträge", screen: "auftraege_page",
      count: auftraege.length ? `${auftraege.length} laufend` : "Keine laufenden",
      badge: null,
    });
    tiles.push({
      ico: "📞", label: "Rückrufe", screen: "rueckrufe_page",
      count: rueckrufe.length ? `${rueckrufe.length} offen` : "Keine offen",
      badge: rueckrufe.length || null, badgeClass: "warn",
    });
    tiles.push({
      ico: "🎙️", label: "Kundengespräche", screen: "gespraeche",
      count: aufnahmenCount ? `${aufnahmenCount} Gespräche` : "Keine Gespräche",
      badge: null,
    });
    // Das Kunden-Formular gehoert zur taeglichen Arbeit (es haengt an jeder
    // Anfrage-Mail), nicht in die Einstellungen — darum hier statt in „Mehr".
    if (feats.has("anfrage_formular") && can("einstellungen.verwalten")) {
      tiles.push({
        ico: "📝", label: "Anfrage-Formular", screen: "formulare",
        count: "Was Kunden ausfüllen", badge: null,
      });
    }
    // Feste Reihenfolge — NICHT nach Badge umsortieren. Die Zielgruppe lernt
    // die Kachel-Position per Muskelgedaechtnis ("Buchhaltung ist da unten");
    // ein wanderndes Grid zerstoert das. Handlungsbedarf zeigt das Badge.
    parts.push(`<div class="homescreen-grid">`);
    tiles.forEach((t) => {
      const badge = (t.badge && t.badge > 0)
        ? `<span class="tile-badge ${t.badgeClass || ""}">${t.badge}</span>` : "";
      parts.push(
        `<button class="home-tile" data-go="${esc(t.screen)}">
           ${badge}
           <span class="tile-ico">${t.ico}</span>
           <span class="tile-label">${esc(t.label)}</span>
           <span class="tile-count">${esc(t.count)}</span>
         </button>`);
    });
    parts.push(`</div>`);

    App.view.innerHTML = parts.join("");
    const inline = document.getElementById("enable-notif-inline");
    if (inline) inline.addEventListener("click", enablePush);
    const briefRefresh = document.getElementById("ak-briefing-refresh");
    if (briefRefresh) briefRefresh.addEventListener("click", () => loadBriefing(true));
    loadBriefing(false);
    bindAktuelles();
    document.querySelectorAll(".home-tile[data-go]").forEach((b) =>
      b.addEventListener("click", () => navigate(b.dataset.go)));
  },

  async auftraege_page() {
    App.view.innerHTML = `<div class="loading">Lädt …</div>`;
    const res = await api("/app/api/auftraege");
    if (res && !res.ok) { App.view.innerHTML = errorScreen("Auftraege konnten nicht geladen werden."); return; }
    const d = res && res.ok ? await res.json() : { auftraege: [] };
    // Auftraege anlegen/steuern — serverseitig durchgesetzt, hier nur Anzeige.
    const isInhaber = can("auftraege.fuehren");
    const list = (d.auftraege || []).map((a) => auftragCard(a, isInhaber)).join("");
    // Unten die Sammel-Funktionen: das Archiv der abgerechneten Aufträge,
    // die vollständige Historie (abgerechnet UND abgebrochen) und der Editor
    // für den Ablauf selbst. Umbrechend, damit die Knöpfe auf dem Handy nicht
    // zu schmalen Streifen zusammengequetscht werden.
    const fuss =
      `<div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:18px">
         <button class="btn-sm btn-ghost" id="auf-fertig" style="flex:1 1 45%;padding:14px 10px;text-align:center">✅ Abgeschlossene Aufträge</button>
         <button class="btn-sm btn-ghost" id="auf-historie" style="flex:1 1 45%;padding:14px 10px;text-align:center">🗂️ Auftragshistorie</button>
         ${isInhaber ? `<button class="btn-sm btn-ghost" id="auf-prozess" style="flex:1 1 100%;padding:14px 10px;text-align:center">⚙️ Auftragsprozess bearbeiten</button>` : ""}
       </div>`;
    // Neu-Button oben: Aufträge von Hand sind der Einstieg für alles, was
    // ohne Angebot reinkommt — der gehört über die Liste, nicht in den Fuß.
    const neu = isInhaber
      ? `<button class="btn-sm btn-ghost" id="auf-neu" style="width:100%;margin-bottom:12px;padding:14px 10px">➕ Auftrag von Hand anlegen</button>`
      : "";
    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-db" style="margin-bottom:10px">← Aktionen</button>` +
      `<h1 style="font-size:22px;margin:4px 4px 14px">Aufträge</h1>` +
      neu +
      (list || `<div class="card">${emptyRow("Keine laufenden Aufträge")}</div>`) +
      fuss;
    document.getElementById("back-db").addEventListener("click", () => navigate("aktuelles"));
    const neuBtn = document.getElementById("auf-neu");
    if (neuBtn) neuBtn.addEventListener("click", () => navigate("auftrag_neu"));
    document.getElementById("auf-fertig").addEventListener("click",
      () => navigate("auftraege_fertig"));
    document.getElementById("auf-historie").addEventListener("click",
      () => navigate("auftraege_historie"));
    const proz = document.getElementById("auf-prozess");
    if (proz) proz.addEventListener("click", () => showProzessEditor("auftraege_page"));
    bindAuftragOeffnen("auftraege_page");
    bindFortschrittsRegler();
    bindStundenBuchung();
    if (isInhaber) bindAuftragActions(() => navigate("auftraege_page", { mode: "none" }));
  },

  async auftraege_fertig() {
    App.view.innerHTML = `<div class="loading">Lädt …</div>`;
    const res = await api("/app/api/auftraege/abgeschlossen");
    if (res && !res.ok) { App.view.innerHTML = errorScreen("Auftraege konnten nicht geladen werden."); return; }
    const d = res && res.ok ? await res.json() : { auftraege: [] };
    const liste = (d.auftraege || []).map((a) => {
      const archiv = a.archiv_url
        ? `<a href="${esc(a.archiv_url)}" target="_blank" rel="noopener" class="sub" style="white-space:nowrap">📁 Drive ›</a>`
        : `<span class="sub" style="white-space:nowrap">—</span>`;
      return `<div class="card" style="cursor:pointer">
        <div class="row">
          <div data-auftrag-open="${esc(a.id)}" style="flex:1">
            <div><b>${esc(a.kunde)}</b></div>
            <div class="sub">${esc(a.betrag)}${a.abgeschlossen_am ? " · abgeschlossen " + esc(a.abgeschlossen_am) : ""}</div>
          </div>
          ${archiv}
        </div></div>`;
    }).join("");
    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-auf" style="margin-bottom:10px">← Aufträge</button>` +
      `<h1 style="font-size:22px;margin:4px 4px 6px">Abgeschlossene Aufträge</h1>` +
      `<p class="muted" style="margin:0 4px 14px">Fertig abgerechnet. Jeder Auftrag liegt zusätzlich als eigener Ordner im Drive.</p>` +
      (liste || `<div class="card">${emptyRow("Noch keine abgeschlossenen Aufträge")}</div>`);
    document.getElementById("back-auf").addEventListener("click", () => navigate("auftraege_page"));
    bindAuftragOeffnen("auftraege_fertig");
  },

  // Auftragshistorie: alles, was durch ist — abgerechnet UND abgebrochen.
  // Bewusst neben der Abgeschlossenen-Liste: die ist das Rechnungs-Archiv
  // mit den Drive-Ordnern, hier steht die vollständige Vergangenheit
  // („was hatten wir bei dem Kunden schon"), inkl. der Abbrüche.
  async auftraege_historie() {
    App.view.innerHTML = `<div class="loading">Lädt …</div>`;
    const res = await api("/app/api/auftraege/historie");
    if (res && !res.ok) { App.view.innerHTML = errorScreen("Historie konnte nicht geladen werden."); return; }
    const d = res && res.ok ? await res.json() : { auftraege: [] };
    const liste = (d.auftraege || []).map((a) => {
      const archiv = a.archiv_url
        ? `<a href="${esc(a.archiv_url)}" target="_blank" rel="noopener" class="sub" style="white-space:nowrap">📁 Drive ›</a>`
        : "";
      // Der Abbruch ist die Information, die man auf einen Blick braucht —
      // sonst liest man eine Zeile wie einen erledigten Auftrag.
      const pill = a.abgebrochen
        ? `<span class="pill danger">Abgebrochen</span>`
        : `<span class="pill ok">Abgerechnet</span>`;
      const wann = a.beendet_am
        ? (a.abgebrochen ? " · abgebrochen " : " · abgeschlossen ") + esc(a.beendet_am)
        : "";
      return `<div class="card" style="cursor:pointer">
        <div class="row">
          <div data-auftrag-open="${esc(a.id)}" style="flex:1">
            <div><b>${esc(a.kunde)}</b></div>
            <div class="sub">${esc(a.betrag)}${wann}</div>
          </div>
          <div style="display:flex;align-items:center;gap:8px">${pill}${archiv}</div>
        </div></div>`;
    }).join("");
    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-auf" style="margin-bottom:10px">← Aufträge</button>` +
      `<h1 style="font-size:22px;margin:4px 4px 6px">Auftragshistorie</h1>` +
      `<p class="muted" style="margin:0 4px 14px">Alle fertiggestellten Aufträge — abgerechnete und abgebrochene. Sie sind aus der laufenden Liste raus.</p>` +
      (liste || `<div class="card">${emptyRow("Noch keine fertiggestellten Aufträge")}</div>`);
    document.getElementById("back-auf").addEventListener("click", () => navigate("auftraege_page"));
    bindAuftragOeffnen("auftraege_historie");
  },

  // Auftrag von Hand — für Arbeit, die nie durch die Angebots-Pipeline lief
  // (am Telefon vereinbart, auf der Baustelle zugerufen, Stammkunde). Teilt
  // sich Kunden- und Positionen-Felder mit dem Angebots-Composer, damit
  // beide Formulare gleich zu bedienen sind.
  async auftrag_neu() {
    App.view.innerHTML = `<div class="loading">Lädt …</div>`;
    _composerPositionen = [{ name: "", menge: 1, einheit: "Stueck", preis_brutto_eur: 0 }];
    // Die Startschritte kommen aus dem Auftragsprozess des Betriebs, damit
    // sie genauso heißen wie in der Fortschrittszeile. Eigene Schritte sind
    // keine Status, und „Rechnung raus" schließt den Auftrag ab (Geld-Pfad)
    // — beides fällt raus.
    const res = await api("/app/api/auftragsprozess");
    const d = res && res.ok ? await res.json() : { schritte: [] };
    const opts = (d.schritte || [])
      .filter((s) => s.typ === "kern" && s.kern_status !== "rechnung_gesendet")
      .map((s) => `<option value="${esc(s.kern_status)}"${s.kern_status === "accepted" ? " selected" : ""}>${esc(s.label)}</option>`)
      .join("");
    const inp = "width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px";
    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="an-back" style="margin-bottom:10px">← Aufträge</button>` +
      `<h1 style="font-size:22px;margin:4px 4px 6px">Auftrag von Hand</h1>` +
      `<p class="muted" style="margin:0 4px 14px">Für Arbeit, die ohne Angebot reingekommen ist. Der Auftrag läuft danach ganz normal weiter — die Rechnung entsteht am Ende aus den Positionen.</p>` +
      _kiExtractCard("Kunde + Positionen mit Preisen") +
      _composerKundenFields() +
      `<div class="card"><h2>Positionen</h2>
         <div id="pos-list"></div>
         <button class="btn-sm btn-ghost" id="pos-add" style="margin-top:6px;width:100%">+ Position</button>
         <div class="row" style="margin-top:12px;padding-top:10px;border-top:1px solid var(--line)">
           <b>Gesamt brutto</b><b id="pos-summe">0,00 €</b>
         </div>
       </div>` +
      `<div class="card"><h2>Startschritt</h2>
         <label class="sub">Wo steht der Auftrag gerade?</label>
         <select id="an-status" style="${inp}">${opts}</select>
       </div>` +
      `<button class="btn-sm" id="an-save" style="width:100%;margin-top:8px">Auftrag anlegen</button>`;

    document.getElementById("an-back").addEventListener("click", () => navigate("auftraege_page"));
    _renderPositionen();
    document.getElementById("pos-add").addEventListener("click", () => {
      _composerPositionen.push({ name: "", menge: 1, einheit: "Stueck", preis_brutto_eur: 0 });
      _renderPositionen();
    });
    _bindKiExtract("/app/api/angebote/extrahieren", (ex) => {
      _applyExtractedToKunde(ex);
      if (Array.isArray(ex.positionen) && ex.positionen.length) {
        _composerPositionen = ex.positionen.map((p) => ({
          name: p.name || "", beschreibung: p.beschreibung || "",
          menge: p.menge || 1, einheit: p.einheit || "Stueck",
          preis_brutto_eur: p.preis_brutto_eur || 0,
          mwst_prozent: p.mwst_prozent || 19,
        }));
        _renderPositionen();
      }
    });
    document.getElementById("an-save").addEventListener("click", _submitAuftragNeu);
  },

  async rechnungen_page() {
    const feats = new Set(App.me.features || []);
    // Weiterleitung (z.B. via veraltetem Deep-Link) darf keinen eigenen
    // Zurueck-Schritt erzeugen, sonst landet man in einer Schleife.
    if (!feats.has("lexware")) { navigate("aktuelles", { mode: "replace" }); return; }
    // Rechnungen schreiben — serverseitig durchgesetzt, hier nur Anzeige.
    const isInhaber = can("buchhaltung.fuehren");
    const res = await api("/app/api/rechnungen");
    if (res && !res.ok) { App.view.innerHTML = errorScreen("Rechnungen konnten nicht geladen werden."); return; }
    const d = res && res.ok ? await res.json() : { rechnungen: [] };
    const rechnungen = d.rechnungen || [];
    const btns = isInhaber
      ? `<div style="display:flex;gap:6px;flex-wrap:wrap">
           <button class="btn-sm btn-ghost" id="rech-pruefen-btn" style="padding:8px 12px">🔄 Prüfen</button>
           <button class="btn-sm" id="rech-new-btn" style="padding:8px 14px">+ Neu</button>
         </div>` : "";
    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-db" style="margin-bottom:10px">← Buchhaltung</button>` +
      `<div style="display:flex;align-items:center;justify-content:space-between;margin:4px 4px 14px">
         <h1 style="font-size:22px;margin:0">Rechnungen</h1>${btns}
       </div>` +
      `<div class="card">${rechnungen.length
        ? rechnungen.map((x) => rowPillLink(x.kunde + (x.nummer ? " · " + x.nummer : ""), x.betrag + " · " + x.zeit, x.status, x.pill, x.lexware_link)).join("")
        : emptyRow("Noch keine Rechnungen")
      }</div>`;
    document.getElementById("back-db").addEventListener("click", () => navigate("buchhaltung"));
    const rBtn = document.getElementById("rech-new-btn");
    if (rBtn) rBtn.addEventListener("click", () => { App.lastScreen = "rechnungen_page"; showRechnungForm(); });
    const pBtn = document.getElementById("rech-pruefen-btn");
    if (pBtn) pBtn.addEventListener("click", async () => {
      const orig = pBtn.textContent;
      pBtn.disabled = true; pBtn.textContent = "Prüfe …";
      const r = await api("/app/api/rechnungen/pruefen", { method: "POST", body: "{}" });
      const j = r ? await r.json().catch(() => null) : null;
      pBtn.disabled = false; pBtn.textContent = orig;
      if (j && j.ok) {
        if ((j.bezahlt || 0) > 0) { toast(`✓ ${j.bezahlt} Rechnung(en) als bezahlt markiert`); navigate("rechnungen_page"); }
        else { alert(`Geprüft: ${j.geprueft || 0} offene Rechnung(en) — keine neuen Zahlungen.`); }
      } else { alert((j && j.error) || "Konnte nicht prüfen."); }
    });
  },

  async angebote_page() {
    const feats = new Set(App.me.features || []);
    // Weiterleitung (z.B. via veraltetem Deep-Link) darf keinen eigenen
    // Zurueck-Schritt erzeugen, sonst landet man in einer Schleife.
    if (!feats.has("lexware")) { navigate("aktuelles", { mode: "replace" }); return; }
    // Angebote schreiben — serverseitig durchgesetzt, hier nur Anzeige.
    const isInhaber = can("buchhaltung.fuehren");
    const res = await api("/app/api/angebote");
    if (res && !res.ok) { App.view.innerHTML = errorScreen("Angebote konnten nicht geladen werden."); return; }
    const d = res && res.ok ? await res.json() : { angebote: [] };
    const angebote = d.angebote || [];
    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-db" style="margin-bottom:10px">← Buchhaltung</button>` +
      `<div style="display:flex;align-items:center;justify-content:space-between;margin:4px 4px 14px">
         <h1 style="font-size:22px;margin:0">Angebote</h1>
         ${isInhaber ? `<button class="btn-sm" id="ang-new-btn" style="padding:8px 14px">+ Neu</button>` : ""}
       </div>` +
      `<div class="card">${angebote.length
        ? angebote.map((x) => rowPillLink(x.kunde, x.betrag + " · " + x.zeit, x.status, x.pill, x.lexware_link)).join("")
        : emptyRow("Noch keine Angebote")
      }</div>`;
    document.getElementById("back-db").addEventListener("click", () => navigate("buchhaltung"));
    const aBtn = document.getElementById("ang-new-btn");
    if (aBtn) aBtn.addEventListener("click", () => { App.lastScreen = "angebote_page"; showAngebotForm(); });
  },

  async rueckrufe_page() {
    const res = await api("/app/api/rueckrufe");
    if (res && !res.ok) { App.view.innerHTML = errorScreen("Rueckrufe konnten nicht geladen werden."); return; }
    const d = res && res.ok ? await res.json() : { rueckrufe: [] };
    const rueckrufe = d.rueckrufe || [];
    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-db" style="margin-bottom:10px">← Aktionen</button>` +
      `<div style="display:flex;align-items:center;justify-content:space-between;margin:4px 4px 14px">
         <h1 style="font-size:22px;margin:0">Offene Rückrufe</h1>
         <button class="btn-sm btn-ghost" id="rr-new-btn" style="padding:8px 12px">+ Rückruf</button>
       </div>` +
      `<div class="card">${rueckrufe.length
        ? rueckrufe.map((r) => rowAction(r.kunde, r.telefon + (r.anliegen ? " · " + esc(r.anliegen) : ""), "", r.id, "rueckruf-done", "Erledigt")).join("")
        : emptyRow("Keine offenen Rückrufe")
      }</div>`;
    document.getElementById("back-db").addEventListener("click", () => navigate("aktuelles"));
    document.getElementById("rr-new-btn").addEventListener("click", showNewRueckrufForm);
    bindRueckrufDone();
  },

  async termine() {
    const [t, a] = await Promise.all([api("/app/api/termine"), api("/app/api/aufnahmen")]);
    if (t && !t.ok) { App.view.innerHTML = errorScreen("Termine konnten nicht geladen werden."); return; }
    const d = t && t.ok ? await t.json() : { termine: [] };
    const ad = a && a.ok ? await a.json() : { aufnahmen: [] };
    const list = d.termine || [];
    const aufnahmen = ad.aufnahmen || [];
    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-db" style="margin-bottom:10px">← Aktionen</button>` +
      `<div style="display:flex;align-items:center;justify-content:space-between;margin:4px 4px 14px">
        <h1 style="font-size:22px;margin:0">Termine</h1>
        <button class="btn-sm" id="termin-new-btn" style="padding:8px 14px">+ Neu</button>
      </div>` +
      `<div class="card"><h2>Anstehende Termine</h2>${
        list.length ? list.map((x) => rowAction(x.zeit, x.kunde, x.ort, x.id, "storno", "Stornieren")).join("") : emptyRow("Keine anstehenden Termine")
      }</div>` +
      `<div class="card"><h2>Briefings</h2>${
        aufnahmen.length ? aufnahmen.map((x) => rowTap(x.kunde || "Aufnahme", x.briefing || "", x.zeit, x.id)).join("") : emptyRow("Keine Briefings")
      }</div>`;
    document.getElementById("back-db").addEventListener("click", () => navigate("aktuelles"));
    bindStorno();
    bindAufnahmen();
    document.getElementById("termin-new-btn").addEventListener("click", showNewTerminForm);
  },

  // Kundengespräche. Bewusst OHNE die offenen Rueckrufe: die haben ihre
  // eigene Kachel (rueckrufe_page) und sind eine To-do-Liste, kein Gespräch.
  // Der Screen hiess frueher "Anrufe", dann "Aufnahmen" — beides zu eng: hier
  // haengt inzwischen alles vom Kundentermin (Diktat, Notiz, Fotos, Bilder).
  async gespraeche() {
    const a = await api("/app/api/gespraeche");
    if (a && !a.ok) { App.view.innerHTML = errorScreen("Gespraeche konnten nicht geladen werden."); return; }
    const ad = a && a.ok ? await a.json() : { gespraeche: [] };
    const liste = ad.gespraeche || [];
    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-db" style="margin-bottom:10px">← Aktionen</button>` +
      `<h1 style="font-size:22px;margin:4px 4px 14px">Kundengespräche</h1>` +
      `<button class="btn-sm" id="gespr-neu" style="width:100%;margin-bottom:12px;padding:14px 10px">➕ Neues Kundengespräch</button>` +
      // Platzhalter: der Kalender-Abruf geht über den Provider und dauert
      // länger als die Liste — der Screen soll darauf nicht warten.
      `<div class="card" id="gespr-geplant"><h2>Geplant</h2><div class="empty">Schaue in den Kalender …</div></div>` +
      `<div class="card"><h2>Bisherige Gespräche</h2>${
        liste.length
          ? liste.map((x) => rowTap(x.kunde || "Gespräch",
              (x.abgeschlossen ? "✅ eingepflegt · " : "") + (x.briefing || ""), x.zeit, x.id)).join("")
          : emptyRow("Noch kein Gespräch erfasst")
      }</div>`;
    document.getElementById("back-db").addEventListener("click", () => navigate("aktuelles"));
    document.getElementById("gespr-neu").addEventListener("click", () => navigate("gespraech_neu"));
    bindAufnahmen();
    _geplanteGespraecheLaden();
  },

  // Alter Screen-Name — Q („zeig mir die Aufnahmen") und alte Links zeigen
  // weiter hierher, landen aber im neuen Bereich.
  async aufnahmen() { await SCREENS.gespraeche(); },

  // Einstieg: erst der Kunde, dann alles andere. Nur so kann die App im
  // Gespräch seine Daten zeigen, statt sie hinterher aus dem Diktat zu raten.
  //
  // Aus einem geplanten Kalendertermin heraus (App.gespraechVorgabe) ist der
  // Name schon vorgeschlagen — der Handwerker bestätigt ihn oder tippt um.
  // Geraten wird er aus dem Termin-Betreff, deshalb wird er NIE ungefragt
  // als Kunde angelegt.
  async gespraech_neu() {
    const vorgabe = App.gespraechVorgabe || null;
    App.gespraechVorgabe = null;
    App.view.innerHTML = `<div class="loading">Lädt …</div>`;
    const r = await api("/app/api/kunden");
    const d = r && r.ok ? await r.json() : { kunden: [] };
    const alle = (d.kunden || []).map((k) => (typeof k === "string" ? { name: k } : k));
    const inp = "width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;font-size:16px";
    const untertitel = vorgabe && vorgabe.zeit
      ? `Termin ${esc(vorgabe.zeit)}${vorgabe.ort ? " · " + esc(vorgabe.ort) : ""} — mit wem sprichst du?`
      : "Mit wem sprichst du?";
    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="gn-back" style="margin-bottom:10px">← Gespräche</button>` +
      `<h1 style="font-size:22px;margin:4px 4px 6px">Neues Kundengespräch</h1>` +
      `<p class="muted" style="margin:0 4px 14px">${untertitel}</p>` +
      `<div class="card">
         <input type="text" id="gn-suche" placeholder="Kunde suchen oder neuen Namen eingeben" style="${inp}"
                autocomplete="off" value="${esc((vorgabe && vorgabe.name) || "")}" />
         <div id="gn-treffer" style="margin-top:10px"></div>
       </div>`;
    document.getElementById("gn-back").addEventListener("click", () => navigate("gespraeche"));

    const feld = document.getElementById("gn-suche");
    const trefferEl = document.getElementById("gn-treffer");
    const zeichnen = () => {
      const q = feld.value.trim().toLowerCase();
      const treffer = (q ? alle.filter((k) => (k.name || "").toLowerCase().includes(q)) : alle).slice(0, 12);
      trefferEl.innerHTML =
        treffer.map((k) => `<button class="row menu-item" data-kid="${esc(k.id || "")}" data-kname="${esc(k.name || "")}">
            <span>👤 ${esc(k.name || "")}</span><span class="sub">›</span></button>`).join("") +
        (q.length >= 2 && !treffer.some((k) => (k.name || "").toLowerCase() === q)
          ? `<button class="row menu-item" data-kid="" data-kname="${esc(feld.value.trim())}">
               <span>🆕 „${esc(feld.value.trim())}" als neuen Kunden</span><span class="sub">›</span></button>`
          : "") +
        (!treffer.length && q.length < 2 ? emptyRow("Noch keine Kunden — Namen eintippen") : "");
      trefferEl.querySelectorAll("[data-kname]").forEach((b) =>
        b.addEventListener("click", () => starten(b.dataset.kid, b.dataset.kname)));
    };
    const starten = async (kid, kname) => {
      const nutzlast = kid ? { kunde_id: kid } : { kunde_name: kname };
      if (vorgabe) {
        nutzlast.kalender_event_id = vorgabe.event_id || "";
        nutzlast.termin_iso = vorgabe.termin_iso || "";
        nutzlast.termin_ort = vorgabe.ort || "";
      }
      const res = await api("/app/api/gespraeche", { method: "POST",
        body: JSON.stringify(nutzlast) });
      const j = res ? await res.json().catch(() => null) : null;
      if (j && j.ok) { showGespraech(j.id); return; }
      alert((j && j.error) || "Konnte das Gespräch nicht anlegen.");
    };
    feld.addEventListener("input", zeichnen);
    zeichnen();
  },

  // Ein Bereich fuer alles, was mit Geld zu tun hat: offene Posten,
  // Rechnungen, Angebote, Belege. Frueher lagen die auf drei Kacheln
  // ("Angebote", "Rechnungen") plus einem verwaisten "Buero"-Screen, den
  // man nur ueber Hilfe & Tour fand — die Belege waren damit praktisch
  // unsichtbar. Eine einzige Abfrage (/app/api/buchhaltung) liefert alles.
  async buchhaltung() {
    const feats = new Set(App.me.features || []);
    if (!feats.has("lexware")) { navigate("aktuelles", { mode: "replace" }); return; }
    // Mahnen, Zahlungsabgleich, Neu — serverseitig durchgesetzt, hier nur Anzeige.
    const isInhaber = can("buchhaltung.fuehren");
    App.view.innerHTML = `<div class="loading">Lädt …</div>`;
    const res = await api("/app/api/buchhaltung");
    const d = res && res.ok ? await res.json() : null;
    if (!d || !d.ok) {
      App.view.innerHTML =
        `<button class="btn-sm btn-ghost" id="back-db" style="margin-bottom:10px">← Aktionen</button>` +
        `<div class="card">${emptyRow((d && d.error) || "Buchhaltung nicht erreichbar.")}</div>`;
      document.getElementById("back-db").addEventListener("click", () => navigate("aktuelles"));
      return;
    }
    const k = d.kennzahlen || {};
    const posten = d.offene_posten || [];
    const nachfassen = d.nachfassen || [];
    const rechnungen = d.rechnungen || [];
    const angebote = d.angebote || [];
    const belege = d.belege || [];
    const VORSCHAU = 6;   // wie viele Zeilen pro Abschnitt, Rest hinter "Alle"

    const parts = [];
    parts.push(
      `<button class="btn-sm btn-ghost" id="back-db" style="margin-bottom:10px">← Aktionen</button>` +
      `<div style="display:flex;align-items:center;justify-content:space-between;margin:4px 4px 14px">
         <h1 style="font-size:22px;margin:0">Buchhaltung</h1>
         ${isInhaber ? `<button class="btn-sm btn-ghost" id="bu-pruefen" style="padding:8px 12px">🔄 Zahlungen</button>` : ""}
       </div>`);

    // Kennzahlen zuerst: die eine Zahl, die der Chef morgens wissen will.
    // Die Frist steht dran, inklusive Herkunft — bei "standard" ist sie
    // geraten, und dann soll die Anzeige das auch zugeben.
    const zielText = d.zahlungsziel_tage === 0
      ? "sofort fällig"
      : `über ${d.zahlungsziel_tage} Tage`;
    parts.push(
      `<div class="geld-grid">
         ${geldKpi("Offen", k.offen_eur, `${k.offen_anzahl || 0} Rechnung(en)`, "")}
         ${geldKpi("Überfällig", k.ueberfaellig_eur,
                   `${k.ueberfaellig_anzahl || 0} ${zielText}`,
                   (k.ueberfaellig_anzahl || 0) > 0 ? "danger" : "")}
         ${geldKpi("Bezahlt (30 T)", k.bezahlt_30t_eur, `${k.bezahlt_30t_anzahl || 0} Zahlung(en)`, "ok")}
       </div>`);
    if (d.zahlungsziel_quelle === "standard") {
      parts.push(
        `<p class="muted" style="margin:-8px 6px 14px;font-size:12px">Zahlungsziel geschätzt (${d.zahlungsziel_tage} Tage) — in Lexware ist keins hinterlegt.</p>`);
    }

    if (isInhaber) {
      parts.push(
        `<div style="display:flex;flex-wrap:wrap;gap:8px;margin:0 4px 16px">
           <button class="btn-sm" id="bu-rechnung-neu" style="flex:1 1 30%;padding:12px 8px">+ Rechnung</button>
           <button class="btn-sm btn-ghost" id="bu-angebot-neu" style="flex:1 1 30%;padding:12px 8px">+ Angebot</button>
           <button class="btn-sm btn-ghost" id="bu-beleg-neu" style="flex:1 1 30%;padding:12px 8px">📄 Beleg</button>
         </div>`);
    } else {
      parts.push(
        `<button class="btn-sm" id="bu-beleg-neu" style="width:100%;margin-bottom:16px;padding:12px 8px">📄 Beleg erfassen</button>`);
    }

    // Offene Posten = die eigentliche Arbeit. Ueberfaellige stehen oben,
    // Entwuerfe ("liegt noch hier") sind extra markiert.
    if (posten.length) {
      parts.push(`<div class="section-title">Offene Posten (${posten.length})</div>`);
      parts.push(`<div class="card">${posten.map((p) => postenRow(p, isInhaber)).join("")}</div>`);
    }

    if (nachfassen.length) {
      parts.push(`<div class="section-title">Angebote ohne Rückmeldung (${nachfassen.length})</div>`);
      parts.push(`<div class="card">${nachfassen.map((n) =>
        rowPillLink(n.kunde, `${fmtEur(n.betrag_eur)} · seit ${n.tage} Tagen`,
                    "nachfassen", "warn", n.lexware_link) +
        (isInhaber
          ? `<div style="margin:-4px 0 10px"><button class="btn-sm btn-ghost" data-erinnern="nachfass" data-id="${esc(n.id)}" style="padding:7px 12px">✉️ Nachfassen</button></div>`
          : "")).join("")}</div>`);
    }

    parts.push(abschnitt("Rechnungen", rechnungen, VORSCHAU, "bu-alle-rechnungen",
      (x) => rowPillLink(x.kunde + (x.nummer ? " · " + x.nummer : ""),
                         `${x.betrag} · ${x.zeit}`, x.status, x.pill, x.lexware_link),
      "Noch keine Rechnungen"));
    parts.push(abschnitt("Angebote", angebote, VORSCHAU, "bu-alle-angebote",
      (x) => rowPillLink(x.kunde, `${x.betrag} · ${x.zeit}`, x.status, x.pill, x.lexware_link),
      "Noch keine Angebote"));
    parts.push(abschnitt("Belege", belege, VORSCHAU, "",
      belegRow, "Noch keine Belege — Quittungen einfach abfotografieren"));

    // Ausgaben kommen aus Lexware (zwei Aufrufe) und werden deshalb
    // nachgeladen — der Bereich soll nicht auf eine fremde API warten.
    parts.push(
      `<div class="card" id="bu-ausgaben"><h2>Ausgaben</h2>
         <div class="loading" style="padding:8px 0">Lädt aus Lexware …</div>
       </div>`);

    App.view.innerHTML = parts.join("");
    ladeAusgaben();
    document.getElementById("back-db").addEventListener("click", () => navigate("aktuelles"));
    const on = (id, fn) => { const el = document.getElementById(id); if (el) el.addEventListener("click", fn); };
    on("bu-beleg-neu", showBelegUpload);
    on("bu-rechnung-neu", () => { App.lastScreen = "buchhaltung"; showRechnungForm(); });
    on("bu-angebot-neu", () => { App.lastScreen = "buchhaltung"; showAngebotForm(); });
    on("bu-alle-rechnungen", () => navigate("rechnungen_page"));
    on("bu-alle-angebote", () => navigate("angebote_page"));
    document.querySelectorAll("[data-erinnern]").forEach((b) =>
      b.addEventListener("click", () => zeigeErinnerung(b.dataset.erinnern, b.dataset.id)));
    on("bu-pruefen", async () => {
      const btn = document.getElementById("bu-pruefen");
      const orig = btn.textContent;
      btn.disabled = true; btn.textContent = "Prüfe …";
      const r = await api("/app/api/rechnungen/pruefen", { method: "POST", body: "{}" });
      const j = r ? await r.json().catch(() => null) : null;
      btn.disabled = false; btn.textContent = orig;
      if (j && j.ok) {
        if ((j.bezahlt || 0) > 0) {
          toast(`✓ ${j.bezahlt} Rechnung(en) als bezahlt markiert`);
          navigate("buchhaltung", { mode: "none" });
        } else {
          alert(`Geprüft: ${j.geprueft || 0} offene Rechnung(en) — keine neuen Zahlungen.`);
        }
      } else { alert((j && j.error) || "Konnte nicht prüfen."); }
    });
  },

  async team() {
    const res = await api("/app/api/team");
    if (res && !res.ok) { App.view.innerHTML = errorScreen("Team konnte nicht geladen werden."); return; }
    const d = res && res.ok ? await res.json() : { team: [] };
    // Team verwalten (aktivieren, krank/Urlaub) und Rechte vergeben
    // sind getrennte Rechte — Anzeige folgt dem Server.
    const isInhaber = can("team.fuehren");
    const darfRechte = can("team.rechte");
    const cards = (d.team || []).map((e) => {
      const tags = [];
      if (e.abwesend_heute) tags.push(`<span class="pill danger">${e.abwesend_heute === "krank" ? "krank" : "abwesend"}</span>`);
      if (!e.is_active) tags.push(`<span class="pill">inaktiv</span>`);
      if (e.kalender_verbunden) tags.push(`<span class="pill ok">Kalender</span>`);
      if (e.app_verbunden) tags.push(`<span class="pill ok">App-Zugang</span>`);
      else if (!e.is_inhaber) tags.push(`<span class="pill warn">kein Zugang</span>`);
      const up = (e.kommende_abwesenheiten || []).map((a) =>
        `<div class="sub">${a.typ === "urlaub" ? "Urlaub" : a.typ}: ${esc(a.von)}–${esc(a.bis)}</div>`).join("");
      const skills = (e.skills || []).length ? `<div class="sub">${(e.skills || []).map(esc).join(", ")}</div>` : "";
      const akt = e.aktivitaet_30t || {};
      const aktTotal = (akt.logins || 0) + (akt.diktate || 0) + (akt.assistent || 0);
      const aktLine = (isInhaber && aktTotal)
        ? `<div class="sub" style="margin-top:2px">📊 30 Tage: ${akt.logins || 0}× aktiv · ${akt.diktate || 0} Diktate · ${akt.assistent || 0} Q-Befehle</div>` : "";
      let actions = "";
      if (isInhaber && !e.is_inhaber) {
        actions = `<button class="btn-sm btn-ghost" data-act="toggle" data-slug="${esc(e.slug)}" data-active="${e.is_active ? "1" : "0"}">${e.is_active ? "Deaktivieren" : "Aktivieren"}</button>`;
      }
      // Rolle als Chip + Einstieg in die Feinjustierung.
      const rechteLine = (darfRechte && !e.is_inhaber)
        ? `<div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
             <span class="pill">${esc(ROLLEN_LABEL[e.rolle] || e.rolle || "")}</span>
             <button class="btn-sm btn-ghost" data-act="rechte" data-slug="${esc(e.slug)}" style="padding:6px 10px">Rechte</button>
           </div>`
        : "";
      // Inhaber-Aktions-Zeile: Krank/Urlaub melden, oder bei laufender
      // Abwesenheit "Wieder da". Aktiv nur fuer aktive Mitarbeiter.
      let absenceActions = "";
      if (isInhaber && e.is_active) {
        if (e.abwesend_heute) {
          absenceActions = `<button class="btn-sm" data-act="zurueck" data-slug="${esc(e.slug)}" style="padding:6px 10px">Wieder da</button>`;
        } else {
          absenceActions =
            `<button class="btn-sm btn-ghost" data-act="absence" data-slug="${esc(e.slug)}" data-name="${esc(e.name)}" data-typ="krank" style="padding:6px 10px">Krankmelden</button>` +
            `<button class="btn-sm btn-ghost" data-act="absence" data-slug="${esc(e.slug)}" data-name="${esc(e.name)}" data-typ="urlaub" style="padding:6px 10px">Urlaub</button>`;
        }
      }
      return `<div class="card">
        <div class="row"><div><div><b>${esc(e.name)}</b>${e.is_inhaber ? " · Inhaber" : (e.job_title ? " · " + esc(e.job_title) : "")}</div>${skills}${up}${aktLine}</div><div>${actions}</div></div>
        <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">${tags.join("")}</div>
        ${rechteLine}
        ${absenceActions ? `<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">${absenceActions}</div>` : ""}
      </div>`;
    }).join("");
    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-mehr" style="margin-bottom:10px">← Zurück</button>` +
      `<div style="display:flex;align-items:center;justify-content:space-between;margin:4px 4px 14px">
        <div class="section-title" style="margin:0">Team (${(d.team || []).length})</div>` +
       (isInhaber ? `<button class="btn-sm" id="team-new-btn" style="padding:8px 14px">+ Mitarbeiter</button>` : "") +
       `</div>` +
      (cards || emptyRow("Keine Mitarbeiter"));
    document.getElementById("back-mehr").addEventListener("click", () => navigate("mehr"));
    const newBtn = document.getElementById("team-new-btn");
    if (newBtn) newBtn.addEventListener("click", showNewEmployeeForm);
    document.querySelectorAll('[data-act="toggle"]').forEach((b) =>
      b.addEventListener("click", async () => {
        b.disabled = true;
        const res = await api(`/app/api/team/${encodeURIComponent(b.dataset.slug)}/aktiv`,
          { method: "POST", body: JSON.stringify({ active: b.dataset.active !== "1" }) });
        if (res && res.ok) navigate("team"); else { b.disabled = false; alert("Aktion fehlgeschlagen."); }
      }));
    document.querySelectorAll('[data-act="rechte"]').forEach((b) =>
      b.addEventListener("click", () => showTeamRechte(b.dataset.slug)));
    // Krank/Urlaub-Buttons → kleines Date-Picker-Dialog
    document.querySelectorAll('[data-act="absence"]').forEach((b) =>
      b.addEventListener("click", () => showAbsenceDialog(b.dataset.slug, b.dataset.name, b.dataset.typ)));
    // Zurueck-Button → einfach senden mit confirm
    document.querySelectorAll('[data-act="zurueck"]').forEach((b) =>
      b.addEventListener("click", async () => {
        if (!confirm("Mitarbeiter als zurueck markieren?")) return;
        b.disabled = true;
        const res = await api(`/app/api/team/${encodeURIComponent(b.dataset.slug)}/zurueck`,
          { method: "POST", body: "{}" });
        if (res && res.ok) navigate("team"); else { b.disabled = false; alert("Konnte nicht aktualisieren."); }
      }));
  },

  async kunden() {
    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-mehr" style="margin-bottom:10px">← Zurück</button>` +
      `<div class="card"><input id="kunde-q" type="text" placeholder="Kundenname filtern …" autocomplete="off" /></div>` +
      `<div id="kunde-res"><div class="loading">Lädt …</div></div>`;
    document.getElementById("back-mehr").addEventListener("click", () => navigate("mehr"));
    const input = document.getElementById("kunde-q");
    const res = document.getElementById("kunde-res");
    let timer = null;

    async function runSearch(q) {
      res.innerHTML = `<div class="loading">Suche …</div>`;
      const r = await api("/app/api/kunden?q=" + encodeURIComponent(q));
      const d = r && r.ok ? await r.json() : {};
      const blocks = [];
      // Kundenstamm (Phase 6): echte Kunden-Einträge mit id; bei
      // namensgleichen Kunden liefert die API ein unterscheidendes
      // Merkmal (Mail > Tel-Endziffern > Erstkontakt) mit.
      const eintraege = (d.kunden || []).map((k) => ({
        name: (k.name || "").trim(), id: k.id || null, merkmal: k.merkmal || null,
      })).filter((k) => k.name);
      const seen = new Set(eintraege.map((k) => k.name.toLowerCase()));
      // Fallback für Alt-Zeilen ohne Kundenstamm-Eintrag (Phase 7 weg)
      [...(d.drive_kunden || [])].forEach((x) => {
        const n = (x.name || "").trim();
        if (n && !seen.has(n.toLowerCase())) { seen.add(n.toLowerCase()); eintraege.push({ name: n, id: null, merkmal: null }); }
      });
      [...(d.gespraeche || []), ...(d.angebote || []), ...(d.rechnungen || [])].forEach((x) => {
        const n = (x.kunde || "").trim();
        if (n && n !== "—" && !seen.has(n.toLowerCase())) { seen.add(n.toLowerCase()); eintraege.push({ name: n, id: null, merkmal: null }); }
      });
      eintraege.sort((a, b) => a.name.localeCompare(b.name, "de"));
      if (eintraege.length) blocks.push(`<div class="card"><h2>Kunden</h2>${eintraege.map((k) =>
        `<button class="row menu-item" data-kunde="${esc(k.name)}"${k.id ? ` data-kunde-id="${esc(k.id)}"` : ""}><span>👤 ${esc(k.name)}${k.merkmal ? ` <span class="sub">(${esc(k.merkmal)})</span>` : ""}</span><span class="sub">Profil ›</span></button>`).join("")}</div>`);
      if ((d.gespraeche || []).length && q) blocks.push(`<div class="card"><h2>Gespräche</h2>${d.gespraeche.map((x) => rowTap(x.kunde, x.briefing, x.zeit, x.id)).join("")}</div>`);
      if ((d.angebote || []).length && q) blocks.push(`<div class="card"><h2>Angebote</h2>${d.angebote.map((x) => row(x.kunde, x.betrag, x.zeit)).join("")}</div>`);
      if ((d.rechnungen || []).length && q) blocks.push(`<div class="card"><h2>Rechnungen</h2>${d.rechnungen.map((x) => row(x.kunde + (x.nummer ? " · " + esc(x.nummer) : ""), x.betrag, x.zeit)).join("")}</div>`);
      res.innerHTML = blocks.length ? blocks.join("") : `<p class="empty">${q ? `Nichts gefunden für „${esc(q)}".` : "Keine Kunden vorhanden."}</p>`;
      res.querySelectorAll("[data-kunde]").forEach((b) =>
        b.addEventListener("click", () => showKundenProfil(b.dataset.kunde, b.dataset.kundeId)));
      bindAufnahmen();
    }

    runSearch("");
    input.focus();
    input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => runSearch(input.value.trim()), 300);
    });
  },

  async anfragen() {
    const res = await api("/app/api/anfragen");
    if (res && !res.ok) { App.view.innerHTML = errorScreen("Anfragen konnten nicht geladen werden."); return; }
    const d = res && res.ok ? await res.json() : { items: [] };
    const items = d.items || [];
    // Aufteilung: offene oben, erledigte unten (collapsed). Erledigte-
    // Liste ist in der Klinik-/Buero-Realitaet sehr lang — separat sortiert.
    const open = items.filter((x) => !x.closed);
    const closed = items.filter((x) => x.closed);

    const renderItem = (x) => {
      const head = `<div class="row" style="align-items:flex-start">
        <div style="flex:1;min-width:0">
          <div><b>${esc(x.kunde_name || x.kunde_email)}</b></div>
          <div class="sub" style="margin-top:2px">${esc(x.subject)}</div>
          ${x.preview ? `<div class="sub" style="margin-top:4px;opacity:.8">${esc(x.preview)}</div>` : ""}
        </div>
        <div style="text-align:right;margin-left:8px;display:flex;flex-direction:column;gap:4px;align-items:flex-end">
          ${x.classification_label ? `<span class="pill ${x.classification_style || ""}">${esc(x.classification_label)}</span>` : ""}
          <span class="sub">${esc(x.updated_at_fmt)}</span>
        </div>
      </div>
      <div style="margin-top:6px"><span class="pill ${x.state_style || ""}">${esc(x.state_label)}</span></div>`;
      return `<button class="row menu-item" data-anfrage="${esc(x.id)}" style="display:block;text-align:left;padding:14px">${head}</button>`;
    };

    let html =
      `<button class="btn-sm btn-ghost" id="back-db" style="margin-bottom:10px">← Aktionen</button>` +
      `<h1 style="font-size:22px;margin:4px 4px 14px">Anfragen</h1>` +
      `<div class="card"><h2>Offen (${open.length})</h2>` +
      (open.length ? open.map(renderItem).join("") : emptyRow("Keine offenen Anfragen.")) +
      `</div>`;
    if (closed.length) {
      html += `<details class="card"><summary style="cursor:pointer;font-weight:600">Erledigt (${closed.length})</summary>` +
        closed.slice(0, 50).map(renderItem).join("") +
        `</details>`;
    }
    App.view.innerHTML = html;
    document.getElementById("back-db").addEventListener("click", () => navigate("aktuelles"));
    document.querySelectorAll("[data-anfrage]").forEach((b) =>
      b.addEventListener("click", () => showAnfrage(b.dataset.anfrage)));
  },

  async wissen() {
    const res = await api("/app/api/wissen");
    if (res && !res.ok) { App.view.innerHTML = errorScreen("Wissen konnte nicht geladen werden."); return; }
    const d = res && res.ok ? await res.json() : { eintraege: [], kategorien: [] };
    // Wissen anlegen/loeschen — serverseitig durchgesetzt, hier nur Anzeige.
    const isInhaber = can("wissen.pflegen");
    // nach Kategorie gruppieren
    const byCat = {};
    (d.eintraege || []).forEach((e) => { (byCat[e.kategorie_label] = byCat[e.kategorie_label] || []).push(e); });
    const groups = Object.keys(byCat).map((label) =>
      `<div class="card"><h2>${esc(label)}</h2>${byCat[label].map((e) =>
        `<div class="row"><div>${esc(e.text)}</div>${isInhaber ? `<button class="btn-sm btn-ghost" data-del-wissen="${e.id}">✕</button>` : ""}</div>`).join("")}</div>`).join("");
    const opts = (d.kategorien || []).map((k) => `<option value="${k.key}">${esc(k.label)}</option>`).join("");
    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-mehr" style="margin-bottom:10px">← Zurück</button>` +
      (groups || `<p class="empty">Noch keine Einträge.</p>`) +
      `<div class="card"><h2>Neuer Eintrag</h2>
         <select id="w-kat" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin-bottom:8px">${opts}</select>
         <textarea id="w-text" rows="3" placeholder="Wissen eingeben (z.B. Preise, Anfahrt, Öffnungszeiten) …" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;font-size:16px"></textarea>
         <button class="btn-sm" id="w-add" style="margin-top:8px;width:100%">Hinzufügen</button></div>`;
    document.getElementById("back-mehr").addEventListener("click", () => navigate("mehr"));
    document.getElementById("w-add").addEventListener("click", async () => {
      const kategorie = document.getElementById("w-kat").value;
      const text = document.getElementById("w-text").value.trim();
      if (text.length < 3) { alert("Bitte etwas mehr Text eingeben."); return; }
      const r = await api("/app/api/wissen", { method: "POST", body: JSON.stringify({ kategorie, text }) });
      if (r && r.ok) navigate("wissen"); else alert("Konnte nicht speichern.");
    });
    document.querySelectorAll("[data-del-wissen]").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!confirm("Eintrag löschen?")) return;
        const r = await api(`/app/api/wissen/${b.dataset.delWissen}/loeschen`, { method: "POST", body: "{}" });
        if (r && r.ok) navigate("wissen"); else alert("Konnte nicht löschen.");
      }));
  },

  async material() {
    const res = await api("/app/api/material");
    const d = res && res.ok ? await res.json() : { items: [] };
    // Materialkatalog pflegen — serverseitig durchgesetzt, hier nur Anzeige.
    const isInhaber = can("material.verwalten");
    const items = d.items || [];
    const active = items.filter((m) => m.aktiv);
    const inactive = items.filter((m) => !m.aktiv);

    const render = (m) =>
      `<div class="card">
         <div class="row" style="align-items:flex-start">
           <div style="flex:1;min-width:0">
             <div><b>${esc(m.name)}</b>${m.lieferant ? " · " + esc(m.lieferant) : ""}</div>
             ${m.notes ? `<div class="sub" style="margin-top:2px">${esc(m.notes)}</div>` : ""}
             <div class="sub" style="margin-top:4px">${esc(String(m.standard_menge))} ${esc(m.einheit)}</div>
           </div>
         </div>
         <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
           ${m.bestell_link && m.aktiv ? `<button class="btn-sm" data-mat-order="${esc(m.id)}" data-link="${esc(m.bestell_link)}">🛒 Bestellen</button>` : ""}
           ${isInhaber ? `<button class="btn-sm btn-ghost" data-mat-toggle="${esc(m.id)}">${m.aktiv ? "Deaktivieren" : "Aktivieren"}</button>` : ""}
         </div>
       </div>`;

    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-mehr" style="margin-bottom:10px">← Zurück</button>` +
      `<div style="display:flex;align-items:center;justify-content:space-between;margin:4px 4px 14px">
         <h1 style="font-size:22px;margin:0">Material</h1>
         <div style="display:flex;gap:6px">
           <button class="btn-sm btn-ghost" id="mat-verlauf-btn" style="padding:8px 12px">🧾 Verlauf</button>
           ${isInhaber ? `<button class="btn-sm" id="mat-new-btn" style="padding:8px 12px">+ Neu</button>` : ""}
         </div>
       </div>` +
      `<div class="section-title">Aktiv (${active.length})</div>` +
      (active.length ? active.map(render).join("") : emptyRow("Noch kein aktives Material.")) +
      (inactive.length ? `<details class="card"><summary style="cursor:pointer;font-weight:600">Inaktiv (${inactive.length})</summary>${inactive.map(render).join("")}</details>` : "");

    document.getElementById("back-mehr").addEventListener("click", () => navigate("mehr"));
    document.getElementById("mat-verlauf-btn").addEventListener("click", showMaterialBestellungen);
    const newBtn = document.getElementById("mat-new-btn");
    if (newBtn) newBtn.addEventListener("click", showNewMaterialForm);
    document.querySelectorAll("[data-mat-toggle]").forEach((b) =>
      b.addEventListener("click", async () => {
        b.disabled = true;
        const r = await api(`/app/api/material/${b.dataset.matToggle}/toggle`,
          { method: "POST", body: "{}" });
        if (r && r.ok) navigate("material"); else { b.disabled = false; alert("Konnte nicht ändern."); }
      }));
    // Bestellen: Link sofort im Klick-Gesture öffnen (kein Popup-Blocker),
    // Bestellung im Hintergrund protokollieren.
    document.querySelectorAll("[data-mat-order]").forEach((b) =>
      b.addEventListener("click", () => {
        if (b.dataset.link) window.open(b.dataset.link, "_blank", "noopener");
        b.disabled = true; b.textContent = "✓ Bestellt";
        api(`/app/api/material/${b.dataset.matOrder}/bestellen`, { method: "POST", body: "{}" })
          .catch(() => {});
      }));
  },

  async visualisierung() {
    const res = await api("/app/api/visualisierungen");
    const d = res && res.ok ? await res.json() : { visualisierungen: [] };
    const inputStyle = "width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px";
    const recent = (d.visualisierungen || []).map((v) => {
      const label = v.prompt || "Visualisierung";
      if (v.fertig) {
        return `<a class="row" href="/app/api/visualisierungen/${esc(v.id)}/bild" target="_blank" rel="noopener" style="text-decoration:none;color:inherit"><div><div>${esc(label)}</div><div class="sub">${esc(v.zeit)}</div></div><span class="pill ok">ansehen ›</span></a>`;
      }
      const pill = v.status === "failed" ? `<span class="pill danger">fehlgeschlagen</span>` : `<span class="pill warn">${esc(v.status)}</span>`;
      return `<div class="row"><div><div>${esc(label)}</div><div class="sub">${esc(v.zeit)}</div></div>${pill}</div>`;
    }).join("");

    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-mehr" style="margin-bottom:10px">← Zurück</button>` +
      `<h1 style="font-size:22px;margin:4px 4px 6px">Visualisierung</h1>` +
      `<p class="muted" style="margin:0 4px 14px">Foto eines Raums/Objekts hochladen und beschreiben, was verändert werden soll — die KI rendert eine fotorealistische Vorschau.</p>` +
      `<div class="card">
         <label class="sub">Foto (JPEG/PNG, max 15 MB)</label>
         <input type="file" id="viz-file" accept="image/jpeg,image/png" capture="environment" style="${inputStyle}" />
         <label class="sub">Was soll verändert werden?</label>
         <textarea id="viz-prompt" rows="3" placeholder="z.B. Wände in warmem Grau streichen, Eichenparkett verlegen" style="${inputStyle};font-family:inherit"></textarea>
         <button class="btn-sm" id="viz-go" style="width:100%;margin-top:4px" disabled>Visualisierung erstellen</button>
         <p class="muted" id="viz-status" style="margin-top:12px;min-height:20px"></p>
       </div>
       <div id="viz-result"></div>` +
      (recent ? `<div class="card"><h2>Bisherige</h2>${recent}</div>` : "");

    // Visualisierung wird aus dem Q-Aktionsmenü gestartet → zurück in den Chat.
    document.getElementById("back-mehr").addEventListener("click", () => navigate("assistent"));
    const fileEl = document.getElementById("viz-file");
    const promptEl = document.getElementById("viz-prompt");
    const goBtn = document.getElementById("viz-go");
    const statusEl = document.getElementById("viz-status");
    const resultEl = document.getElementById("viz-result");

    const refresh = () => {
      const f = fileEl.files && fileEl.files[0];
      goBtn.disabled = !(f && promptEl.value.trim().length >= 5);
    };
    fileEl.addEventListener("change", () => {
      const f = fileEl.files && fileEl.files[0];
      if (f && ["image/jpeg", "image/png"].indexOf(f.type) === -1) {
        statusEl.textContent = "Nur JPEG oder PNG."; fileEl.value = "";
      } else if (f && f.size > 15 * 1024 * 1024) {
        statusEl.textContent = `Foto zu groß (${Math.round(f.size / 1024 / 1024)} MB, max 15 MB).`; fileEl.value = "";
      } else {
        statusEl.textContent = "";
      }
      refresh();
    });
    promptEl.addEventListener("input", refresh);

    goBtn.addEventListener("click", async () => {
      const f = fileEl.files && fileEl.files[0];
      const prompt = promptEl.value.trim();
      if (!f || prompt.length < 5) return;
      goBtn.disabled = true;
      resultEl.innerHTML = "";
      statusEl.textContent = "Rendert das Bild … (ca. 10–20 Sek)";
      let res2;
      try {
        res2 = await fetch("/app/api/visualisierungen?prompt=" + encodeURIComponent(prompt), {
          method: "POST",
          headers: { "X-CSRF-Token": App.me.csrf, "Content-Type": f.type },
          body: f,
        });
      } catch (e) {
        statusEl.textContent = "Netzwerkfehler. Bitte erneut versuchen.";
        goBtn.disabled = false; return;
      }
      if (res2.status === 303 || res2.status === 401 || res2.redirected) { location.href = "/app/login"; return; }
      let j = null;
      try { j = await res2.json(); } catch (e) {}
      if (res2.ok && j && j.ok) {
        statusEl.textContent = "";
        resultEl.innerHTML =
          `<div class="card"><h2>✓ Fertig</h2>
             <img src="${esc(j.bild_url)}" alt="Visualisierung" style="width:100%;border-radius:10px;margin-top:8px" />
             <a class="btn-sm" href="${esc(j.bild_url)}" target="_blank" rel="noopener" style="display:block;text-align:center;width:100%;margin-top:10px;text-decoration:none">In voller Größe öffnen</a>
           </div>
           <button class="btn-sm btn-ghost" id="viz-again" style="width:100%;margin-top:8px">Weitere Visualisierung</button>`;
        document.getElementById("viz-again").addEventListener("click", () => navigate("visualisierung"));
      } else {
        statusEl.textContent = (j && j.error) || "Konnte kein Bild erstellen. Bitte erneut versuchen.";
        goBtn.disabled = false;
      }
    });
  },

  async mein_kalender() {
    const res = await api("/app/api/mein-kalender");
    const d = res && res.ok ? await res.json() : null;
    if (!d || !d.ok) {
      App.view.innerHTML =
        `<button class="btn-sm btn-ghost" id="back-mehr" style="margin-bottom:10px">← Zurück</button>` +
        `<div class="card"><p class="empty">Konnte den Status nicht laden.</p></div>`;
      document.getElementById("back-mehr").addEventListener("click", () => navigate("mehr"));
      return;
    }

    const zeile = (key, label, hinweis) => {
      const st = (d.status || {})[key] || {};
      const knopf = st.verbunden
        ? `<button class="btn-sm btn-ghost" data-trennen="${key}" style="padding:6px 12px">Trennen</button>`
        : `<button class="btn-sm" data-verbinden="${key}" style="padding:6px 12px">Verbinden</button>`;
      return `<div class="card">
        <div class="row" style="display:block">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:10px">
            <div><b>${esc(label)}</b>${st.verbunden ? ` <span class="pill ok">verbunden</span>` : ""}</div>
            <div style="flex-shrink:0">${knopf}</div>
          </div>
          ${st.konto ? `<div class="sub">${esc(st.konto)}</div>` : ""}
          <div class="sub" style="margin-top:6px">${esc(hinweis)}</div>
        </div>
      </div>`;
    };

    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-mehr" style="margin-bottom:10px">← Zurück</button>` +
      `<h1 style="font-size:22px;margin:4px 4px 4px">Mein Kalender</h1>` +
      `<div class="sub" style="margin:0 4px 14px">Verbinde deinen eigenen Kalender, damit Termine bei dir landen und niemand dich doppelt verplant.</div>` +
      zeile("google", "Google Kalender", d.hinweis_google || "") +
      zeile("microsoft", "Outlook", d.hinweis_microsoft || "");

    document.getElementById("back-mehr").addEventListener("click", () => navigate("mehr"));

    document.querySelectorAll("[data-verbinden]").forEach((b) =>
      b.addEventListener("click", () => {
        // Popup SYNCHRON im Klick oeffnen (sonst Popup-Blocker),
        // Ziel-URL nachreichen — gleiches Muster wie bei den
        // Betriebs-Verbindungen.
        const w = window.open("", "ga_oauth", "width=520,height=720");
        api("/app/api/mein-kalender/verbinden",
            { method: "POST", body: JSON.stringify({ provider: b.dataset.verbinden }) })
          .then((r) => (r && r.ok ? r.json() : null))
          .then((j) => {
            if (j && j.ok && j.auth_url) {
              if (w) {
                w.location = j.auth_url;
                const iv = setInterval(() => {
                  if (w.closed) { clearInterval(iv); navigate("mein_kalender"); }
                }, 1000);
                setTimeout(() => clearInterval(iv), 300000);
              } else { window.location = j.auth_url; }
            } else {
              if (w) w.close();
              toast((j && j.error) || "Verbindung konnte nicht gestartet werden.", "err");
            }
          });
      }));

    document.querySelectorAll("[data-trennen]").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!confirm("Kalender wirklich trennen? Q kann dir dann keine Termine mehr eintragen.")) return;
        b.disabled = true;
        const r = await api("/app/api/mein-kalender/trennen",
          { method: "POST", body: JSON.stringify({ provider: b.dataset.trennen }) });
        if (r && r.ok) { toast("Getrennt"); navigate("mein_kalender"); }
        else { b.disabled = false; toast("Konnte nicht trennen.", "err"); }
      }));
  },

  async einstellungen() {
    // Übersicht: die Einstellungen sind thematisch auf Unterseiten
    // verteilt (App / Betrieb / Verbindungen / System) statt auf einer
    // langen Sammelseite zu liegen.
    // Einstellungen schreiben — serverseitig durchgesetzt, hier nur Anzeige.
    const isInhaber = can("einstellungen.verwalten");
    const item = (go, ico, label, sub) =>
      `<button class="row menu-item" data-go="${go}"><span>${ico} ${esc(label)}<span class="sub" style="display:block">${esc(sub)}</span></span><span class="sub">›</span></button>`;
    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-mehr" style="margin-bottom:10px">← Zurück</button>` +
      `<h1 style="font-size:22px;margin:4px 4px 14px">Einstellungen</h1>` +
      `<div class="card">` +
        item("einstellungen_app", "📱", "App-Einstellungen", "Farbe & Darstellung") +
        item("einstellungen_betrieb", "🏢", "Betrieb",
          isInhaber ? "Firmendaten, Adresse, Logo & Website" : "Firmendaten & Adresse") +
        item("einstellungen_automatisierung", "🎚️", "Automatisierung",
          isInhaber ? "Wie selbstständig Q handeln darf" : "Was Q selbst erledigt") +
        (isInhaber ? item("einstellungen_verbindungen", "🔌", "Verbindungen", "Google, Outlook, Lexware") : "") +
        item("einstellungen_system", "ℹ️", "System", "Paket, Funktionen, Daten-Speicherung") +
      `</div>`;
    document.getElementById("back-mehr").addEventListener("click", () => navigate("mehr"));
    App.view.querySelectorAll("[data-go]").forEach((b) =>
      b.addEventListener("click", () => navigate(b.dataset.go)));
  },

  async einstellungen_automatisierung() {
    // Pro Funktion einstellen, wie selbstständig Q handeln darf:
    // manuell (gar nicht) · assistiert (fragt vorher) · automatisch (macht
    // direkt). Registry + Semantik liegen im Backend
    // (core/features/automations.py) — hier wird nur gerendert, was
    // /app/api/automatisierung liefert. Kein Feature-Wissen im Frontend.
    // Automatisierung schreiben — serverseitig durchgesetzt, hier nur Anzeige.
    const isInhaber = can("einstellungen.verwalten");
    const res = await api("/app/api/automatisierung");
    const d = res && res.ok ? await res.json() : null;
    const back =
      `<button class="btn-sm btn-ghost" id="back-einst" style="margin-bottom:10px">← Einstellungen</button>` +
      `<h1 style="font-size:22px;margin:4px 4px 6px">Automatisierung</h1>`;

    if (!d || !d.ok) {
      App.view.innerHTML = back +
        `<div class="card"><p class="empty">Konnte die Einstellungen nicht laden.</p></div>`;
      document.getElementById("back-einst").addEventListener("click", () => navigate("einstellungen"));
      return;
    }

    const modes = d.modes || [];
    const legende = modes
      .map((m) => `<b>${esc(m.label)}</b> — ${esc(m.description)}`)
      .join("<br>");

    // Nach group bündeln, Reihenfolge wie vom Backend geliefert.
    const groups = [];
    (d.automations || []).forEach((a) => {
      let g = groups.find((x) => x.name === a.group);
      if (!g) { g = { name: a.group, items: [] }; groups.push(g); }
      g.items.push(a);
    });

    const zeile = (a) => {
      const segs = modes.map((m) => {
        const on = a.mode === m.key;
        const erlaubt = (a.allowed_modes || []).indexOf(m.key) !== -1;
        return `<button type="button" class="${on ? "active" : ""}"` +
          ` data-key="${esc(a.key)}" data-mode="${esc(m.key)}"` +
          `${erlaubt && isInhaber ? "" : " disabled"}>${esc(m.label)}</button>`;
      }).join("");
      // Hinweis nur zeigen, wenn wirklich eine Stufe fehlt — sonst wäre er
      // bei den meisten Zeilen nur Rauschen.
      const fehlt = modes.length !== (a.allowed_modes || []).length;
      const note = fehlt && a.unsupported_hint
        ? `<p class="auto-note">${esc(a.unsupported_hint)}</p>` : "";
      return `<div class="autolist">
          <div class="auto-label">${esc(a.label)}</div>
          <div class="auto-desc">${esc(a.description)}</div>
          <div class="seg" data-seg="${esc(a.key)}">${segs}</div>
          ${note}
        </div>`;
    };

    App.view.innerHTML = back +
      `<p class="muted" style="margin:0 4px 14px;font-size:14px">${legende}</p>` +
      (isInhaber ? "" :
        `<div class="banner">Ändern darf das nur der Inhaber. Du siehst hier, was Q für euch selbst erledigt.</div>`) +
      groups.map((g) =>
        `<div class="section-title">${esc(g.name)}</div>` +
        `<div class="card">${g.items.map(zeile).join("")}</div>`
      ).join("") +
      `<p class="muted" style="margin:4px 4px 14px;font-size:12px">Gilt für den ganzen Betrieb, nicht nur für dein Gerät.</p>` +
      // Speicherleiste: erscheint erst, wenn wirklich etwas geändert wurde.
      // Bewusst kein Sofort-Speichern mehr — wer Q auf „automatisch" stellt,
      // gibt ihm die Hand fuer den ganzen Betrieb frei, und ein
      // versehentlicher Fingertipp soll das nicht tun.
      `<div class="save-bar" id="auto-savebar" hidden>
         <span class="save-bar-info" id="auto-saveinfo"></span>
         <button class="btn-sm btn-ghost" id="auto-verwerfen">Verwerfen</button>
         <button class="btn-sm" id="auto-speichern">Speichern</button>
       </div>`;

    document.getElementById("back-einst").addEventListener("click", () => navigate("einstellungen"));
    if (!isInhaber) return;

    // Merken, welche Stufen dauerhaft gesperrt sind (Feature/Registry) —
    // die bleiben auch nach dem Speichern deaktiviert.
    App.view.querySelectorAll(".seg button[disabled]").forEach((b) =>
      b.setAttribute("data-was-disabled", "1"));

    // gespeicherter Stand vom Server; dagegen wird „geändert" gemessen
    const gespeichert = {};
    (d.automations || []).forEach((a) => { gespeichert[a.key] = a.mode; });
    const gewaehlt = Object.assign({}, gespeichert);

    const savebar = document.getElementById("auto-savebar");
    const saveinfo = document.getElementById("auto-saveinfo");
    const saveBtn = document.getElementById("auto-speichern");
    const undoBtn = document.getElementById("auto-verwerfen");

    const geaendert = () =>
      Object.keys(gewaehlt).filter((k) => gewaehlt[k] !== gespeichert[k]);

    function markiere() {
      const offen = geaendert();
      savebar.hidden = offen.length === 0;
      App.view.classList.toggle("has-savebar", offen.length > 0);
      saveinfo.textContent = offen.length === 1
        ? "1 Änderung noch nicht gespeichert"
        : offen.length + " Änderungen noch nicht gespeichert";
      // Geänderte Zeilen sichtbar machen, damit man beim Speichern weiss,
      // was gleich passiert.
      App.view.querySelectorAll(".autolist").forEach((row) => {
        const seg = row.querySelector(".seg");
        if (seg) row.classList.toggle("dirty", offen.indexOf(seg.dataset.seg) !== -1);
      });
    }

    // Verlassen mit ungespeicherten Änderungen: nachfragen statt still
    // wegwerfen. navigate() ruft den Waechter vor jedem Wechsel auf.
    App.dirtyGuard = () => !geaendert().length ||
      confirm("Du hast Änderungen an der Automatisierung noch nicht gespeichert. Trotzdem verlassen?");

    App.view.querySelectorAll(".seg button").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (btn.disabled) return;
        const seg = btn.parentElement;
        seg.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        gewaehlt[btn.dataset.key] = btn.dataset.mode;
        markiere();
      });
    });

    undoBtn.addEventListener("click", () => {
      Object.keys(gespeichert).forEach((k) => { gewaehlt[k] = gespeichert[k]; });
      App.view.querySelectorAll(".seg").forEach((seg) => {
        seg.querySelectorAll("button").forEach((b) =>
          b.classList.toggle("active", b.dataset.mode === gespeichert[seg.dataset.seg]));
      });
      markiere();
    });

    saveBtn.addEventListener("click", async () => {
      const offen = geaendert();
      if (!offen.length) return;
      saveBtn.disabled = true; undoBtn.disabled = true;
      saveBtn.textContent = "Speichert …";
      // Einzeln statt in einem Rutsch: der Server validiert pro Funktion,
      // und so bleibt zuordenbar, welche Zeile gescheitert ist.
      const fehler = [];
      for (const key of offen) {
        const r = await api("/app/api/automatisierung",
          { method: "POST", body: JSON.stringify({ key: key, mode: gewaehlt[key] }) });
        const j = r && r.ok ? await r.json().catch(() => null) : null;
        if (j && j.ok) gespeichert[key] = gewaehlt[key];
        else fehler.push(((d.automations || []).find((a) => a.key === key) || {}).label
          || key + ": " + ((j && j.error) || "unbekannter Fehler"));
      }
      saveBtn.disabled = false; undoBtn.disabled = false;
      saveBtn.textContent = "Speichern";
      // Gescheiterte Zeilen auf den gespeicherten Stand zurueckstellen, damit
      // der Schalter nicht etwas anderes zeigt als der Server kennt.
      if (fehler.length) {
        Object.keys(gespeichert).forEach((k) => { gewaehlt[k] = gespeichert[k]; });
        App.view.querySelectorAll(".seg").forEach((seg) => {
          seg.querySelectorAll("button").forEach((b) =>
            b.classList.toggle("active", b.dataset.mode === gespeichert[seg.dataset.seg]));
        });
        alert("Nicht gespeichert: " + fehler.join(", "));
        markiere();
      } else {
        // Kurze Bestaetigung stehen lassen, erst danach die Leiste einziehen —
        // sonst verschwindet sie im selben Moment, in dem man speichert.
        saveinfo.textContent = "Gespeichert ✓";
        savebar.classList.add("ok");
        // Zeilen-Markierung sofort weg — gespeichert ist gespeichert; nur die
        // Bestaetigung in der Leiste bleibt noch kurz stehen.
        App.view.querySelectorAll(".autolist.dirty").forEach((r) => r.classList.remove("dirty"));
        setTimeout(() => { savebar.classList.remove("ok"); markiere(); }, 1400);
      }
    });

    markiere();
  },

  async einstellungen_app() {
    // App-Farbe (Tenant-weit, nur Inhaber änderbar). Die Darstellung-Karte
    // (hell/dunkel, pro Gerät) hängt beta.js nach dem Rendern an.
    const res = await api("/app/api/einstellungen");
    const d = res && res.ok ? await res.json() : { stammdaten: {} };
    const st = d.stammdaten || {};
    const isInhaber = !!d.is_inhaber;
    const brandColorVal = st.brand_color || "#0066cc";
    const colorField = isInhaber
      ? `<label class="sub">App-Farbe</label>
         <div style="display:flex;align-items:center;gap:10px;margin:4px 0 10px">
           <input type="color" id="set-brand_color-picker" value="${esc(brandColorVal)}"
             style="width:48px;height:40px;border:1px solid var(--line);border-radius:8px;padding:2px;cursor:pointer;background:none" />
           <input type="text" id="set-brand_color" value="${esc(brandColorVal)}" maxlength="7"
             style="flex:1;padding:12px;border:1px solid var(--line);border-radius:10px;font-size:15px" placeholder="#0066cc" />
         </div>
         <p class="muted" style="font-size:12px;margin:0 0 8px">Färbt Buttons und Bedienelemente — gilt für alle im Betrieb.</p>
         <button class="btn-sm" id="set-save-color" style="width:100%">Speichern</button>`
      : `<div class="row"><span>App-Farbe</span><span class="sub" style="display:flex;align-items:center;gap:6px"><span style="display:inline-block;width:14px;height:14px;border-radius:3px;background:${esc(brandColorVal)};border:1px solid var(--line)"></span>${esc(brandColorVal)}</span></div>
         <p class="muted" style="font-size:12px;margin:8px 0 0">Die Farbe legt der Inhaber fest. Hell/Dunkel wählst du unten — das gilt nur für dein Gerät.</p>`;
    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-einst" style="margin-bottom:10px">← Einstellungen</button>` +
      `<h1 style="font-size:22px;margin:4px 4px 14px">App-Einstellungen</h1>` +
      `<div class="card"><h2>App-Farbe</h2>${colorField}</div>`;
    document.getElementById("back-einst").addEventListener("click", () => navigate("einstellungen"));
    if (!isInhaber) return;
    const picker = document.getElementById("set-brand_color-picker");
    const hex = document.getElementById("set-brand_color");
    picker.addEventListener("input", () => {
      hex.value = picker.value;
      applyBrandColor(picker.value);
    });
    hex.addEventListener("input", () => {
      if (/^#[0-9a-fA-F]{6}$/.test(hex.value)) {
        picker.value = hex.value;
        applyBrandColor(hex.value);
      }
    });
    const saveBtn = document.getElementById("set-save-color");
    saveBtn.addEventListener("click", async () => {
      const colorHex = (hex.value || "").trim();
      if (!/^#[0-9a-fA-F]{6}$/.test(colorHex)) {
        alert("Bitte eine Farbe im Format #rrggbb angeben.");
        return;
      }
      saveBtn.disabled = true; saveBtn.textContent = "Speichere …";
      const r = await api("/app/api/einstellungen",
        { method: "POST", body: JSON.stringify({ brand_color: colorHex }) });
      const j = r && r.ok ? await r.json().catch(() => null) : null;
      if (j && j.ok) {
        applyBrandColor(colorHex);
        if (App.me && App.me.tenant) App.me.tenant.brand_color = colorHex;
        saveBtn.textContent = "✓ Gespeichert";
        setTimeout(() => { saveBtn.textContent = "Speichern"; saveBtn.disabled = false; }, 1500);
        return;
      }
      alert("Konnte nicht speichern: " + ((j && j.error) || "unbekannt"));
      saveBtn.disabled = false; saveBtn.textContent = "Speichern";
    });
  },

  async einstellungen_betrieb() {
    // Firmendaten + Werkstatt-Adresse (Inhaber editierbar, sonst
    // read-only). Die Logo-&-Website-Karte hängt beta.js an (Inhaber).
    const res = await api("/app/api/einstellungen");
    const d = res && res.ok ? await res.json() : { stammdaten: {} };
    const st = d.stammdaten || {};
    const isInhaber = !!d.is_inhaber;
    // OAuth/Voice-Felder sind hier bewusst nicht editierbar — der
    // Setup-Wizard bzw. Admin-UI bleibt dafür zuständig.
    const fld = (label, key, type = "text", hint = "") => {
      const val = st[key] || "";
      if (!isInhaber) {
        return `<div class="row"><span>${esc(label)}</span><span class="sub">${esc(val) || "—"}</span></div>`;
      }
      return `<label class="sub">${esc(label)}</label>
        <input type="${type}" id="set-${key}" value="${esc(val)}" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />${
        hint ? `<p class="muted" style="margin:-6px 0 8px;font-size:12px">${esc(hint)}</p>` : ""
      }`;
    };
    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-einst" style="margin-bottom:10px">← Einstellungen</button>` +
      `<h1 style="font-size:22px;margin:4px 4px 14px">Betrieb</h1>` +
      `<div class="card"><h2>Firma & Kontakt</h2>` +
        fld("Firmenname", "company_name") +
        fld("Branche", "branche", "text", "z.B. Heizungsbau, Elektro, Sanitär") +
        fld("Ansprechpartner", "contact_name") +
        fld("Kontakt-E-Mail", "contact_email", "email") +
        fld("Kontakt-Telefon", "contact_phone", "tel") +
      `</div>` +
      `<div class="card"><h2>Werkstatt-/Lager-Adresse</h2>
        <p class="muted" style="font-size:12px;margin-top:0">Wird für Fahrtzeit-Berechnung beim Terminbuchen benötigt (Start- und Endpunkt der täglichen Touren).</p>` +
        fld("Straße + Nr.", "heimat_strasse") +
        fld("PLZ", "heimat_plz") +
        fld("Ort", "heimat_ort") +
      `</div>` +
      (isInhaber ? `<button class="btn-sm" id="set-save" style="width:100%;margin-top:8px">Speichern</button>` : "");
    document.getElementById("back-einst").addEventListener("click", () => navigate("einstellungen"));
    const saveBtn = document.getElementById("set-save");
    if (saveBtn) {
      saveBtn.addEventListener("click", async () => {
        const keys = ["company_name", "branche", "contact_name", "contact_email", "contact_phone",
                      "heimat_strasse", "heimat_plz", "heimat_ort"];
        const body = {};
        keys.forEach((k) => { body[k] = (document.getElementById("set-" + k).value || "").trim(); });
        saveBtn.disabled = true; saveBtn.textContent = "Speichere …";
        const r = await api("/app/api/einstellungen",
          { method: "POST", body: JSON.stringify(body) });
        const j = r && r.ok ? await r.json().catch(() => null) : null;
        if (j && j.ok) {
          saveBtn.textContent = "✓ Gespeichert";
          setTimeout(() => { saveBtn.textContent = "Speichern"; saveBtn.disabled = false; }, 1500);
          return;
        }
        alert("Konnte nicht speichern: " + ((j && j.error) || "unbekannt"));
        saveBtn.disabled = false; saveBtn.textContent = "Speichern";
      });
    }
  },

  async einstellungen_system() {
    // Read-only: Paket, aktive Funktionen, Daten-Retention.
    const res = await api("/app/api/einstellungen");
    const d = res && res.ok ? await res.json() : { features: [] };
    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-einst" style="margin-bottom:10px">← Einstellungen</button>` +
      `<h1 style="font-size:22px;margin:4px 4px 14px">System</h1>` +
      `<div class="card"><h2>Verbundene Dienste</h2>
        <div class="row"><span>Funktionen aktiv</span><span class="sub">${(d.features || []).length}</span></div>
        ${(d.features || []).length ? `<div class="sub" style="margin-top:6px">${(d.features || []).map(esc).join(", ")}</div>` : ""}
        <div class="row"><span>Paket</span><span class="sub">${esc(d.package_tier || "—")}</span></div>
        <div class="row"><span>Daten-Retention</span><span class="sub">${esc(String(d.data_retention_days || ""))} Tage</span></div>
        <p class="muted" style="margin-top:8px">Microsoft, Google, Lexware und die Telefonnummer (Sipgate) verwalte über den Setup-Bereich auf gewerbeagent.de.</p>
      </div>`;
    document.getElementById("back-einst").addEventListener("click", () => navigate("einstellungen"));
  },

  async einstellungen_verbindungen() {
    // OAuth + Lexware (nur Inhaber) — Logik unverändert von der alten
    // Sammelseite hierher verschoben.
    // Verbindungen des Betriebs — serverseitig durchgesetzt, hier nur Anzeige.
    const isInhaber = can("einstellungen.verwalten");
    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-einst" style="margin-bottom:10px">← Einstellungen</button>` +
      `<h1 style="font-size:22px;margin:4px 4px 14px">Verbindungen</h1>` +
      (isInhaber ? `<div id="verbindungen-mount"></div>`
        : `<div class="card"><p class="empty">Verbindungen verwaltet der Inhaber.</p></div>`);
    document.getElementById("back-einst").addEventListener("click", () => navigate("einstellungen"));
    if (!isInhaber) return;

    // Gestapelt statt nebeneinander: bis zu drei Buttons pro Dienst sind auf
    // Handy-Breite breiter als die Zeile — als flex-shrink:0-Nachbar quetschten
    // sie den Titel auf Null Breite (Buchstaben brachen einzeln um).
    const row = (title, sub, btns) =>
      `<div class="row" style="display:block">
         <div>${title}</div>
         <div class="sub" style="word-break:break-word">${sub}</div>
         ${btns ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">${btns}</div>` : ""}
       </div>`;

    const startOAuth = (provider) => {
      // Popup SYNCHRON im Klick-Gesture oeffnen (sonst Popup-Blocker),
      // dann Ziel-URL setzen sobald die Authorize-URL da ist.
      const w = window.open("", "ga_oauth", "width=520,height=720");
      api("/app/api/oauth/start", { method: "POST", body: JSON.stringify({ provider }) })
        .then((r) => (r && r.ok ? r.json() : null))
        .then((j) => {
          if (j && j.ok && j.auth_url) {
            if (w) { w.location = j.auth_url; watchPopup(w); }
            else { window.location = j.auth_url; }
          } else {
            if (w) w.close();
            alert((j && j.error) || "Verbindung konnte nicht gestartet werden.");
          }
        })
        .catch(() => { if (w) w.close(); alert("Verbindung konnte nicht gestartet werden."); });
    };

    const watchPopup = (w) => {
      if (!w) return;
      const iv = setInterval(() => {
        if (w.closed) { clearInterval(iv); renderVerbindungen(); }
      }, 1000);
      setTimeout(() => clearInterval(iv), 300000); // Sicherheitsnetz: 5 min
    };

    const trennen = async (provider) => {
      const names = { google: "Google", microsoft: "Microsoft/Outlook", lexware: "Lexware" };
      if (!confirm(`${names[provider] || provider}-Verbindung wirklich trennen?`)) return;
      const r = await api("/app/api/verbindungen/trennen",
        { method: "POST", body: JSON.stringify({ provider }) });
      if (r && r.ok) renderVerbindungen(); else alert("Konnte nicht trennen.");
    };

    const saveLexware = async () => {
      const inp = document.getElementById("lexware-key");
      const msg = document.getElementById("lexware-msg");
      const btn = document.getElementById("lexware-save");
      const key = (inp.value || "").trim();
      if (key.length < 20) { msg.textContent = "Bitte einen gültigen Schlüssel eingeben."; return; }
      btn.disabled = true; btn.textContent = "Prüfe …"; msg.textContent = "";
      const r = await api("/app/api/lexware/verbinden",
        { method: "POST", body: JSON.stringify({ api_key: key }) });
      const j = r ? await r.json().catch(() => null) : null;
      btn.disabled = false; btn.textContent = "Schlüssel speichern";
      if (j && j.ok) { inp.value = ""; renderVerbindungen(); }
      else { msg.textContent = (j && j.error) || "Konnte nicht speichern."; }
    };

    const renderVerbindungen = async () => {
      const mount = document.getElementById("verbindungen-mount");
      if (!mount) return;
      mount.innerHTML = `<div class="card"><h2>Verbindungen</h2><p class="muted">Lädt …</p></div>`;
      const r = await api("/app/api/verbindungen");
      if (!r || !r.ok) {
        mount.innerHTML = `<div class="card"><h2>Verbindungen</h2><p class="muted">Konnte Verbindungen nicht laden.</p></div>`;
        return;
      }
      const v = await r.json();

      const g = v.google || {};
      const gScopes = [g.kalender ? "Kalender" : null, g.drive ? "Drive" : null].filter(Boolean).join(" + ");
      const gSub = g.connected
        ? `✓ ${esc(g.account || "verbunden")}${gScopes ? ` · ${gScopes}` : ""}`
        : "Kalender & Drive verbinden";
      const gTestBtns = g.connected
        ? `${g.kalender ? `<button class="btn-sm btn-ghost" data-test="kalender">Kalender testen</button>` : ""}${g.drive ? `<button class="btn-sm btn-ghost" data-test="drive">Drive testen</button>` : ""}`
        : "";
      const gBtns = (g.connected
        ? `<button class="btn-sm btn-ghost" data-oauth="google">Neu verbinden</button><button class="btn-sm btn-ghost" data-trennen="google">Trennen</button>`
        : `<button class="btn-sm" data-oauth="google">Verbinden</button>`) + gTestBtns;

      const m = v.microsoft || {};
      let mSub, mBtns;
      if (!m.available) { mSub = "Nicht verfügbar — bitte Support kontaktieren"; mBtns = ""; }
      else if (m.connected) {
        mSub = `✓ ${esc(m.account || "verbunden")}`;
        // Freemail-Postfach: Mails an neue Kunden landen damit oft im
        // Spam-Ordner — und zwar lautlos, es kommt keine Fehlermeldung
        // zurück. Lieber hier einmal warnen als später rätseln.
        if (m.freemail) {
          mSub += `<div style="margin-top:6px;padding:8px 10px;border-radius:8px;background:rgba(255,149,0,.12);font-size:12px;line-height:1.45">⚠️ Privates Freemail-Postfach. Mails an <b>neue</b> Kunden (Angebote, Rechnungen) landen damit häufig im Spam — ohne Fehlermeldung. Für den Echtbetrieb ein Postfach auf der <b>eigenen Domain</b> verbinden.</div>`;
        }
        mBtns = `<button class="btn-sm btn-ghost" data-oauth="microsoft">Neu verbinden</button><button class="btn-sm btn-ghost" data-trennen="microsoft">Trennen</button><button class="btn-sm btn-ghost" data-test="microsoft">Test-Mail</button>`;
      } else {
        mSub = "Outlook-Postfach & Kalender verbinden";
        mBtns = `<button class="btn-sm" data-oauth="microsoft">Verbinden</button>`;
      }

      const lx = v.lexware || {};
      const lxSub = lx.connected
        ? `✓ verbunden${lx.account ? ` · Org ${esc(lx.account)}` : ""}`
        : "Buchhaltung verbinden (API-Schlüssel)";
      const lxBtns = lx.connected
        ? `<button class="btn-sm btn-ghost" data-lexware="1">Schlüssel ändern</button><button class="btn-sm btn-ghost" data-trennen="lexware">Trennen</button><button class="btn-sm btn-ghost" data-test="lexware">Lexware testen</button>`
        : `<button class="btn-sm" data-lexware="1">Verbinden</button>`;

      mount.innerHTML = `<div class="card"><h2>Verbindungen</h2>
        <p class="muted" style="font-size:12px;margin-top:0">Verknüpfe deine Konten direkt hier — kein Umweg mehr über Telegram oder den Setup-Bereich.</p>
        ${row("📅 Google (Kalender + Drive)", gSub, gBtns)}
        ${row("✉️ Microsoft / Outlook", mSub, mBtns)}
        ${row("🧾 Lexware Office", lxSub, lxBtns)}
        <div id="lexware-form" style="display:none;margin-top:10px">
          <label class="sub">Lexware API-Schlüssel</label>
          <input type="text" id="lexware-key" placeholder="aus app.lexware.de → Profil → API-Keys" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 8px;font-size:16px" />
          <button class="btn-sm" id="lexware-save" style="width:100%">Schlüssel speichern</button>
          <p class="muted" id="lexware-msg" style="font-size:12px;margin-top:6px"></p>
        </div>
      </div>`;

      mount.querySelectorAll("[data-oauth]").forEach((b) =>
        b.addEventListener("click", () => startOAuth(b.getAttribute("data-oauth"))));
      mount.querySelectorAll("[data-trennen]").forEach((b) =>
        b.addEventListener("click", () => trennen(b.getAttribute("data-trennen"))));
      mount.querySelectorAll("[data-test]").forEach((b) =>
        b.addEventListener("click", async () => {
          const dienst = b.getAttribute("data-test");
          const orig = b.textContent;
          b.disabled = true; b.textContent = "Teste …";
          const r = await api(`/app/api/verbindungen/${encodeURIComponent(dienst)}/test`,
            { method: "POST", body: "{}" });
          const j = r ? await r.json().catch(() => null) : null;
          b.disabled = false; b.textContent = orig;
          if (j && j.ok) toast("✓ " + (j.detail || "Verbindung funktioniert."));
          else alert("✗ " + ((j && j.error) || "Test fehlgeschlagen."));
        }));
      mount.querySelectorAll("[data-lexware]").forEach((b) =>
        b.addEventListener("click", () => {
          const f = document.getElementById("lexware-form");
          f.style.display = f.style.display === "none" ? "block" : "none";
          if (f.style.display === "block") document.getElementById("lexware-key").focus();
        }));
      const lxSave = document.getElementById("lexware-save");
      if (lxSave) lxSave.addEventListener("click", saveLexware);
    };

    renderVerbindungen();
  },

  async formulare() {
    // Anfrage-Formular-Editor (Inhaber). Quelle der Wahrheit ist `state`;
    // Text-Inputs schreiben live in state -> ein struktureller Re-Render
    // (Feld hinzufügen/löschen/verschieben/Typwechsel) verliert nichts.
    const state = {
      // "auto": der Server liefert den Typ, den die Kunden dieses Betriebs
      // wirklich bekommen (Branche entscheidet). Sonst saesse ein Tischler
      // im Allgemein-Formular und wunderte sich, warum seine Aenderungen
      // beim Kunden nicht ankommen.
      typ: (App.formular && App.formular.typ) || "auto",
      title: "", subtitle: "", fields: [],
      fieldTypes: [], optionTypes: [], typen: [], dirty: false,
      previewUrl: "", aktivTyp: "",
      vorschlag: null,  // von Q gebauter Entwurf, noch nicht uebernommen
    };
    App.formular = state;

    const isOptionType = (t) => state.optionTypes.indexOf(t) !== -1;
    const normField = (f) => ({
      name: f.name || "", label: f.label || "", type: f.type || "text",
      required: !!f.required, placeholder: f.placeholder || "",
      options: Array.isArray(f.options) ? f.options.slice() : [],
    });
    const setMsg = (t, ok) => {
      const m = document.getElementById("f-msg");
      if (m) { m.textContent = t; m.style.color = ok ? "var(--ok,#1a7f37)" : "var(--err,#b42318)"; }
    };
    const inputStyle = "width:100%;padding:10px;border:1px solid var(--line);border-radius:10px;margin:4px 0 8px;font-size:16px";

    // ---- Vorschau -----------------------------------------------------
    // Gerendert wird auf dem Server mit DERSELBEN Funktion, die der Kunde
    // spaeter sieht (render_anfrage_form_html). Ein zweiter Renderer hier
    // wuerde vom Original wegdriften — und dann taeuscht die Vorschau.
    let vorschauTimer = null;
    const entwurf = () => ({
      title: (state.title || "").trim(),
      subtitle: (state.subtitle || "").trim(),
      fields: state.fields.map((f) => ({
        name: f.name || "", label: (f.label || "").trim(), type: f.type,
        required: !!f.required, placeholder: f.placeholder || "",
        options: f.options || [],
      })),
    });

    const setVorschauHinweis = (text) => {
      const box = document.getElementById("f-preview-note");
      if (box) box.textContent = text || "";
    };

    const renderVorschau = async (daten) => {
      const frame = document.getElementById("f-preview");
      if (!frame) return;
      const body = daten || entwurf();
      // Ein Feld ohne Bezeichnung wuerde der Server (zu Recht) ablehnen —
      // waehrend des Tippens ist das der Normalfall, kein Fehler.
      if (!body.fields.length || body.fields.some((f) => !f.label)) {
        setVorschauHinweis("Vorschau wartet — jedes Feld braucht noch eine Bezeichnung.");
        return;
      }
      setVorschauHinweis("Aktualisiere …");
      const r = await api(`/app/api/formulare/${encodeURIComponent(state.typ)}/vorschau`,
        { method: "POST", body: JSON.stringify(body) });
      const j = r ? await r.json().catch(() => null) : null;
      if (j && j.ok) { frame.srcdoc = j.html; setVorschauHinweis(""); }
      else setVorschauHinweis((j && j.error) || "Vorschau konnte nicht geladen werden.");
    };

    const vorschauSpaeter = () => {
      clearTimeout(vorschauTimer);
      vorschauTimer = setTimeout(() => renderVorschau(), 600);
    };

    // Jede Aenderung im Editor: merken, dass ungespeichert ist, und die
    // Vorschau nachziehen (gebuendelt, sonst ein Request je Tastendruck).
    const touch = () => { state.dirty = true; vorschauSpaeter(); };

    // ---- Q-Zeile ------------------------------------------------------
    const qErgebnis = () => document.getElementById("f-q-result");

    const qVerwerfen = () => {
      state.vorschlag = null;
      const box = qErgebnis();
      if (box) box.innerHTML = "";
      renderVorschau();
    };

    const qUebernehmen = () => {
      if (!state.vorschlag) return;
      state.title = state.vorschlag.title || state.title;
      state.subtitle = state.vorschlag.subtitle || "";
      state.fields = (state.vorschlag.fields || []).map(normField);
      state.vorschlag = null;
      state.dirty = true;   // Vorschau zieht shell() gleich selbst nach
      shell();  // Titel/Untertitel-Inputs neu befuellen
      setMsg("Übernommen — noch nicht gespeichert.", true);
    };

    const qFragen = async () => {
      const inp = document.getElementById("f-q-input");
      const btn = document.getElementById("f-q-go");
      const auftrag = ((inp && inp.value) || "").trim();
      if (auftrag.length < 3) { inp && inp.focus(); return; }
      const box = qErgebnis();
      btn.disabled = true; btn.textContent = "Q denkt nach …";
      if (box) box.innerHTML = `<p class="muted" style="font-size:13px;margin:8px 0 0">Q baut dein Formular um …</p>`;
      const r = await api(`/app/api/formulare/${encodeURIComponent(state.typ)}/q`,
        { method: "POST", body: JSON.stringify({ auftrag, ...entwurf() }) });
      const j = r ? await r.json().catch(() => null) : null;
      btn.disabled = false; btn.textContent = "Ändern lassen";
      if (!j || !j.ok) {
        if (box) box.innerHTML = `<p class="muted" style="font-size:13px;margin:8px 0 0;color:var(--err,#b42318)">${esc((j && j.error) || "Q konnte das nicht umbauen.")}</p>`;
        return;
      }
      state.vorschlag = { title: j.title || "", subtitle: j.subtitle || "", fields: j.fields || [] };
      if (inp) inp.value = "";
      if (box) {
        box.innerHTML =
          `<div style="margin-top:10px;padding:10px 12px;border-radius:10px;background:rgba(10,132,255,.10)">
             <p style="margin:0 0 8px;font-size:14px;line-height:1.45">${esc(j.erklaerung || "Vorschlag steht in der Vorschau.")}</p>
             <p class="muted" style="margin:0 0 10px;font-size:12px">Oben siehst du den Vorschlag. Übernehmen ändert nur den Entwurf — gespeichert wird erst mit „Speichern".</p>
             <div style="display:flex;gap:8px;flex-wrap:wrap">
               <button class="btn-sm" id="f-q-ok">Übernehmen</button>
               <button class="btn-sm btn-ghost" id="f-q-no">Verwerfen</button>
             </div>
           </div>`;
        document.getElementById("f-q-ok").addEventListener("click", qUebernehmen);
        document.getElementById("f-q-no").addEventListener("click", qVerwerfen);
      }
      // Vorschau zeigt sofort den Vorschlag — sehen statt lesen.
      renderVorschau(state.vorschlag);
    };

    const renderFields = () => {
      const wrap = document.getElementById("f-fields");
      if (!wrap) return;
      if (!state.fields.length) {
        wrap.innerHTML = `<div class="card"><p class="muted">Noch keine Felder. Füge unten ein Feld hinzu.</p></div>`;
        return;
      }
      wrap.innerHTML = state.fields.map((f, i) => {
        const typeOpts = state.fieldTypes.map((t) =>
          `<option value="${t.value}" ${t.value === f.type ? "selected" : ""}>${esc(t.label)}</option>`).join("");
        const optBlock = isOptionType(f.type)
          ? `<label class="sub">Optionen (eine pro Zeile)</label>
             <textarea class="f-opts" data-i="${i}" rows="3" style="${inputStyle};font-size:15px">${esc((f.options || []).join("\n"))}</textarea>`
          : "";
        const phBlock = (f.type === "text" || f.type === "textarea" || f.type === "tel")
          ? `<label class="sub">Platzhalter (optional)</label>
             <input class="f-ph" data-i="${i}" value="${esc(f.placeholder || "")}" style="${inputStyle};font-size:15px">`
          : "";
        return `<div class="card">
          <div class="row" style="margin-bottom:4px"><b>Feld ${i + 1}</b>
            <span style="display:flex;gap:6px">
              <button class="btn-sm btn-ghost f-up" data-i="${i}" ${i === 0 ? "disabled" : ""}>↑</button>
              <button class="btn-sm btn-ghost f-down" data-i="${i}" ${i === state.fields.length - 1 ? "disabled" : ""}>↓</button>
              <button class="btn-sm btn-ghost f-del" data-i="${i}">✕</button>
            </span>
          </div>
          <label class="sub">Bezeichnung (für Kunden sichtbar)</label>
          <input class="f-label" data-i="${i}" value="${esc(f.label || "")}" style="${inputStyle}">
          <label class="sub">Typ</label>
          <select class="f-type" data-i="${i}" style="${inputStyle}">${typeOpts}</select>
          ${optBlock}${phBlock}
          <label style="display:flex;align-items:center;gap:8px;margin-top:4px">
            <input type="checkbox" class="f-req" data-i="${i}" ${f.required ? "checked" : ""}> <span>Pflichtfeld</span>
          </label>
        </div>`;
      }).join("");

      const idx = (e) => +e.currentTarget.dataset.i;
      wrap.querySelectorAll(".f-label").forEach((el) => el.addEventListener("input", (e) => { state.fields[idx(e)].label = e.target.value; touch(); }));
      wrap.querySelectorAll(".f-ph").forEach((el) => el.addEventListener("input", (e) => { state.fields[idx(e)].placeholder = e.target.value; touch(); }));
      wrap.querySelectorAll(".f-opts").forEach((el) => el.addEventListener("input", (e) => { state.fields[idx(e)].options = e.target.value.split("\n").map((s) => s.trim()).filter(Boolean); touch(); }));
      wrap.querySelectorAll(".f-req").forEach((el) => el.addEventListener("change", (e) => { state.fields[idx(e)].required = e.target.checked; touch(); }));
      wrap.querySelectorAll(".f-type").forEach((el) => el.addEventListener("change", (e) => {
        const i = idx(e); state.fields[i].type = e.target.value;
        if (isOptionType(e.target.value) && !(state.fields[i].options || []).length) state.fields[i].options = [];
        touch(); renderFields();
      }));
      wrap.querySelectorAll(".f-up").forEach((el) => el.addEventListener("click", (e) => { const i = idx(e); if (i > 0) { const a = state.fields; const t = a[i - 1]; a[i - 1] = a[i]; a[i] = t; touch(); renderFields(); } }));
      wrap.querySelectorAll(".f-down").forEach((el) => el.addEventListener("click", (e) => { const i = idx(e); const a = state.fields; if (i < a.length - 1) { const t = a[i + 1]; a[i + 1] = a[i]; a[i] = t; touch(); renderFields(); } }));
      wrap.querySelectorAll(".f-del").forEach((el) => el.addEventListener("click", (e) => {
        const i = idx(e);
        if (state.fields.length <= 1) { alert("Mindestens ein Feld muss bleiben."); return; }
        if (confirm("Dieses Feld löschen?")) { state.fields.splice(i, 1); touch(); renderFields(); }
      }));
    };

    const addField = () => {
      state.fields.push({ name: "", label: "", type: "text", required: false, placeholder: "", options: [] });
      touch(); renderFields();
      const labels = document.querySelectorAll("#f-fields .f-label");
      if (labels.length) labels[labels.length - 1].focus();
    };

    const save = async () => {
      const btn = document.getElementById("f-save");
      if (state.fields.some((f) => !(f.label || "").trim())) { setMsg("Jedes Feld braucht eine Bezeichnung.", false); return; }
      btn.disabled = true; btn.textContent = "Speichere …"; setMsg("", true);
      const body = {
        title: (state.title || "").trim(), subtitle: (state.subtitle || "").trim(),
        fields: state.fields.map((f) => ({
          name: f.name || "", label: (f.label || "").trim(), type: f.type,
          required: !!f.required, placeholder: f.placeholder || "", options: f.options || [],
        })),
      };
      const r = await api(`/app/api/formulare/${encodeURIComponent(state.typ)}`,
        { method: "POST", body: JSON.stringify(body) });
      const j = r ? await r.json().catch(() => null) : null;
      btn.disabled = false; btn.textContent = "Speichern";
      if (j && j.ok) { state.dirty = false; await load(); setMsg("✓ Gespeichert.", true); }
      else { setMsg((j && j.error) || "Konnte nicht speichern.", false); }
    };

    const reset = async () => {
      if (!confirm("Formular wirklich auf den Standard zurücksetzen? Deine Anpassungen gehen verloren.")) return;
      const r = await api(`/app/api/formulare/${encodeURIComponent(state.typ)}/reset`,
        { method: "POST", body: JSON.stringify({}) });
      const j = r ? await r.json().catch(() => null) : null;
      if (j && j.ok) {
        state.title = j.title || ""; state.subtitle = j.subtitle || "";
        state.fields = (j.fields || []).map(normField); state.dirty = false;
        shell(); setMsg("✓ Auf Standard zurückgesetzt.", true);
      } else { setMsg((j && j.error) || "Konnte nicht zurücksetzen.", false); }
    };

    const switchTyp = async (typ) => {
      if (typ === state.typ) return;
      if (state.dirty && !confirm("Ungespeicherte Änderungen verwerfen und Typ wechseln?")) return;
      state.typ = typ; App.formular.typ = typ;
      await load();
    };

    const shell = () => {
      // Am aktiven Typ steht dran, dass DIESES Formular rausgeht — sonst
      // sieht man zwei gleichberechtigte Knoepfe und raet.
      const typPills = state.typen.map((t) =>
        `<button class="btn-sm ${t.value === state.typ ? "" : "btn-ghost"}" data-typ="${t.value}">${esc(t.label)}${t.value === state.aktivTyp ? " ✓" : ""}</button>`).join(" ");
      App.view.innerHTML =
        `<button class="btn-sm btn-ghost" id="back-mehr" style="margin-bottom:10px">← Zurück</button>` +
        `<h1 style="font-size:22px;margin:4px 4px 10px">Anfrage-Formular</h1>` +
        `<p class="muted" style="font-size:12px;margin:0 4px 12px">Das Formular, das deine Kunden per Mail bekommen. Oben siehst du es genau so, wie es bei ihnen ankommt — Änderungen gelten für neue Anfragen.</p>` +
        (state.typen.length > 1
          ? `<div class="card"><h2>Formular-Typ</h2>
               <div style="display:flex;gap:8px;flex-wrap:wrap">${typPills}</div>
               <p class="sub" style="margin:8px 0 0">${
                 state.typ === state.aktivTyp
                   ? "✓ Dieses Formular bekommen deine Kunden."
                   : "Dieses Formular wird aktuell <b>nicht</b> verschickt — deine Kunden bekommen das mit ✓ markierte (richtet sich nach deiner Branche)."
               }</p>
             </div>`
          : "") +
        // Vorschau zuerst: das Ergebnis steht vor den Reglern, nicht dahinter.
        `<div class="card">
           <div class="row" style="margin:0 0 8px"><h2 style="margin:0">So sieht es der Kunde</h2>
             <button class="btn-sm btn-ghost" id="f-preview-reload" style="padding:6px 10px">↻</button></div>
           <iframe id="f-preview" title="Vorschau des Kundenformulars" sandbox="allow-scripts"
             style="width:100%;height:460px;border:1px solid var(--line);border-radius:12px;background:#fff"></iframe>
           <p class="muted" id="f-preview-note" style="font-size:12px;margin:8px 0 0"></p>
         </div>` +
        // Q direkt unter der Vorschau: sagen, sehen, uebernehmen.
        `<div class="card">
           <h2 style="margin-top:0">Q ändern lassen</h2>
           <p class="sub" style="margin:0 0 8px">Sag in einem Satz, was anders sein soll — z.B. „frag noch nach der Raumgröße und mach Telefon zur Pflicht".</p>
           <textarea id="f-q-input" rows="2" placeholder="Was soll sich ändern?"
             style="${inputStyle};font-size:15px;resize:vertical"></textarea>
           <button class="btn-sm" id="f-q-go" style="width:100%">Ändern lassen</button>
           <div id="f-q-result"></div>
         </div>` +
        `<div class="card"><h2>Überschrift</h2>
          <label class="sub">Titel</label>
          <input id="f-title" value="${esc(state.title)}" style="${inputStyle}">
          <label class="sub">Untertitel</label>
          <input id="f-subtitle" value="${esc(state.subtitle)}" style="${inputStyle};margin-bottom:2px">
        </div>` +
        `<div id="f-fields"></div>` +
        `<button class="btn-sm btn-ghost" id="f-add" style="width:100%;margin:6px 0 14px">+ Feld hinzufügen</button>` +
        `<button class="btn-sm" id="f-save" style="width:100%">Speichern</button>` +
        `<button class="btn-sm btn-ghost" id="f-reset" style="width:100%;margin-top:8px">Auf Standard zurücksetzen</button>` +
        `<p class="muted" id="f-msg" style="text-align:center;margin-top:10px;font-size:13px"></p>` +
        `<div class="card" style="margin-top:14px">
           <h2 style="margin-top:0">Formular teilen</h2>
           <p class="sub" style="margin:0 0 8px">Vorschau-Link — zeigt das Formular, ohne dass etwas abgeschickt wird:</p>
           <!-- Die Adresse steht als umbrechender Text statt in einem
                schmalen readonly-Feld: auf Handybreite war von der URL nur
                ein Bruchteil lesbar. Farben ueber echte Theme-Tokens —
                vorher stand hier var(--bg2,#f8f8f8), und --bg2 gibt es im
                Stylesheet nicht: im Dunkelmodus hellgrauer Kasten mit
                fast weisser Schrift, also unlesbar. -->
           <div style="border:1px solid var(--line);border-radius:10px;background:var(--bg);padding:10px 12px;margin-bottom:8px">
             <span id="f-preview-url" style="display:block;color:var(--text);font-size:14px;line-height:1.5;word-break:break-all;font-family:ui-monospace,SFMono-Regular,Menlo,monospace">${esc(state.previewUrl)}</span>
           </div>
           <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
             <button class="btn-sm btn-ghost" id="f-copy-preview" style="flex:1">📋 Kopieren</button>
             <a class="btn-sm btn-ghost" id="f-open-preview" href="${esc(state.previewUrl)}" target="_blank" rel="noopener"
                style="flex:1;text-align:center;text-decoration:none;line-height:2.2">↗ Öffnen</a>
           </div>
           <button class="btn-sm btn-ghost" id="f-gen-link" style="width:100%">Kunden-Link generieren …</button>
           <p class="muted" style="font-size:12px;margin:8px 0 0">Generiert einen persönlichen Ausfüll-Link mit Ablaufdatum für einen bestimmten Kunden.</p>
         </div>`;
      document.getElementById("back-mehr").addEventListener("click", () => history.back());
      document.getElementById("f-preview-reload").addEventListener("click", () => renderVorschau());
      document.getElementById("f-q-go").addEventListener("click", qFragen);
      document.querySelectorAll("[data-typ]").forEach((b) => b.addEventListener("click", () => switchTyp(b.dataset.typ)));
      document.getElementById("f-title").addEventListener("input", (e) => { state.title = e.target.value; touch(); });
      document.getElementById("f-subtitle").addEventListener("input", (e) => { state.subtitle = e.target.value; touch(); });
      document.getElementById("f-add").addEventListener("click", addField);
      document.getElementById("f-save").addEventListener("click", save);
      document.getElementById("f-reset").addEventListener("click", reset);
      document.getElementById("f-copy-preview").addEventListener("click", () => {
        const feld = document.getElementById("f-preview-url");
        const url = feld ? feld.textContent.trim() : "";
        if (!url) return;
        navigator.clipboard.writeText(url).then(() => {
          const btn = document.getElementById("f-copy-preview");
          if (btn) { btn.textContent = "✓ Kopiert"; setTimeout(() => { btn.textContent = "📋 Kopieren"; }, 2000); }
        });
      });
      document.getElementById("f-gen-link").addEventListener("click", () => showFormularLinkModal(state.typ));
      renderFields();
      renderVorschau(state.vorschlag || undefined);
    };

    const load = async () => {
      const r = await api(`/app/api/formulare/${encodeURIComponent(state.typ)}`);
      const j = r ? await r.json().catch(() => null) : null;
      if (!j || !j.ok) {
        App.view.innerHTML =
          `<button class="btn-sm btn-ghost" id="back-mehr" style="margin-bottom:10px">← Zurück</button>` +
          `<div class="card"><p class="muted">${esc((j && j.error) || "Konnte Formular nicht laden.")}</p></div>`;
        const bb = document.getElementById("back-mehr");
        if (bb) bb.addEventListener("click", () => navigate("mehr"));
        return;
      }
      state.title = j.title || ""; state.subtitle = j.subtitle || "";
      state.fields = (j.fields || []).map(normField);
      state.fieldTypes = j.field_types || []; state.optionTypes = j.option_types || [];
      state.typen = j.anfrage_typen || []; state.typ = j.anfrage_typ || state.typ;
      state.aktivTyp = j.aktiv_typ || "";
      state.previewUrl = j.preview_url || ""; state.dirty = false;
      shell();
    };

    // Verlaesst der Inhaber den Screen mit ungespeichertem Entwurf, fragt
    // navigate() nach (globaler Guard, greift auch bei der Zurueck-Taste).
    App.dirtyGuard = () => !state.dirty
      || confirm("Das geänderte Formular ist noch nicht gespeichert. Trotzdem verlassen?");

    await load();
  },

  async diagnose() {
    // Aggregierter Health-Report: Verbindungen, Cron-Heartbeats,
    // Mail-Pipeline, Werkstatt-Adresse. Sichtbar fuer alle Rollen.
    const render = async () => {
      App.view.innerHTML = `<div class="loading">Lädt …</div>`;
      const r = await api("/app/api/diagnose");
      if (!r || !r.ok) {
        App.view.innerHTML = `<div class="card"><p class="empty">Konnte Status nicht laden.</p></div>`;
        return;
      }
      const d = await r.json();
      const pill = (s) => `<span class="pill ${esc(s)}">${s === "ok" ? "OK" : (s === "warn" ? "Hinweis" : "Problem")}</span>`;

      // Verbindungen
      const vList = (d.verbindungen || []).map((v) =>
        `<div class="row"><div><div>${esc(v.label)}</div><div class="sub">${esc(v.detail || "")}</div></div>${pill(v.status)}</div>`
      ).join("");

      // Crons
      const cList = (d.crons || []).map((c) => {
        const status = c.ok ? "ok" : "danger";
        const age = c.age_min == null ? "—" : `${c.age_min} Min`;
        return `<div class="row"><div><div>${esc(c.name)}</div><div class="sub">letzter Heartbeat: ${esc(age)}${c.max_min ? ` · max. ${esc(String(c.max_min))} Min` : ""}</div></div>${pill(status)}</div>`;
      }).join("") || `<div class="empty">Noch keine Cron-Daten.</div>`;

      // Mail-Pipeline
      const mailOk = d.mail && (d.mail.failed_queue || 0) === 0 ? "ok" : "warn";
      const mailDetail = d.mail
        ? `${d.mail.last_eingang_fmt ? "Letzte Mail: " + esc(d.mail.last_eingang_fmt) : "Noch keine Mails verarbeitet"} · ${d.mail.failed_queue} in Warteschlange`
        : "—";

      // Werkstatt
      const w = d.werkstatt || {};
      const wDetail = w.gesetzt
        ? `${esc(w.strasse)} · ${esc(w.plz)} ${esc(w.ort)}`
        : "Adresse fehlt — Einstellungen prüfen.";
      const wStatus = w.gesetzt ? "ok" : "warn";

      App.view.innerHTML =
        `<button class="btn-sm btn-ghost" id="back-mehr" style="margin-bottom:10px">← Zurück</button>` +
        `<div style="display:flex;align-items:center;justify-content:space-between;margin:4px 4px 14px">
           <h1 style="font-size:22px;margin:0">Status</h1>
           <button class="btn-sm btn-ghost" id="diag-refresh">Aktualisieren</button>
         </div>` +
        `<div class="card"><h2>Verbindungen</h2>${vList}</div>` +
        `<div class="card"><h2>Hintergrund-Prozesse (Crons)</h2>${cList}</div>` +
        `<div class="card"><h2>Mail-Pipeline</h2>
           <div class="row"><div><div>${esc(mailDetail)}</div></div>${pill(mailOk)}</div>
         </div>` +
        `<div class="card"><h2>Werkstatt-Adresse</h2>
           <div class="row"><div><div>${esc(wDetail)}</div></div>${pill(wStatus)}</div>
         </div>`;
      document.getElementById("back-mehr").addEventListener("click", () => navigate("mehr"));
      document.getElementById("diag-refresh").addEventListener("click", render);
    };
    await render();
  },

  async hilfe() {
    // Statische Feature-Übersicht. Inhaber sieht alles, Mitarbeiter
    // nur die fuer ihn relevanten Bereiche.
    const isInhaber = App.me.employee.is_inhaber;
    const card = (title, items) =>
      `<div class="card"><h2>${esc(title)}</h2>` +
      items.map((it) =>
        `<button class="row menu-item" data-go="${esc(it.go)}">
           <div><div><b>${esc(it.icon)} ${esc(it.label)}</b></div>
             <div class="sub">${esc(it.hint)}</div></div>
           <span class="sub">›</span>
         </button>`).join("") +
      `</div>`;

    const tagesarbeit = [
      { icon: "📋", label: "Aktionen", go: "aktuelles", hint: "Alle Bereiche auf einen Blick" },
      { icon: "📅", label: "Termine", go: "termine", hint: "Termine anschauen + neu anlegen" },
      { icon: "📞", label: "Rückrufe", go: "rueckrufe_page", hint: "Offene Rückrufe abhaken oder neu anlegen" },
      { icon: "✉️", label: "Anfragen", go: "anfragen", hint: "Mail-Anfragen lesen + direkt antworten" },
      { icon: "🛠️", label: "Aufträge", go: "auftraege_page", hint: "Laufende Aufträge + Fortschritt" },
      { icon: "💰", label: "Buchhaltung", go: "buchhaltung", hint: "Offene Posten, Rechnungen, Angebote, Belege" },
    ];
    const buero = [
      { icon: "💰", label: "Buchhaltung", go: "buchhaltung", hint: "Offene Posten, Rechnungen, Angebote, Belege — alles an einem Ort" },
      { icon: "🤖", label: "Q-Assistent", go: "assistent", hint: "Diktiere Termin/Rückruf/Angebot/Rechnung" },
    ];
    const stammdaten = [
      { icon: "🔍", label: "Kunden", go: "kunden", hint: "Kunden suchen + Profil + Archiv-Upload" },
      { icon: "📚", label: "Wissen", go: "wissen", hint: "Häufige Antworten + Preise" },
      { icon: "🧰", label: "Material", go: "material", hint: "Bestell-Links + Lieferanten" },
      { icon: "👥", label: "Team", go: "team", hint: "Mitarbeiter anlegen + Krank/Urlaub" },
      { icon: "📝", label: "Anfrage-Formular", go: "formulare", hint: "Welche Felder Kunden ausfüllen sollen" },
    ];
    const diagnose = [
      { icon: "🩺", label: "Status", go: "diagnose", hint: "Läuft alles? Verbindungen + Crons + Pipeline" },
      { icon: "⚙️", label: "Einstellungen", go: "einstellungen", hint: "Firma, Verbindungen, Werkstatt-Adresse" },
    ];

    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-mehr" style="margin-bottom:10px">← Zurück</button>` +
      `<h1 style="font-size:22px;margin:4px 4px 14px">Hilfe &amp; Tour</h1>` +
      `<p class="muted" style="margin:0 4px 14px">So findest du dich zurecht — tippe auf einen Bereich um direkt dorthin zu springen.</p>` +
      card("Tagesarbeit", tagesarbeit) +
      (isInhaber ? card("Büro", buero) : "") +
      (isInhaber ? card("Stammdaten", stammdaten) : "") +
      card("Diagnose & Einstellungen", diagnose);

    document.getElementById("back-mehr").addEventListener("click", () => navigate("mehr"));
    document.querySelectorAll(".menu-item[data-go]").forEach((b) =>
      b.addEventListener("click", () => navigate(b.dataset.go)));
  },

  async mehr() {
    const m = App.me;
    const feats = new Set(m.features || []);
    const schnell = [];
    schnell.push(`<button class="row menu-item" data-go="kunden"><span>🔍 Kunden suchen</span><span class="sub">›</span></button>`);
    schnell.push(`<button class="row menu-item" data-go="material"><span>🧰 Material</span><span class="sub">›</span></button>`);
    if (feats.has("visualisierung")) schnell.push(`<button class="row menu-item" data-go="visualisierung"><span>🎨 Visualisierung</span><span class="sub">›</span></button>`);
    schnell.push(`<button class="row menu-item" data-go="wissen"><span>📚 Wissensdatenbank</span><span class="sub">›</span></button>`);
    const einst = [];
    // Team-Uebersicht zeigt auch Abwesenheiten und (fuer wer das Team
    // fuehrt) die App-Nutzung der Kollegen — deshalb hinter team.sehen.
    if (feats.has("mitarbeiter") && can("team.sehen")) einst.push(`<button class="row menu-item" data-go="team"><span>👥 Team</span><span class="sub">›</span></button>`);
    // Der EIGENE Kalender ist keine Betriebs-Einstellung — den darf
    // jeder selbst anschliessen (und nur den eigenen).
    if (feats.has("kalender")) einst.push(`<button class="row menu-item" data-go="mein_kalender"><span>📅 Mein Kalender</span><span class="sub">›</span></button>`);
    einst.push(`<button class="row menu-item" data-go="einstellungen"><span>⚙️ Einstellungen</span><span class="sub">›</span></button>`);
    einst.push(`<button class="row menu-item" data-go="diagnose"><span>🩺 Status</span><span class="sub">›</span></button>`);
    einst.push(`<button class="row menu-item" data-go="hilfe"><span>❓ Hilfe &amp; Tour</span><span class="sub">›</span></button>`);
    einst.push(`<button class="row menu-item" data-tour="1"><span>🚀 Einrichtung starten</span><span class="sub">›</span></button>`);
    App.view.innerHTML =
      `<div class="card"><h2>${esc(m.tenant.company_name || "Mein Betrieb")}</h2>
        <div class="row"><span>Angemeldet als</span><span class="sub">${esc(m.employee.name)}${m.employee.rolle ? " (" + esc(ROLLEN_LABEL[m.employee.rolle] || m.employee.rolle) + ")" : ""}</span></div>
        <div class="row"><span>Freigeschaltete Funktionen</span><span class="sub">${(m.features || []).length}</span></div>
      </div>` +
      `<div class="card"><h2>Schnellzugriff</h2>${schnell.join("")}</div>` +
      `<div class="card"><h2>Einstellungen</h2>${einst.join("")}</div>` +
      `<div class="card"><h2>Benachrichtigungen</h2>
        <div class="row"><span>Push auf diesem Gerät</span><span class="pill ${notifGranted() ? "ok" : "warn"}">${notifGranted() ? "aktiv" : (notifSupported() ? "aus" : "nicht unterstützt")}</span></div>
        ${notifSupported() ? `<button class="btn-sm btn-ghost" id="enable-notif-more" style="margin-top:8px">Push aktivieren</button>` : `<p class="muted" style="margin-top:6px">Auf dem iPhone: erst „Zum Home-Bildschirm" hinzufügen, dann sind Benachrichtigungen möglich.</p>`}
      </div>
      <div class="card"><form method="post" action="/app/logout"><button type="submit">Abmelden</button></form></div>
      <p class="muted" style="text-align:center;margin-top:14px">Weitere Funktionen folgen.</p>`;
    const b = document.getElementById("enable-notif-more");
    if (b) b.addEventListener("click", enablePush);
    document.querySelectorAll(".menu-item").forEach((mi) =>
      mi.addEventListener("click", () => {
        if (mi.dataset.tour) { App.startOnboarding = true; navigate("assistent"); }
        else navigate(mi.dataset.go);
      }));
  },

  async assistent() {
    App.qchat = App.qchat || [];
    // Gesprächsverlauf für Q (Mehrfach-Rückfragen behalten den Kontext).
    // Getrennt von App.qchat, weil Schnellaktionen (startIntent) ihren
    // Befehl NICHT als sichtbare Bubble ablegen, der Kontext aber zählt.
    App.qhistory = App.qhistory || [];
    const QHIST_MAX = 12; // wie viele frühere Turns an Q mitgeschickt werden
    // Schlanke Strich-Icons (currentColor) statt Emojis — Look moderner Chat-UIs.
    const IC = {
      spark: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.7 4.8L18.5 9.5l-4.8 1.7L12 16l-1.7-4.8L5.5 9.5l4.8-1.7L12 3z"/><path d="M19 14.5l.6 1.9 1.9.6-1.9.6-.6 1.9-.6-1.9-1.9-.6 1.9-.6.6-1.9z"/></svg>`,
      clip: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>`,
      cam: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>`,
      mic: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="11" rx="3"/><path d="M5 10v1a7 7 0 0 0 14 0v-1"/><line x1="12" y1="19" x2="12" y2="22"/></svg>`,
      stop: `<svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="6" y="6" width="12" height="12" rx="3"/></svg>`,
      send: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="20" x2="12" y2="5"/><polyline points="6 11 12 5 18 11"/></svg>`,
      trash: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>`,
    };
    const _miniSphereHtml =
      `<button class="cbtn q-composer-mic" id="q-mini-sphere" title="${SPRECH_TITEL}" hidden aria-label="Sprechen">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <rect x="9" y="2" width="6" height="11" rx="3"/>
          <path d="M5 10a7 7 0 0 0 14 0"/>
          <line x1="12" y1="19" x2="12" y2="22"/>
          <line x1="8" y1="22" x2="16" y2="22"/>
        </svg>
      </button>`;
    // Aufnahme-Leiste (WhatsApp-Prinzip): ersetzt die Eingabezeile, solange
    // aufgenommen wird — roter Punkt + Laufzeit + Live-Pegel zeigen, dass es
    // wirklich laeuft; verwerfen links, senden rechts.
    const _recBarHtml =
      `<div class="q-recbar" id="q-recbar" hidden>
         <button class="q-rec-x" id="q-rec-cancel" title="Aufnahme verwerfen" aria-label="Aufnahme verwerfen">${IC.trash}</button>
         <span class="q-rec-dot" aria-hidden="true"></span>
         <span class="q-rec-time" id="q-rec-time">0:00</span>
         <canvas class="q-rec-wave" id="q-rec-wave" aria-hidden="true"></canvas>
         <button class="cbtn q-rec-go" id="q-rec-send" title="Aufnahme senden" aria-label="Aufnahme senden">${IC.send}</button>
       </div>`;
    App.view.innerHTML =
      `<div class="chat" id="q-chat"></div>
       <div class="composer">
         <div class="q-menu" id="q-menu" hidden></div>
         <div class="composer-preview" id="composer-preview" hidden></div>
         <div class="composer-inner">
           <button class="cbtn ghost" id="q-actions" title="Funktionen" aria-label="Funktionen">${IC.spark}</button>
           <button class="cbtn ghost" id="q-attach" title="Datei anhängen" aria-label="Anhängen">${IC.clip}</button>
           <button class="cbtn ghost" id="q-camera" title="Foto machen" aria-label="Foto machen">${IC.cam}</button>
           <textarea id="q-input" rows="1" placeholder="Schreib Q …"></textarea>
           ${_miniSphereHtml}
           <button class="cbtn" id="q-send" title="Senden" aria-label="Senden">${IC.send}</button>
         </div>
         ${_recBarHtml}
       </div>
       <input type="file" id="q-file" accept="image/jpeg,image/png,image/webp,application/pdf" style="display:none">
       <input type="file" id="q-camera-file" accept="image/*" capture="environment" style="display:none">`;

    const chatEl = document.getElementById("q-chat");
    const input = document.getElementById("q-input");
    const sendBtn = document.getElementById("q-send");
    const attachBtn = document.getElementById("q-attach");
    const fileEl = document.getElementById("q-file");
    const cameraBtn = document.getElementById("q-camera");
    const cameraFileEl = document.getElementById("q-camera-file");

    function scrollDown() { requestAnimationFrame(() => window.scrollTo(0, document.body.scrollHeight)); }
    function auto() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 120) + "px"; }

    // resultText/resultHtml leben auf Modul-Ebene (_assistResultText/-Html),
    // weil auch das Q-Overlay Aktionen ausführt und die Ergebnistexte braucht.
    const resultText = _assistResultText;
    const resultHtml = _assistResultHtml;

    // Q-Globus: WebGL-Netzwerk-Globus (Drahtgitter-Ikosaeder + Partikelwolke +
    // Energiebögen) wie auf der Website; Three.js wird lokal geladen. Fallback
    // auf das SVG-Ring-Muster bei fehlendem/instabilem WebGL.
    const sphereWrap =
      `<div class="q-sphere-wrap" id="q-sphere-wrap" role="button" tabindex="0" aria-label="${SPRECH_TITEL}">
         <canvas id="q-sphere-canvas"></canvas>
         <svg class="q-sphere-fallback" viewBox="0 0 100 100" aria-hidden="true">
           <circle class="ring-1" cx="50" cy="50" r="35"/>
           <circle class="ring-2" cx="50" cy="50" r="28"/>
           <circle class="ring-3" cx="50" cy="50" r="22"/>
           <circle cx="50" cy="50" r="2" fill="#0066cc" stroke="none"/>
         </svg>
       </div>`;

    function render() {
      const hasMsgs = App.qchat.some((m) => m.role !== "typing");
      // Funktion angetippt → animierte Sphere statt Seed-Text, während Q übernimmt.
      if (App.qIntent && !hasMsgs) {
        App.qWorking = true; // .working-Klasse lässt den Globus schneller drehen
        chatEl.innerHTML =
          `<div class="q-hero working">${sphereWrap}
             <div class="q-working">
               <span class="qm-ico">${App.qIntent.ico}</span>
               <span>${esc(App.qIntent.label)}</span>
               <span class="q-dots"><span></span><span></span><span></span></span>
             </div>
           </div>`;
        mountQSphere();
        return;
      }
      if (!hasMsgs) {
        App.qWorking = false;
        const miniSphHide = document.getElementById("q-mini-sphere");
        if (miniSphHide) miniSphHide.hidden = true;
        chatEl.innerHTML = `<div class="q-hero">${sphereWrap}<p class="q-sphere-hint" id="q-sphere-hint">${SPRECH_TITEL}</p></div>`;
        mountQSphere();
        // Laeuft gerade eine Aufnahme, muss der frisch gebaute Globus wieder
        // in den Aufnahme-Zustand (Klasse + Hinweistext gingen sonst verloren).
        if (App.qVoice) App.qVoice.refreshUi();
        return;
      }
      if (App.qSphereStop) { try { App.qSphereStop(); } catch (e) {} App.qSphereStop = null; App.qSphereCanvas = null; }
      const miniSph = document.getElementById("q-mini-sphere");
      if (miniSph) miniSph.hidden = false;
      chatEl.innerHTML = App.qchat.map((m, i) => {
        if (m.role === "me") {
          const imgTag = m.previewUrl ? `<img class="msg-img" src="${m.previewUrl}" alt="${esc(m.fileName || "Bild")}">` : "";
          return `<div class="bubble me">${imgTag}${m.text ? esc(m.text) : ""}</div>`;
        }
        if (m.role === "typing") return `<div class="q-typing-row"><div class="q-thinking-orb" id="q-typing-orb"><canvas id="q-mini-sphere-canvas"></canvas></div><div class="q-typing-ring-wrap"><div class="bubble q typing"><span></span><span></span><span></span></div></div></div>`;
        if (m.role === "err") return `<div class="bubble q err">${esc(m.text)}</div>`;
        if (m.role === "onb") {
          const OB_TITLE = { google: "Google verbinden", microsoft: "Microsoft / Outlook verbinden",
                             lexware: "Lexware Office verbinden", push: "Benachrichtigungen aktivieren" };
          if (m.resolved) {
            const txt = m.status === "ok" ? "✓ verbunden" : m.status === "fail" ? "nicht verbunden" : "später";
            return `<div class="bubble q confirm"><p class="q-summary">${esc(OB_TITLE[m.kind] || "")}</p><div class="confirm-done">${txt}</div></div>`;
          }
          if (m.kind === "lexware" && m.expand) {
            return `<div class="bubble q confirm">
               <p class="q-summary">Lexware API-Schlüssel</p>
               <input type="text" class="rech-input" data-ob-lexkey="${i}" placeholder="aus app.lexware.de → Profil → API-Keys" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false">
               <p class="sub" data-ob-msg="${i}" style="min-height:16px;margin:4px 0 0"></p>
               <div class="confirm-actions"><button class="btn-sm" data-ob-lexsave="${i}">Speichern</button><button class="btn-sm btn-ghost" data-ob-skip="${i}">Später</button></div>
             </div>`;
          }
          const primary = m.kind === "push" ? "Aktivieren" : "Verbinden";
          const primaryAttr = m.kind === "push" ? `data-ob-push="${i}"`
            : m.kind === "lexware" ? `data-ob-lex="${i}"` : `data-ob-conn="${i}:${m.kind}"`;
          return `<div class="bubble q confirm">
             <p class="q-summary">${esc(OB_TITLE[m.kind] || "")}</p>
             <div class="confirm-actions"><button class="btn-sm" ${primaryAttr}>${primary}</button><button class="btn-sm btn-ghost" data-ob-skip="${i}">Später</button></div>
           </div>`;
        }
        if (m.role === "confirm") {
          const btns = m.resolved
            ? `<div class="confirm-done">${m.cancelled ? "✕ Abgebrochen" : "✓ Bestätigt"}</div>`
            : `<div class="confirm-actions"><button class="btn-sm" data-cyes="${i}">Ausführen</button><button class="btn-sm btn-ghost" data-cno="${i}">Abbrechen</button></div>`;
          return `<div class="bubble q confirm">${m.frage ? `<p style="margin:0 0 8px">${esc(m.frage)}</p>` : ""}<p class="q-summary">${esc(m.summary)}</p>${btns}</div>`;
        }
        if (m.role === "upload") {
          const thumb = m.previewUrl
            ? `<img src="${m.previewUrl}" alt="${esc(m.name)}" style="width:100%;max-height:220px;object-fit:cover;border-radius:10px;margin-bottom:8px">` : "";
          if (m.resolved) {
            const lbl = m.cancelled ? "✕ Abgebrochen"
              : m.choice === "viz" ? "🎨 Visualisierung"
              : m.choice === "archiv" ? "📁 Im Archiv gespeichert"
              : "📄 Als Beleg abgelegt";
            return `<div class="bubble q confirm">${thumb}<p class="q-summary">${esc(m.name)}</p><div class="confirm-done">${lbl}</div></div>`;
          }
          if (m.stage === "vizprompt") {
            return `<div class="bubble q confirm">${thumb}
               <p class="q-summary">🎨 Was soll am Bild verändert werden?</p>
               <textarea class="rech-input" data-upvtext="${i}" rows="3" placeholder="z.B. Wände in warmem Grau streichen, Eichenparkett verlegen">${esc(m.vizText || "")}</textarea>
               <div class="confirm-actions"><button class="btn-sm" data-upvizgo="${i}">Visualisieren</button><button class="btn-sm btn-ghost" data-upcancel="${i}">Abbrechen</button></div>
             </div>`;
          }
          if (m.stage === "archivkunde") {
            return `<div class="bubble q confirm">${thumb}
               <p class="q-summary">📁 Für welchen Kunden ablegen?</p>
               <input type="text" class="rech-input" data-upktext="${i}" value="${esc(m.kundeName || "")}" placeholder="z.B. Müller, Hauptstr. 3" autocomplete="off">
               <div class="confirm-actions"><button class="btn-sm" data-uparchgo="${i}">Speichern</button><button class="btn-sm btn-ghost" data-upcancel="${i}">Abbrechen</button></div>
             </div>`;
          }
          const feats = new Set(App.me.features || []);
          const opts = [];
          if (feats.has("visualisierung")) opts.push(`<button class="btn-sm" data-upviz="${i}">🎨 Visualisieren</button>`);
          if (feats.has("lexware")) opts.push(`<button class="btn-sm btn-ghost" data-upbeleg="${i}">📄 Als Beleg</button>`);
          if (feats.has("drive_archiv")) opts.push(`<button class="btn-sm btn-ghost" data-uparchiv="${i}">📁 Speichern</button>`);
          opts.push(`<button class="btn-sm btn-ghost" data-upcancel="${i}">Abbrechen</button>`);
          return `<div class="bubble q confirm">${thumb}
             <p class="q-summary">📎 ${esc(m.name)}</p>
             <p style="margin:0 0 8px">Was soll ich damit machen?</p>
             <div class="confirm-actions" style="flex-wrap:wrap">${opts.join("")}</div>
           </div>`;
        }
        if (m.role === "mail") {
          const d = m.data || {};
          if (m.resolved) {
            return `<div class="bubble q confirm mail"><p class="q-summary">✉️ E-Mail an ${esc(d.empfaenger || d.empfaenger_name || "")}</p><div class="confirm-done">${m.sent ? "✓ Gesendet" : "✕ Abgebrochen"}</div></div>`;
          }
          const chips = (d.anhaenge || []).map((a, k) =>
            `<span class="mail-chip">📎 ${esc(a.name || "Anhang")}<button class="mail-chip-x" data-mdel="${i}:${k}" aria-label="Anhang entfernen">✕</button></span>`).join("");
          return `<div class="bubble q confirm mail">
             ${m.frage ? `<p style="margin:0 0 8px">${esc(m.frage)}</p>` : ""}
             <p class="q-summary">✉️ E-Mail-Entwurf</p>
             ${d.hinweis ? `<p class="sub" style="margin:0 0 8px">${esc(d.hinweis)}</p>` : ""}
             <label class="sub">An</label>
             <input type="email" class="rech-input" data-mmail="${i}" value="${esc(d.empfaenger || "")}" placeholder="kunde@example.de" autocomplete="off" autocapitalize="off" spellcheck="false">
             <label class="sub">Betreff</label>
             <input type="text" class="rech-input" data-msubj="${i}" value="${esc(d.betreff || "")}" placeholder="Betreff">
             <label class="sub">Text</label>
             <textarea class="rech-input" data-mtext="${i}" rows="9">${esc(d.text || "")}</textarea>
             <div class="mail-anh">${chips}<button class="btn-sm btn-ghost" data-madd="${i}">📎 Datei anhängen</button></div>
             <input type="file" data-mfile="${i}" style="display:none">
             <div class="confirm-actions"><button class="btn-sm" data-msend="${i}">Senden</button><button class="btn-sm btn-ghost" data-mcancel="${i}">Abbrechen</button></div>
           </div>`;
        }
        if (m.role === "rechnung") {
          const r = m.data || {};
          if (m.resolved) {
            return `<div class="bubble q confirm"><p class="q-summary">Rechnung an ${esc(r.kunde)}</p><div class="confirm-done">${m.sent ? "✓ Rechnung gesendet" : "✕ Abgebrochen"}</div></div>`;
          }
          const pos = (r.positionen || []).map((p) =>
            `<div class="row" style="padding:6px 0"><div><div>${esc(p.name)}</div>${p.beschreibung ? `<div class="sub">${esc(p.beschreibung)}</div>` : ""}</div><span class="sub">${esc(String(p.menge))} ${esc(p.einheit || "")} · ${esc(p.preis || "")}</span></div>`).join("");
          return `<div class="bubble q confirm rechnung">
             <p class="q-summary">Rechnung an ${esc(r.kunde)}${r.betrag ? " · " + esc(r.betrag) : ""}</p>
             <div class="rech-pos">${pos || `<div class="sub">Keine Positionen hinterlegt</div>`}</div>
             <label class="sub">Empfänger-E-Mail</label>
             <input type="email" class="rech-input" data-rmail="${i}" value="${esc(r.kunde_email || "")}" placeholder="kunde@example.de">
             <label class="sub">Anschreiben</label>
             <textarea class="rech-input" data-rtext="${i}" rows="6">${esc(r.anschreiben || "")}</textarea>
             <div class="confirm-actions"><button class="btn-sm" data-rsend="${i}">Rechnung senden</button><button class="btn-sm btn-ghost" data-rcancel="${i}">Abbrechen</button></div>
           </div>`;
        }
        // Onboarding-Texte dürfen einfaches Markup (z.B. <b>) führen; dynamische
        // Teile (Name) sind in der Quelle bereits mit esc() gefiltert. Plain-Texte
        // werden ge-esc()-t und enthaltene URLs klickbar gemacht (linkify).
        return `<div class="bubble q">${m.html ? m.text : linkify(esc(m.text))}</div>`;
      }).join("");
      chatEl.querySelectorAll("[data-cyes]").forEach((b) =>
        b.addEventListener("click", () => doConfirm(parseInt(b.dataset.cyes, 10))));
      chatEl.querySelectorAll("[data-cno]").forEach((b) =>
        b.addEventListener("click", () => {
          const m = App.qchat[parseInt(b.dataset.cno, 10)];
          m.resolved = true; m.cancelled = true;
          App.qchat.push({ role: "q", text: "Okay, lasse ich." });
          render(); scrollDown();
        }));
      // Rechnungs-Karte: Eingaben in m.data spiegeln (überleben Re-Renders)
      chatEl.querySelectorAll("[data-rtext]").forEach((t) =>
        t.addEventListener("input", () => { App.qchat[parseInt(t.dataset.rtext, 10)].data.anschreiben = t.value; }));
      chatEl.querySelectorAll("[data-rmail]").forEach((t) =>
        t.addEventListener("input", () => { App.qchat[parseInt(t.dataset.rmail, 10)].data.kunde_email = t.value; }));
      chatEl.querySelectorAll("[data-rsend]").forEach((b) =>
        b.addEventListener("click", () => doRechnungSenden(parseInt(b.dataset.rsend, 10))));
      chatEl.querySelectorAll("[data-rcancel]").forEach((b) =>
        b.addEventListener("click", () => {
          const m = App.qchat[parseInt(b.dataset.rcancel, 10)];
          m.resolved = true; m.cancelled = true;
          App.qchat.push({ role: "q", text: "Okay, die Rechnung lasse ich erstmal." });
          render(); scrollDown();
        }));
      // Mail-Entwurf: Eingaben in m.data spiegeln (überleben Re-Renders),
      // Anhänge hinzufügen/entfernen, senden oder verwerfen.
      chatEl.querySelectorAll("[data-mmail]").forEach((t) =>
        t.addEventListener("input", () => { App.qchat[parseInt(t.dataset.mmail, 10)].data.empfaenger = t.value; }));
      chatEl.querySelectorAll("[data-msubj]").forEach((t) =>
        t.addEventListener("input", () => { App.qchat[parseInt(t.dataset.msubj, 10)].data.betreff = t.value; }));
      chatEl.querySelectorAll("[data-mtext]").forEach((t) =>
        t.addEventListener("input", () => { App.qchat[parseInt(t.dataset.mtext, 10)].data.text = t.value; }));
      chatEl.querySelectorAll("[data-madd]").forEach((b) =>
        b.addEventListener("click", () => {
          const f = chatEl.querySelector(`[data-mfile="${b.dataset.madd}"]`);
          if (f) f.click();
        }));
      chatEl.querySelectorAll("[data-mfile]").forEach((f) =>
        f.addEventListener("change", () => {
          const file = f.files && f.files[0];
          f.value = "";
          if (file) addMailAnhang(parseInt(f.dataset.mfile, 10), file);
        }));
      chatEl.querySelectorAll("[data-mdel]").forEach((b) =>
        b.addEventListener("click", () => {
          const [mi, ai] = b.dataset.mdel.split(":").map((x) => parseInt(x, 10));
          const m = App.qchat[mi];
          if (m && m.data && m.data.anhaenge) { m.data.anhaenge.splice(ai, 1); render(); }
        }));
      chatEl.querySelectorAll("[data-msend]").forEach((b) =>
        b.addEventListener("click", () => doMailSenden(parseInt(b.dataset.msend, 10))));
      chatEl.querySelectorAll("[data-mcancel]").forEach((b) =>
        b.addEventListener("click", () => {
          const m = App.qchat[parseInt(b.dataset.mcancel, 10)];
          m.resolved = true; m.cancelled = true;
          App.qchat.push({ role: "q", text: "Okay, die Mail geht nicht raus." });
          render(); scrollDown();
        }));
      // 📎-Upload-Karte: Bild visualisieren, als Beleg ablegen oder im Archiv speichern
      chatEl.querySelectorAll("[data-upviz]").forEach((b) =>
        b.addEventListener("click", () => { App.qchat[parseInt(b.dataset.upviz, 10)].stage = "vizprompt"; render(); }));
      chatEl.querySelectorAll("[data-uparchiv]").forEach((b) =>
        b.addEventListener("click", () => { App.qchat[parseInt(b.dataset.uparchiv, 10)].stage = "archivkunde"; render(); }));
      chatEl.querySelectorAll("[data-upbeleg]").forEach((b) =>
        b.addEventListener("click", () => {
          const m = App.qchat[parseInt(b.dataset.upbeleg, 10)];
          m.resolved = true; m.choice = "beleg"; render(); doUploadBeleg(m.file);
        }));
      chatEl.querySelectorAll("[data-upvtext]").forEach((t) =>
        t.addEventListener("input", () => { App.qchat[parseInt(t.dataset.upvtext, 10)].vizText = t.value; }));
      chatEl.querySelectorAll("[data-upktext]").forEach((t) =>
        t.addEventListener("input", () => { App.qchat[parseInt(t.dataset.upktext, 10)].kundeName = t.value; }));
      chatEl.querySelectorAll("[data-upvizgo]").forEach((b) =>
        b.addEventListener("click", () => {
          const m = App.qchat[parseInt(b.dataset.upvizgo, 10)];
          const prompt = (m.vizText || "").trim();
          if (prompt.length < 5) { push({ role: "err", text: "Bitte etwas genauer beschreiben (min. 5 Zeichen)." }); return; }
          m.resolved = true; m.choice = "viz"; render(); doVisualisieren(m.file, prompt);
        }));
      chatEl.querySelectorAll("[data-uparchgo]").forEach((b) =>
        b.addEventListener("click", () => {
          const m = App.qchat[parseInt(b.dataset.uparchgo, 10)];
          const kunde = (m.kundeName || "").trim();
          if (kunde.length < 2) { push({ role: "err", text: "Bitte einen Kundennamen angeben." }); return; }
          m.resolved = true; m.choice = "archiv"; render(); doArchiv(m.file, kunde);
        }));
      chatEl.querySelectorAll("[data-upcancel]").forEach((b) =>
        b.addEventListener("click", () => {
          const m = App.qchat[parseInt(b.dataset.upcancel, 10)];
          m.resolved = true; m.cancelled = true;
          App.qchat.push({ role: "q", text: "Okay, lasse ich." });
          render(); scrollDown();
        }));
      // Onboarding-Karten (Q führt durch die Ersteinrichtung)
      chatEl.querySelectorAll("[data-ob-conn]").forEach((b) =>
        b.addEventListener("click", () => obConn(b.dataset.obConn)));
      chatEl.querySelectorAll("[data-ob-lex]").forEach((b) =>
        b.addEventListener("click", () => { App.qchat[parseInt(b.dataset.obLex, 10)].expand = true; render(); }));
      chatEl.querySelectorAll("[data-ob-lexsave]").forEach((b) =>
        b.addEventListener("click", () => obLexSave(parseInt(b.dataset.obLexsave, 10))));
      chatEl.querySelectorAll("[data-ob-push]").forEach((b) =>
        b.addEventListener("click", () => obPush(parseInt(b.dataset.obPush, 10))));
      chatEl.querySelectorAll("[data-ob-skip]").forEach((b) =>
        b.addEventListener("click", () => obSkip(parseInt(b.dataset.obSkip, 10))));
      _mountMiniSphere("q-typing-orb", "q-mini-sphere-canvas", "qMiniSphereStop");
      scrollDown();
    }

    async function doRechnungSenden(idx) {
      const m = App.qchat[idx];
      if (!m || m.resolved) return;
      const r = m.data || {};
      m.resolved = true; m.sent = false; render();
      push({ role: "typing" });
      let res, j = null;
      try {
        res = await fetch("/app/api/rechnung/senden", { method: "POST",
          headers: { "X-CSRF-Token": App.me.csrf, "Content-Type": "application/json" },
          body: JSON.stringify({ angebot_id: r.angebot_id, anschreiben: r.anschreiben, kunde_email: r.kunde_email }) });
      } catch (e) { popTyping(); push({ role: "err", text: "Netzwerkfehler beim Senden." }); return; }
      if (res.status === 303 || res.status === 401 || res.redirected) { location.href = "/app/login"; return; }
      try { j = await res.json(); } catch (e) {}
      popTyping();
      if (j && j.ok && j.mail_sent) { m.sent = true; render(); push({ role: "q", text: `✓ Rechnung an ${esc(j.email_used || r.kunde)} gesendet — Auftrag abgeschlossen.` }); }
      else if (j && j.ok) { m.sent = true; render(); push({ role: "q", text: `Rechnung in Lexware finalisiert${j.mail_error ? " (Mail offen: " + esc(j.mail_error) + ")" : ""}.` }); }
      else { push({ role: "err", text: (j && (j.error || j.mail_error)) || "Rechnung konnte nicht gesendet werden." }); }
    }

    // Anhang aus dem Dateisystem an einen Mail-Entwurf hängen. Die Bytes
    // reisen als Base64 im Freigabe-Request mit — Microsoft Graph nimmt
    // Anhänge bis 3 MB direkt am Entwurf entgegen (darüber bräuchte es eine
    // Upload-Session), darum dieselbe Grenze schon hier.
    const MAIL_ANH_MAX = 3 * 1024 * 1024;
    const MAIL_ANH_COUNT = 5;

    function fileToB64(file) {
      return new Promise((resolve, reject) => {
        const r = new FileReader();
        r.onload = () => { const s = String(r.result || ""); resolve(s.slice(s.indexOf(",") + 1)); };
        r.onerror = () => reject(new Error("read"));
        r.readAsDataURL(file);
      });
    }

    async function addMailAnhang(idx, file) {
      const m = App.qchat[idx];
      if (!m || m.resolved) return;
      m.data.anhaenge = m.data.anhaenge || [];
      if (m.data.anhaenge.length >= MAIL_ANH_COUNT) {
        push({ role: "err", text: `Maximal ${MAIL_ANH_COUNT} Anhänge pro Mail.` }); return;
      }
      if (file.size > MAIL_ANH_MAX) {
        push({ role: "err", text: `„${file.name}" ist zu groß (max 3 MB pro Anhang).` }); return;
      }
      let b64;
      try { b64 = await fileToB64(file); }
      catch (e) { push({ role: "err", text: "Datei konnte nicht gelesen werden." }); return; }
      m.data.anhaenge.push({ quelle: "upload", name: file.name,
                             mime: file.type || "application/octet-stream", b64 });
      render();
    }

    async function doMailSenden(idx) {
      const m = App.qchat[idx];
      if (!m || m.resolved) return;
      const d = m.data || {};
      if (!/^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$/.test((d.empfaenger || "").trim())) {
        push({ role: "err", text: "Bitte eine gültige Empfänger-Adresse eintragen." }); return;
      }
      if ((d.betreff || "").trim().length < 2) { push({ role: "err", text: "Bitte einen Betreff eintragen." }); return; }
      if ((d.text || "").trim().length < 2) { push({ role: "err", text: "Der Mail-Text fehlt." }); return; }
      m.resolved = true; m.sent = false; render();
      push({ role: "typing" });
      const args = {
        empfaenger: (d.empfaenger || "").trim(),
        empfaenger_name: d.empfaenger_name || "",
        betreff: (d.betreff || "").trim(),
        text: (d.text || "").trim(),
        kunde_name: d.kunde_name || "",
        anhaenge: d.anhaenge || [],
      };
      let res, j = null;
      try {
        res = await fetch("/app/api/assistent/ausfuehren", { method: "POST",
          headers: { "X-CSRF-Token": App.me.csrf, "Content-Type": "application/json" },
          body: JSON.stringify({ tool: "email_schreiben", args }) });
      } catch (e) { popTyping(); m.resolved = false; render(); push({ role: "err", text: "Netzwerkfehler beim Senden." }); return; }
      if (res.status === 303 || res.status === 401 || res.redirected) { location.href = "/app/login"; return; }
      try { j = await res.json(); } catch (e) {}
      popTyping();
      if (j && j.type === "done" && j.result && j.result.ok) {
        m.sent = true; render();
        push({ role: "q", text: "✓ " + resultText("email_schreiben", j.result) });
      } else {
        // Entwurf wieder aufmachen — der Nutzer soll Adresse/Text korrigieren
        // und erneut senden können, statt den Text zu verlieren.
        m.resolved = false; render();
        push({ role: "err", text: (j && (j.text || (j.result && j.result.error))) || "Mail konnte nicht gesendet werden." });
      }
    }

    function push(m) { App.qchat.push(m); render(); }
    function popTyping() { const i = App.qchat.findIndex((x) => x.role === "typing"); if (i >= 0) App.qchat.splice(i, 1); }

    // Bild-Anhang verwerfen (nach ausgefuehrter Aktion oder Abbruch). Der
    // Composer-Vorschau-Blob wird hier freigegeben; die in den Chat-Blasen
    // gerenderten Thumbnails halten eigene Blob-URLs (bewusst nicht revoked,
    // sonst zeigen sie nach einem Re-Render kaputte Bilder).
    function clearPending() {
      App.qPendingFile = null;
      App.qImgChat = [];
      if (App.qPendingPreviewUrl) {
        try { URL.revokeObjectURL(App.qPendingPreviewUrl); } catch (_) {}
        App.qPendingPreviewUrl = null;
      }
      const el = document.getElementById("composer-preview");
      if (el) { el.innerHTML = ""; el.hidden = true; }
    }

    // Ein Bild-Turn: Bild + (optionaler) Text an Q. Q entscheidet, was zu tun
    // ist. Bei einer Rückfrage bleibt das Bild angehängt, damit die Antwort
    // des Nutzers ("bei Müller ablegen") im selben Kontext weiterläuft.
    async function sendImageTurn(text, file) {
      App.qImgChat = App.qImgChat || [];
      // hist = bisheriger Verlauf OHNE den aktuellen Turn — der aktuelle Text
      // geht als ?text= mit und wird serverseitig als finaler Bild-Turn
      // angehängt; sonst stünde er doppelt im Kontext und verwirrt die Runde.
      const hist = encodeURIComponent(JSON.stringify(
        App.qImgChat.slice(-8).map((t) => ({ role: t.role, text: (t.text || "").slice(0, 500) }))));
      let bubbleUrl = null;
      try { bubbleUrl = URL.createObjectURL(file); } catch (_) {}
      push({ role: "me", text, previewUrl: bubbleUrl, fileName: file.name });
      push({ role: "typing" });
      let res, j = null;
      const url = "/app/api/assistent/mit-bild?text=" + encodeURIComponent(text) + "&hist=" + hist;
      try {
        res = await fetch(url, { method: "POST",
          headers: { "X-CSRF-Token": App.me.csrf, "Content-Type": file.type }, body: file });
      } catch (_) { popTyping(); push({ role: "err", text: "Netzwerkfehler. Bitte erneut." }); return; }
      if (res.status === 303 || res.status === 401 || res.redirected) { location.href = "/app/login"; return; }
      try { j = await res.json(); } catch (_) {}
      popTyping();
      if (!j || !j.type) { push({ role: "err", text: "Konnte das Bild nicht verarbeiten." }); return; }

      if (j.type === "action") {
        // Q hat sich für eine Aktion entschieden → im Hintergrund ausführen.
        // Objekt-Fragen behalten das Bild angehängt: „und wo sitzt das
        // Ventil?" ist eine Rückfrage zum selben Gegenstand.
        if (j.action === "objekt_frage") { doObjekt(file, j.frage || text, "doku"); return; }
        if (j.action === "objekt_kaufen") { doObjekt(file, j.beschreibung || text, "kaufen"); return; }
        clearPending();
        if (j.action === "visualisieren") doVisualisieren(file, j.beschreibung || text);
        else if (j.action === "archiv") doArchiv(file, j.kunde_name || "");
        else if (j.action === "beleg") doUploadBeleg(file);
        else push({ role: "err", text: "Unbekannte Aktion." });
        return;
      }

      // message/error → Rückfrage oder Antwort; Bild bleibt für die Folge an.
      // Jetzt erst Frage + Antwort in den Verlauf für die nächste Runde.
      const msg = j.text || "Was soll ich mit dem Bild machen?";
      App.qImgChat.push({ role: "user", text: text || "(Bild angehängt)" });
      App.qImgChat.push({ role: "model", text: msg });
      push({ role: (j.type === "error" ? "err" : "q"), text: msg });
    }

    async function send() {
      const text = (input.value || "").trim();
      const pendingFile = App.qPendingFile || null;
      if (!text && !pendingFile) return;
      input.value = ""; auto();

      // Bild angehängt → Q entscheidet, was damit passiert.
      if (pendingFile) { await sendImageTurn(text, pendingFile); return; }

      // ── Nur Text an /assistent ───────────────────────────────
      const history = App.qhistory.slice(-QHIST_MAX);
      App.qhistory.push({ role: "user", text });
      push({ role: "me", text });
      push({ role: "typing" });
      let res, j = null;
      try {
        res = await fetch("/app/api/assistent", { method: "POST",
          headers: { "X-CSRF-Token": App.me.csrf, "Content-Type": "application/json" },
          body: JSON.stringify({ text, history }) });
      } catch (_) { popTyping(); push({ role: "err", text: "Netzwerkfehler. Bitte erneut." }); return; }
      if (res.status === 303 || res.status === 401 || res.redirected) { location.href = "/app/login"; return; }
      try { j = await res.json(); } catch (_) {}
      popTyping();
      if (!j || !j.type) { push({ role: "err", text: "Konnte den Befehl nicht verarbeiten." }); return; }
      qHistoryRecord(j);
      if (j.type === "message") push({ role: "q", text: j.text });
      else if (j.type === "error") push({ role: "err", text: j.text });
      else if (j.type === "confirm") push({ role: "confirm", tool: j.tool, args: j.args, summary: j.summary, frage: j.frage, resolved: false });
      else if (j.type === "done") pushDone(j);
      else if (j.type === "email_entwurf") push(mailDraftMsg(j));
      else if (j.type === "navigate") { if (j.text) push({ role: "q", text: j.text }); handleNavigate(j.bereich, j.kunde, j.kategorie); }
    }

    // Aktion lief bereits durch — Automatisierungsgrad "automatisch"
    // (Einstellungen → Automatisierung). Es gab keine Bestätigung, deshalb
    // zeigen wir zusätzlich zur Vollzugsmeldung die summary, damit der
    // Nutzer schwarz auf weiß sieht, was Q in seinem Namen getan hat.
    function pushDone(j) {
      if (j.result && j.result.ok === false) {
        push({ role: "err", text: (j.result.error) || "Aktion fehlgeschlagen." });
        return;
      }
      const html = resultHtml(j.tool, j.result || {});
      if (html) push({ role: "q", text: html, html: true });
      else push({ role: "q", text: "✓ " + resultText(j.tool, j.result || {}) });
      if (j.summary) push({ role: "q", text: "Erledigt ohne Rückfrage: " + j.summary });
    }

    // Q-Antwort als Modell-Turn in den Verlauf übernehmen, damit die nächste
    // Rückfrage den Faden behält. Bei error NICHTS speichern (kein echter Turn).
    function qHistoryRecord(j) {
      if (!j) return;
      let say = null;
      if (j.type === "message" || j.type === "navigate") say = j.text;
      else if (j.type === "confirm") say = j.frage || j.summary;
      else if (j.type === "done") say = j.summary || j.frage;
      else if (j.type === "email_entwurf") say = j.frage || `Mail-Entwurf an ${j.empfaenger || j.empfaenger_name || "den Kunden"}: ${j.betreff || ""}`;
      if (say) App.qhistory.push({ role: "model", text: say });
    }

    // Funktion aus dem ⚡-Menü angetippt: Q übernimmt den Flow im Hintergrund.
    // Statt den Befehl als Text einzutippen, animieren wir die Sphere ("Q
    // arbeitet") und lassen Gemini selbst nach den fehlenden Angaben fragen.
    async function startIntent(a) {
      App.qIntent = { ico: a.ico, label: a.label };
      // Sphere ist nur im leeren Chat sichtbar — sonst regulärer Tipp-Indikator.
      if (App.qchat.some((m) => m.role !== "typing")) push({ role: "typing" });
      else render();
      const history = App.qhistory.slice(-QHIST_MAX);
      App.qhistory.push({ role: "user", text: a.intent });
      let res, j = null;
      try {
        res = await fetch("/app/api/assistent", { method: "POST",
          headers: { "X-CSRF-Token": App.me.csrf, "Content-Type": "application/json" },
          body: JSON.stringify({ text: a.intent, history }) });
      } catch (e) { App.qIntent = null; popTyping(); push({ role: "err", text: "Netzwerkfehler. Bitte erneut." }); return; }
      if (res.status === 303 || res.status === 401 || res.redirected) { location.href = "/app/login"; return; }
      try { j = await res.json(); } catch (e) {}
      App.qIntent = null;
      popTyping();
      qHistoryRecord(j);
      if (!j || !j.type) { push({ role: "err", text: "Konnte den Befehl nicht verarbeiten." }); }
      else if (j.type === "message") push({ role: "q", text: j.text });
      else if (j.type === "error") push({ role: "err", text: j.text });
      else if (j.type === "confirm") push({ role: "confirm", tool: j.tool, args: j.args, summary: j.summary, frage: j.frage, resolved: false });
      else if (j.type === "done") pushDone(j);
      else if (j.type === "email_entwurf") push(mailDraftMsg(j));
      else if (j.type === "navigate") { if (j.text) push({ role: "q", text: j.text }); handleNavigate(j.bereich, j.kunde, j.kategorie); }
      input.focus();
    }

    async function doConfirm(idx) {
      const m = App.qchat[idx];
      if (!m || m.resolved) return;
      m.resolved = true; render();
      push({ role: "typing" });
      let res, j = null;
      try {
        res = await fetch("/app/api/assistent/ausfuehren", { method: "POST",
          headers: { "X-CSRF-Token": App.me.csrf, "Content-Type": "application/json" },
          body: JSON.stringify({ tool: m.tool, args: m.args }) });
      } catch (e) { popTyping(); push({ role: "err", text: "Netzwerkfehler. Bitte erneut." }); return; }
      if (res.status === 303 || res.status === 401 || res.redirected) { location.href = "/app/login"; return; }
      try { j = await res.json(); } catch (e) {}
      popTyping();
      if (j && j.type === "done" && j.result && j.result.ok) {
        const html = resultHtml(m.tool, j.result);
        if (html) push({ role: "q", text: html, html: true });
        else push({ role: "q", text: "✓ " + resultText(m.tool, j.result) });
      }
      else push({ role: "err", text: (j && (j.text || (j.result && j.result.error))) || "Aktion fehlgeschlagen." });
    }

    // 📎-Upload: Bilder → als Vorschau in den Composer (Gemini/Claude-Stil).
    // Nutzer kann eine Anweisung dazu tippen und senden — Q entscheidet dann
    // (visualisieren / im Archiv ablegen / als Beleg) oder fragt nach.
    // PDFs → nach wie vor direkt als Beleg hochladen (kein Bild-Pfad).
    // Composer-Vorschau aus App.qPendingFile (neu) befüllen — wird auch beim
    // Wiederbetreten des Screens aufgerufen, damit ein „klebendes" Bild nach
    // einem Tab-Wechsel sichtbar bleibt.
    function showPendingPreview() {
      const previewEl = document.getElementById("composer-preview");
      if (!previewEl || !App.qPendingFile) return;
      const url = App.qPendingPreviewUrl;
      const name = App.qPendingFile.name || "Bild";
      previewEl.innerHTML =
        `<div class="composer-preview-item">
           ${url ? `<img src="${url}" alt="${esc(name)}">` : `<span class="composer-preview-file">📷 ${esc(name)}</span>`}
           <button class="cp-remove" id="q-preview-remove" aria-label="Anhang entfernen">✕</button>
         </div>`;
      previewEl.hidden = false;
      document.getElementById("q-preview-remove").addEventListener("click", clearPending);
    }

    function onFilePicked(file) {
      if (!file) return;
      const isImg = /^image\/(jpeg|png|webp)$/.test(file.type);
      if (isImg) {
        const previewEl = document.getElementById("composer-preview");
        if (!previewEl) return;
        // evtl. vorheriger Anhang weg, neuen Bild-Verlauf starten
        clearPending();
        App.qPendingFile = file;
        App.qImgChat = [];
        let previewUrl = null;
        try { previewUrl = URL.createObjectURL(file); } catch (_) {}
        App.qPendingPreviewUrl = previewUrl;
        showPendingPreview();
        input.focus();
      } else {
        // PDF und andere → bisheriger Flow (Beleg hochladen)
        doUploadBeleg(file);
      }
    }

    async function doArchiv(file, kundeName) {
      push({ role: "me", text: "📁 " + file.name + " → " + kundeName });
      push({ role: "typing" });
      let res, j = null;
      const url = "/app/api/archiv/upload?kunde_name=" + encodeURIComponent(kundeName)
        + "&filename=" + encodeURIComponent(file.name);
      try {
        res = await fetch(url, { method: "POST", headers: { "X-CSRF-Token": App.me.csrf, "Content-Type": file.type }, body: file });
      } catch (e) { popTyping(); push({ role: "err", text: "Upload fehlgeschlagen." }); return; }
      if (res.status === 303 || res.status === 401 || res.redirected) { location.href = "/app/login"; return; }
      try { j = await res.json(); } catch (e) {}
      popTyping();
      if (res.ok && j && j.ok) push({ role: "q", text: "📁 Im Archiv von " + kundeName + " gespeichert." });
      else push({ role: "err", text: (j && j.error) || "Konnte nicht im Archiv speichern." });
    }

    async function doVisualisieren(file, prompt) {
      push({ role: "me", text: "🖼️ " + file.name + " — „" + prompt + "“" });
      push({ role: "typing" });
      // Aus einem Kundengespräch heraus gestartet? Dann hängt das Ergebnis
      // sich dort wieder an (App.vizGespraech setzt showGespraech).
      const g = App.vizGespraech;
      const ziel = "/app/api/visualisierungen?prompt=" + encodeURIComponent(prompt) +
        (g && g.id ? "&gespraech_id=" + encodeURIComponent(g.id) : "");
      let res, j = null;
      try {
        res = await fetch(ziel,
          { method: "POST", headers: { "X-CSRF-Token": App.me.csrf, "Content-Type": file.type }, body: file });
      } catch (e) { popTyping(); push({ role: "err", text: "Netzwerkfehler beim Rendern." }); return; }
      if (res.status === 303 || res.status === 401 || res.redirected) { location.href = "/app/login"; return; }
      try { j = await res.json(); } catch (e) {}
      popTyping();
      if (res.ok && j && j.ok && j.bild_url) {
        const url = String(j.bild_url).replace(/"/g, "%22");
        push({ role: "q", html: true, text:
          `🖼️ Fertig! <a href="${url}" target="_blank" rel="noopener noreferrer">in voller Größe öffnen ↗</a>`
          + `<img src="${url}" alt="Visualisierung" style="width:100%;border-radius:10px;margin-top:8px">` });
        if (j.gespraech_id) {
          // Der Weg zurück ins Gespräch — dort liegt das Bild jetzt auch.
          const zurueck = String(j.gespraech_id);
          App.vizGespraech = null;
          push({ role: "q", html: true, text:
            `Das Bild hängt jetzt am Kundengespräch. <a href="#" id="q-viz-back">Zurück zum Gespräch ›</a>` });
          setTimeout(() => {
            const a = document.getElementById("q-viz-back");
            if (a) a.addEventListener("click", (ev) => { ev.preventDefault(); showGespraech(zurueck); });
          }, 0);
        }
      } else {
        push({ role: "err", text: (j && j.error) || "Konnte kein Bild erzeugen — bitte anderes Foto/Beschreibung versuchen." });
      }
    }

    // Objekt erkennen / Bezugsquelle suchen. Q liest das Foto, schlägt im
    // Netz nach und antwortet mit Quellen — die stehen darunter als echte
    // Links, damit der Handwerker die Anleitung selbst aufmachen kann.
    async function doObjekt(file, frage, modus) {
      const kaufen = modus === "kaufen";
      App.qImgChat = App.qImgChat || [];
      let bubbleUrl = null;
      try { bubbleUrl = URL.createObjectURL(file); } catch (_) {}
      push({ role: "me", text: frage || (kaufen ? "Wo bekomme ich das?" : "Was ist das?"),
             previewUrl: bubbleUrl, fileName: file.name });
      push({ role: "typing" });
      const hist = encodeURIComponent(JSON.stringify(
        App.qImgChat.slice(-6).map((t) => ({ role: t.role, text: (t.text || "").slice(0, 500) }))));
      const url = "/app/api/objekt/frage?frage=" + encodeURIComponent(frage || "") +
        "&modus=" + (kaufen ? "kaufen" : "doku") + "&hist=" + hist;
      let res, j = null;
      try {
        res = await fetch(url, { method: "POST",
          headers: { "X-CSRF-Token": App.me.csrf, "Content-Type": file.type }, body: file });
      } catch (_) { popTyping(); push({ role: "err", text: "Netzwerkfehler bei der Suche." }); return; }
      if (res.status === 303 || res.status === 401 || res.redirected) { location.href = "/app/login"; return; }
      try { j = await res.json(); } catch (_) {}
      popTyping();
      if (!j || !j.ok) {
        push({ role: "err", text: (j && j.error) || "Konnte nichts dazu finden." });
        return;
      }
      const quellen = j.quellen || [];
      let html = linkify(esc(j.text || "")).replace(/\n/g, "<br>");
      if (quellen.length) {
        html += `<div style="margin-top:10px;border-top:1px solid var(--line);padding-top:8px">` +
          `<div class="sub" style="margin-bottom:4px">${kaufen ? "Bezugsquellen" : "Quellen"}</div>` +
          quellen.map((q) =>
            `<div style="margin:3px 0"><a href="${esc(q.url)}" target="_blank" rel="noopener noreferrer">` +
            `${esc(q.domain || q.titel)} ↗</a></div>`).join("") + `</div>`;
      }
      push({ role: "q", html: true, text: html });
      App.qImgChat.push({ role: "user", text: frage || "(Bild)" });
      App.qImgChat.push({ role: "model", text: (j.text || "").slice(0, 900) });
      // Beim Nachkaufen: den Fund direkt in den Material-Katalog übernehmen,
      // dann ist es beim nächsten Mal ein Knopfdruck statt einer neuen Suche.
      if (kaufen && quellen.length) merkenAnbieten(j.text || "", quellen);
    }

    // Kleines Formular unter der Kauf-Antwort: Name + Quelle wählen → landet
    // als Material mit Bestell-Link im Katalog.
    function merkenAnbieten(antwort, quellen) {
      const vorschlag = (antwort.split("\n")[0] || "")
        .replace(/\*\*/g, "").replace(/^[-•*\s]+/, "").slice(0, 120);
      const id = "mrk" + Date.now();
      push({ role: "q", html: true, text:
        `<div id="${id}">
           <div class="sub" style="margin-bottom:6px">Als Material merken?</div>
           <input type="text" data-mrk="name" value="${esc(vorschlag)}" placeholder="Name des Teils"
                  style="width:100%;padding:10px;border:1px solid var(--line);border-radius:9px;margin-bottom:6px;font-size:15px;min-width:0" />
           <select data-mrk="link" style="width:100%;padding:10px;border:1px solid var(--line);border-radius:9px;margin-bottom:6px;font-size:15px;min-width:0">
             ${quellen.map((q) => `<option value="${esc(q.url)}">${esc(q.domain || q.titel)}</option>`).join("")}
           </select>
           <button class="btn-sm btn-ghost" data-mrk="save" style="width:100%">Merken</button>
           <div class="sub" data-mrk="status" style="margin-top:6px"></div>
         </div>` });
      setTimeout(() => {
        const box = document.getElementById(id);
        if (!box) return;
        const btn = box.querySelector('[data-mrk="save"]');
        const stat = box.querySelector('[data-mrk="status"]');
        btn.addEventListener("click", async () => {
          const name = box.querySelector('[data-mrk="name"]').value.trim();
          const link = box.querySelector('[data-mrk="link"]').value;
          const sel = box.querySelector('[data-mrk="link"]');
          if (name.length < 2) { stat.textContent = "Bitte einen Namen eintragen."; return; }
          btn.disabled = true; stat.textContent = "Speichere …";
          const r = await api("/app/api/objekt/merken", { method: "POST",
            body: JSON.stringify({ name, bestell_link: link,
              lieferant: sel.options[sel.selectedIndex].text }) });
          const d = r ? await r.json().catch(() => null) : null;
          if (d && d.ok) { box.innerHTML = `<div class="sub">✓ „${esc(name)}" liegt jetzt im Material-Katalog.</div>`; }
          else { btn.disabled = false; stat.textContent = (d && d.error) || "Konnte nicht speichern."; }
        });
      }, 0);
    }

    async function doUploadBeleg(file) {
      if (!file) return;
      push({ role: "me", text: "📄 " + file.name });
      push({ role: "typing" });
      let res, j = null;
      try {
        res = await fetch("/app/api/belege/upload?caption=" + encodeURIComponent("Hochgeladen über Q"),
          { method: "POST", headers: { "X-CSRF-Token": App.me.csrf, "Content-Type": file.type }, body: file });
      } catch (e) { popTyping(); push({ role: "err", text: "Upload fehlgeschlagen." }); return; }
      if (res.status === 303 || res.status === 401 || res.redirected) { location.href = "/app/login"; return; }
      try { j = await res.json(); } catch (e) {}
      popTyping();
      if (res.ok && j && j.ok) push({ role: "q", text: j.duplikat ? "Den Beleg hatte ich schon — kein Doppel-Upload." : "📄 Beleg gespeichert." });
      else push({ role: "err", text: (j && j.error) || "Beleg konnte nicht gespeichert werden." });
    }

    sendBtn.addEventListener("click", send);
    input.addEventListener("input", auto);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
    attachBtn.addEventListener("click", () => fileEl.click());
    fileEl.addEventListener("change", () => { if (fileEl.files && fileEl.files[0]) onFilePicked(fileEl.files[0]); fileEl.value = ""; });
    cameraBtn.addEventListener("click", () => cameraFileEl.click());
    cameraFileEl.addEventListener("change", () => { if (cameraFileEl.files && cameraFileEl.files[0]) onFilePicked(cameraFileEl.files[0]); cameraFileEl.value = ""; });

    // Paste-Handler: Bilder aus Zwischenablage (Strg+V / Cmd+V) direkt anhängen
    function onPaste(e) {
      const items = (e.clipboardData || {}).items;
      if (!items) return;
      for (const item of items) {
        if (/^image\//.test(item.type)) {
          const file = item.getAsFile();
          if (file) { e.preventDefault(); onFilePicked(file); break; }
        }
      }
    }
    if (App._qPasteListener) document.removeEventListener("paste", App._qPasteListener);
    App._qPasteListener = onPaste;
    document.addEventListener("paste", onPaste);

    // Drag & Drop: Bilder auf den Chat-Bereich ziehen
    chatEl.addEventListener("dragover", (e) => { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; });
    chatEl.addEventListener("drop", (e) => {
      e.preventDefault();
      const files = e.dataTransfer.files;
      if (files && files[0]) onFilePicked(files[0]);
    });

    // ---- Sprechen: einmal tippen startet, einmal tippen sendet -------------
    // Steuerung siehe createVoiceRecorder(); hier haengt nur die Optik dieses
    // Screens dran (Globus, Hinweistext, Mikro im Composer, Eingabezeile).
    const composerInner = App.view.querySelector(".composer-inner");
    primeMicPermissionState();

    const voice = createVoiceRecorder({
      bar: document.getElementById("q-recbar"),
      wave: document.getElementById("q-rec-wave"),
      time: document.getElementById("q-rec-time"),
      onUi(on) {
        const wrap = document.getElementById("q-sphere-wrap");
        const hint = document.getElementById("q-sphere-hint");
        const mini = document.getElementById("q-mini-sphere");
        if (wrap) wrap.classList.toggle("recording", on);
        if (mini) mini.classList.toggle("recording", on);
        if (hint) {
          hint.textContent = on ? SPRECH_HINT_REC : SPRECH_TITEL;
          hint.classList.toggle("rec", on);
        }
        if (composerInner) composerInner.hidden = on;
        if (App.qSphereActive) App.qSphereActive(on);
      },
      onHint(text) {
        const hint = document.getElementById("q-sphere-hint");
        if (hint) hint.textContent = text;
      },
      onText(text) { input.value = text; send(); },
      onError(msg) { push({ role: "err", text: msg }); },
    });

    // render() baut den Globus neu auf und braucht die Aufnahme-Optik zurueck.
    App.qVoice = voice;
    // Tab-Wechsel/Verlassen: Mikrofon nicht offen stehen lassen.
    App.recAbort = () => voice.stop("cancel");

    // Globus (Hero-Ansicht)
    chatEl.addEventListener("pointerdown", (e) => {
      const wrap = e.target.closest("#q-sphere-wrap");
      if (!wrap) return;
      e.preventDefault();
      try { wrap.setPointerCapture(e.pointerId); } catch (_) {}
      voice.pointerDown();
    });
    chatEl.addEventListener("pointerup", (e) => {
      if (e.target.closest("#q-sphere-wrap")) voice.pointerUp();
    });
    chatEl.addEventListener("pointercancel", () => voice.pointerCancel());
    // Tastatur: der Globus ist role="button" — Enter/Leertaste schalten um.
    chatEl.addEventListener("keydown", (e) => {
      if (!e.target.closest("#q-sphere-wrap")) return;
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault();
      voice.toggle();
    });

    // Mikro im Composer (sichtbar, sobald Nachrichten da sind)
    const miniBtn = document.getElementById("q-mini-sphere");
    if (miniBtn) {
      miniBtn.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        try { miniBtn.setPointerCapture(e.pointerId); } catch (_) {}
        voice.pointerDown();
      });
      miniBtn.addEventListener("pointerup", () => voice.pointerUp());
      miniBtn.addEventListener("pointercancel", () => voice.pointerCancel());
    }

    // Knöpfe der Aufnahme-Leiste
    const recCancelBtn = document.getElementById("q-rec-cancel");
    const recSendBtn = document.getElementById("q-rec-send");
    if (recCancelBtn) recCancelBtn.addEventListener("click", () => voice.stop("cancel"));
    if (recSendBtn) recSendBtn.addEventListener("click", () => voice.stop("send"));

    // ---- Funktions-Dropdown (Quick-Aktionen) ----
    // Damit der Handwerker nicht alles per Hand in eigenen Fenstern anlegt:
    // er tippt eine Funktion an, Q bekommt den passenden Satz vorgeschrieben
    // und legt die Sache an (Schreib-Aktionen weiterhin mit Bestätigung).
    const actionsBtn = document.getElementById("q-actions");
    const menuEl = document.getElementById("q-menu");
    const feats = new Set(App.me.features || []);
    // `intent` = vollständiger Starter-Satz. Q (Gemini) übernimmt damit den Flow
    // und fragt fehlende Angaben selbst nach — wir tippen nichts vor.
    const QACTIONS = [
      { ico: "📅", label: "Termin eintragen",   intent: "Ich möchte einen Termin eintragen.",          feature: "kalender" },
      { ico: "🔁", label: "Termin verschieben",  intent: "Ich möchte einen Termin verschieben.",        feature: "kalender" },
      { ico: "📞", label: "Rückruf anlegen",     intent: "Ich möchte einen Rückruf anlegen." },
      { ico: "📁", label: "Drive-Ordner anlegen", intent: "Ich möchte einen Drive-Ordner für einen Kunden anlegen.", feature: "drive_archiv" },
      { ico: "📝", label: "Notiz in Drive ablegen", intent: "Ich möchte eine Notiz für einen Kunden in Drive ablegen.", feature: "drive_archiv" },
      { ico: "🧰", label: "Material bestellen",  intent: "Ich möchte Material bestellen." },
      { ico: "📚", label: "Wissen merken",       intent: "Ich möchte mir etwas in der Wissensdatenbank merken." },
      { ico: "🔍", label: "Kunde nachschlagen",  intent: "Ich möchte einen Kunden nachschlagen." },
      { ico: "✉️", label: "Anfrage beantworten", intent: "Ich möchte eine Kundenanfrage beantworten.",  feature: "mail_intake" },
      { ico: "📧", label: "E-Mail schreiben",    intent: "Ich möchte eine E-Mail schreiben." },
      { ico: "📄", label: "Angebot erstellen",   intent: "Ich möchte ein Angebot erstellen.",           feature: "lexware", perm: "buchhaltung.fuehren" },
      { ico: "🧾", label: "Rechnung erstellen",  intent: "Ich möchte eine Rechnung schreiben.",          feature: "lexware", perm: "buchhaltung.fuehren" },
      { ico: "🎨", label: "Visualisierung",      viz: true,                                              feature: "visualisierung" },
    ].filter((a) => (!a.feature || feats.has(a.feature)) && (!a.perm || can(a.perm)));

    menuEl.innerHTML = QACTIONS.map((a, i) =>
      `<button data-qa="${i}"><span class="qm-ico">${a.ico}</span>${esc(a.label)}</button>`).join("");

    function toggleQMenu(show) {
      const open = show === undefined ? menuEl.hidden : show;
      menuEl.hidden = !open;
      actionsBtn.classList.toggle("active", open);
    }
    actionsBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleQMenu(); });
    // Klick außerhalb schließt das Menü (alten Listener vorher entfernen, kein Leak).
    if (App._qMenuDocClick) document.removeEventListener("click", App._qMenuDocClick);
    App._qMenuDocClick = (e) => {
      if (!menuEl.hidden && !menuEl.contains(e.target) && e.target !== actionsBtn) toggleQMenu(false);
    };
    document.addEventListener("click", App._qMenuDocClick);
    menuEl.querySelectorAll("[data-qa]").forEach((b) =>
      b.addEventListener("click", () => {
        const a = QACTIONS[parseInt(b.dataset.qa, 10)];
        toggleQMenu(false);
        if (a.viz) { navigate("visualisierung"); return; }
        startIntent(a);
      }));

    // ---- Q-geführtes Onboarding (ersetzt das alte Overlay-Tutorial) -------
    // Ein scripted Schritt-für-Schritt-Ablauf direkt im Chat: Q erklärt sich,
    // bietet klickbare Verbinden-Karten (Google/Outlook/Lexware/Push) an und
    // geht weiter, sobald ein Schritt erledigt oder übersprungen ist. Die
    // Karten brauchen einen echten Tap (OAuth-Popup-Blocker) — Q kann nicht
    // selbst klicken. obNext() rückt das Skript vor; Aktions-Karten rufen es
    // bei Auflösung selbst auf, Text-Schritte unmittelbar nach der Ansage.
    let obNext = () => {};

    function obSay(html, cb) {
      push({ role: "typing" });
      setTimeout(() => {
        popTyping();
        App.qchat.push({ role: "q", text: html, html: true });
        render(); scrollDown();
        if (cb) cb();
      }, 650);
    }

    function obCard(kind) { push({ role: "onb", kind, resolved: false }); }

    function obSkip(i) {
      const m = App.qchat[i];
      if (!m || m.resolved) return;
      m.resolved = true; m.status = "skip"; render();
      obNext();
    }

    const OB_OK = { google: "Top, Google ist verbunden! ✓", microsoft: "Super, Outlook ist verbunden! ✓",
                    lexware: "Perfekt, Lexware ist verbunden! ✓" };

    function obConn(spec) {
      const ci = spec.indexOf(":");
      const i = parseInt(spec.slice(0, ci), 10);
      const provider = spec.slice(ci + 1);
      const m = App.qchat[i];
      if (!m || m.resolved) return;
      // OAuth-Popup MUSS synchron im Klick-Gesture geöffnet werden (Blocker);
      // Ziel-URL erst setzen, sobald die Authorize-URL da ist.
      const w = window.open("", "ga_oauth", "width=520,height=720");
      api("/app/api/oauth/start", { method: "POST", body: JSON.stringify({ provider }) })
        .then((r) => (r && r.ok ? r.json() : null))
        .then((j) => {
          if (j && j.ok && j.auth_url) {
            if (w) {
              w.location = j.auth_url;
              const iv = setInterval(() => { if (w.closed) { clearInterval(iv); obAfterOAuth(i, provider); } }, 1000);
              setTimeout(() => clearInterval(iv), 300000);
            } else { window.location = j.auth_url; }
          } else {
            if (w) w.close();
            m.resolved = true; m.status = "fail"; render();
            obSay((j && j.error) || "Das hat nicht geklappt — du kannst es später unter „Mehr → Einstellungen“ nachholen.", obNext);
          }
        })
        .catch(() => {
          if (w) w.close();
          m.resolved = true; m.status = "fail"; render();
          obSay("Das hat nicht geklappt — du kannst es später unter „Mehr → Einstellungen“ nachholen.", obNext);
        });
    }

    async function obAfterOAuth(i, provider) {
      const m = App.qchat[i];
      if (!m || m.resolved) return;
      let connected = false;
      try {
        const r = await api("/app/api/verbindungen");
        if (r && r.ok) { const v = await r.json(); connected = !!((v[provider] || {}).connected); }
      } catch (e) {}
      m.resolved = true; m.status = connected ? "ok" : "skip"; render();
      obSay(connected ? OB_OK[provider] : "Kein Problem — das kannst du später unter „Mehr → Einstellungen“ nachholen.", obNext);
    }

    async function obLexSave(i) {
      const m = App.qchat[i];
      if (!m || m.resolved) return;
      const inp = chatEl.querySelector(`[data-ob-lexkey="${i}"]`);
      const msg = chatEl.querySelector(`[data-ob-msg="${i}"]`);
      const key = ((inp && inp.value) || "").trim();
      if (key.length < 20) { if (msg) msg.textContent = "Bitte einen gültigen Schlüssel eingeben."; return; }
      if (msg) msg.textContent = "Prüfe …";
      const rr = await api("/app/api/lexware/verbinden", { method: "POST", body: JSON.stringify({ api_key: key }) });
      const j = rr ? await rr.json().catch(() => null) : null;
      if (j && j.ok) { m.resolved = true; m.status = "ok"; render(); obSay(OB_OK.lexware, obNext); }
      else if (msg) { msg.textContent = (j && j.error) || "Konnte nicht speichern."; }
    }

    async function obPush(i) {
      const m = App.qchat[i];
      if (!m || m.resolved) return;
      let ok = false;
      try {
        if (notifSupported() && "serviceWorker" in navigator && "PushManager" in window && App.me.vapid_public_key) {
          const perm = await Notification.requestPermission();
          if (perm === "granted") {
            const reg = await navigator.serviceWorker.ready;
            const sub = await reg.pushManager.subscribe({
              userVisibleOnly: true,
              applicationServerKey: urlBase64ToUint8Array(App.me.vapid_public_key),
            });
            await api("/app/api/push/subscribe", { method: "POST", body: JSON.stringify({ subscription: sub }) });
            ok = true;
          }
        }
      } catch (e) { console.error(e); }
      const nb = document.getElementById("notif-btn");
      if (ok && nb) nb.hidden = true;
      m.resolved = true; m.status = ok ? "ok" : "skip"; render();
      obSay(ok ? "Benachrichtigungen sind an ✓" : "Okay — du kannst sie später unter „Mehr“ aktivieren.", obNext);
    }

    function obDone() {
      if (App.me) App.me.onboarding_done = true;
      api("/app/api/onboarding/complete", { method: "POST", body: "{}" }).catch(() => {});
    }

    async function runOnboarding() {
      App.qchat = []; App.qhistory = []; render();
      const isInhaber = !!(App.me.employee && App.me.employee.is_inhaber);
      const first = (((App.me.employee && App.me.employee.name) || "").trim().split(/\s+/)[0]) || "";
      let v = {};
      if (isInhaber) {
        try { const r = await api("/app/api/verbindungen"); if (r && r.ok) v = await r.json(); } catch (e) {}
      }

      const steps = [];
      steps.push((n) => obSay(`${first ? "Hallo " + esc(first) + "!" : "Willkommen!"} Ich bin <b>Q</b> — dein digitaler Büro-Mitarbeiter. Ich gehe ans Telefon, beantworte Kunden-Mails, buche Termine und schreibe Angebote &amp; Rechnungen.`, n));
      steps.push((n) => obSay(`Wir machen dich in ein paar Schritten startklar. Du hast drei Bereiche: <b>Assistent</b> (hier mit mir reden), <b>Aktionen</b> (Briefing, Anfragen, Aufträge) und <b>Mehr</b> (Kunden, Team, Einstellungen).`, n));

      if (isInhaber) {
        steps.push((n) => obSay("Damit ich wirklich für dich arbeiten kann, verbinden wir kurz deine Konten. Alles optional und jederzeit später unter „Mehr → Einstellungen“ änderbar.", n));
        const g = v.google || {};
        if (g.connected) steps.push((n) => obSay(`Dein Google-Konto ist schon verbunden ✓${g.account ? " (" + esc(g.account) + ")" : ""}.`, n));
        else steps.push(() => obSay("Zuerst dein <b>Google</b>-Konto — damit ich Termine in deinen Kalender buche und Kunden-Dateien im Drive ablege.", () => obCard("google")));

        const ms = v.microsoft || {};
        if (ms.available) {
          if (ms.connected) steps.push((n) => obSay("Outlook ist schon verbunden ✓.", n));
          else steps.push(() => obSay("Als Nächstes <b>Microsoft / Outlook</b> — für dein Mail-Postfach und den Kalender.", () => obCard("microsoft")));
        }

        const feats = new Set(App.me.features || []);
        if (feats.has("lexware")) {
          const lx = v.lexware || {};
          if (lx.connected) steps.push((n) => obSay("Lexware ist schon verbunden ✓.", n));
          else steps.push(() => obSay("Und <b>Lexware Office</b> — damit ich Angebote &amp; Rechnungen erstellen und versenden kann.", () => obCard("lexware")));
        }
      } else {
        steps.push((n) => obSay("Die Konten (Google, Outlook, Lexware) richtet der Inhaber deines Betriebs ein — du kannst mich trotzdem sofort nutzen.", n));
      }

      if (notifSupported() && !notifGranted()) {
        steps.push(() => obSay("Zum Schluss: Aktiviere Benachrichtigungen, damit du neue Anfragen und Rückrufe sofort mitbekommst.", () => obCard("push")));
      }

      steps.push(() => { obDone(); obSay("Fertig — du bist startklar! 🎉 Frag mich einfach, was du brauchst: tippe es ein oder <b>tippe den Globus an</b> und sprich es mir — noch ein Tipp schickt die Aufnahme ab. Über das <b>✨-Symbol</b> links neben dem Eingabefeld findest du fertige Funktionen wie Termin eintragen, E-Mail schreiben oder etwas merken.", null); });

      let idx = 0;
      obNext = () => { if (idx < steps.length) { const fn = steps[idx++]; fn(obNext); } };
      obNext();
    }

    render();
    auto();
    // Ein „klebendes" Bild (über Tab-Wechsel hinweg) wieder als Vorschau zeigen.
    if (App.qPendingFile) showPendingPreview();
    // Q-Onboarding beim ersten Start (oder „Mehr → Einrichtung starten").
    if (App.startOnboarding) { App.startOnboarding = false; runOnboarding(); }
    // Visualisierung direkt aus dem Bild-Archiv-Preview gestartet.
    else if (App._autoVizPrompt && App.qPendingFile) {
      const p = App._autoVizPrompt; App._autoVizPrompt = null;
      doVisualisieren(App.qPendingFile, p);
    }
    // Vorbefüllung aus einem Quick-Aktion/„Angebot erstellen"-Tap.
    else if (App.qSeed) { input.value = App.qSeed; App.qSeed = null; auto(); input.focus(); input.setSelectionRange(input.value.length, input.value.length); }
    // Im leeren Chat NICHT fokussieren: sonst poppt die Tastatur und verdeckt
    // die Sphere. Erst fokussieren, wenn schon ein Verlauf da ist.
    else if (App.qchat.length) input.focus();
  },
};

// Q-Tagesbriefing laden + in die Karte schreiben. refresh=true erzwingt eine
// Neuberechnung (sonst kommt die gecachte Tagesfassung sofort).
async function loadBriefing(refresh) {
  const el = document.getElementById("q-briefing-text");
  if (!el) return;
  if (refresh) el.innerHTML = '<span class="q-briefing-load">Q schreibt dein Briefing …</span>';
  const r = await api("/app/api/briefing" + (refresh ? "?refresh=1" : ""));
  if (!r) return; // api() hat ggf. zur Login-Seite umgeleitet
  let j = null;
  try { j = await r.json(); } catch (e) {}
  const cur = document.getElementById("q-briefing-text");
  if (!cur) return; // Tab inzwischen gewechselt
  cur.textContent = (j && j.ok && j.text) ? j.text : "Briefing gerade nicht verfügbar.";
}

// Q hat im Chat eine Ansicht angefordert ("zeig mir die Rechnungen") → die
// passende Stelle öffnen. Anzeigen leben in "Aktuelles" (dorthin + zum
// Abschnitt scrollen); Kleinkram (Kunden/Wissen/…) als eigener Screen.
function handleNavigate(bereich, kunde, kategorie) {
  const b = (bereich || "aktuelles").toLowerCase();
  if (b === "kunden_profil" && kunde) { showKundenProfil(kunde); return; }
  if (b === "kunden_archiv" && kunde) { openArchivKategorie(kunde, kategorie || "bilder"); return; }
  const mehr = { kunden: 1, wissen: 1, material: 1, team: 1, einstellungen: 1, formulare: 1 };
  // "Beleg erfassen" ist ein Formular, kein Screen — direkt oeffnen; sein
  // Zurueck-Knopf fuehrt in den Buchhaltungs-Bereich.
  if (b === "belege") { showBelegUpload(); return; }
  const subscreen = {
    auftraege: "auftraege_page", rechnungen: "rechnungen_page",
    angebote: "angebote_page", rueckrufe: "rueckrufe_page",
    anfragen: "anfragen", termine: "termine", aufnahmen: "gespraeche",
    gespraeche: "gespraeche", buchhaltung: "buchhaltung",
  };
  if (mehr[b]) { navigate(b); return; }
  if (subscreen[b]) { navigate(subscreen[b]); return; }
  navigate("aktuelles");
}

// Lädt Dateien für einen Kunden und öffnet direkt eine Archiv-Kategorie-Unterseite.
async function openArchivKategorie(kundeName, kategorieKey) {
  App.view.innerHTML = `<div class="loading">Lädt …</div>`;
  const res = await api("/app/api/archiv/dateien?kunde_name=" + encodeURIComponent(kundeName));
  const j = res ? await res.json().catch(() => null) : null;
  const all = (j && j.ok && j.dateien) ? j.dateien : [];
  const fileMap = Object.fromEntries(all.map((f) => [f.id, f]));
  const defs = [
    { key: "bilder",  label: "Bilder",  ico: "📷", files: all.filter((f) => f.is_image) },
    { key: "pdfs",    label: "PDFs",    ico: "📄", files: all.filter((f) => !f.is_image && f.mime_type === "application/pdf") },
    { key: "notizen", label: "Notizen", ico: "📝", files: all.filter((f) => !f.is_image && f.mime_type === "text/plain") },
  ];
  const group = defs.find((g) => g.key === kategorieKey) || defs[0];
  showArchivKategorie(kundeName, group, fileMap);
}

function row(a, b, c) {
  return `<div class="row"><div><div>${esc(a)}</div>${b ? `<div class="sub">${esc(b)}</div>` : ""}</div>${c ? `<span class="sub">${esc(c)}</span>` : ""}</div>`;
}
function rowAction(a, b, c, id, action, label) {
  return `<div class="row"><div><div>${esc(a)}</div>${b ? `<div class="sub">${esc(b)}</div>` : ""}</div>` +
    `<button class="btn-sm btn-ghost" data-action="${action}" data-id="${esc(id)}">${label}</button></div>`;
}
function rowPill(a, b, status, pill) {
  return `<div class="row"><div><div>${esc(a)}</div>${b ? `<div class="sub">${esc(b)}</div>` : ""}</div>` +
    `<span class="pill ${pill || ""}">${esc(status)}</span></div>`;
}
// Wie rowPill, nur dass die ganze Zeile nach Lexware verlinkt, wenn es dort
// einen Beleg gibt. Der Deeplink existierte im Backend schon lange
// (invoice_deeplink_view / quotation_deeplink_view), wurde in der App aber
// nirgends genutzt — Rechnung ansehen hiess: Lexware selbst suchen.
function rowPillLink(a, b, status, pill, link) {
  const inner =
    `<div><div>${esc(a)}</div>${b ? `<div class="sub">${esc(b)}</div>` : ""}</div>` +
    `<span class="pill ${pill || ""}">${esc(status)}${link ? " ›" : ""}</span>`;
  if (!link) return `<div class="row">${inner}</div>`;
  return `<a class="row" href="${esc(link)}" target="_blank" rel="noopener" ` +
    `style="text-decoration:none;color:inherit">${inner}</a>`;
}

// ---------- Buchhaltung: Kennzahl-Kachel, offener Posten, Abschnitt ----------
function fmtEur(v) {
  const n = Number(v || 0);
  return n.toLocaleString("de-DE", { style: "currency", currency: "EUR" });
}
function geldKpi(label, betrag, sub, ton) {
  return `<div class="geld-kpi ${ton || ""}">
    <span class="geld-kpi-label">${esc(label)}</span>
    <span class="geld-kpi-wert">${esc(fmtEur(betrag))}</span>
    <span class="geld-kpi-sub">${esc(sub || "")}</span>
  </div>`;
}
function postenRow(p, isInhaber) {
  // Drei Faelle: ueberfaellig (rot), versendet und noch in der Frist,
  // oder noch gar nicht raus (Entwurf liegt beim Betrieb selbst).
  const stand = !p.versendet
    ? ["Nicht versendet", "warn", `Entwurf seit ${p.tage} Tagen`]
    : p.ueberfaellig
      ? ["Überfällig", "danger", `versendet vor ${p.tage} Tagen`]
      : ["Offen", "", `versendet vor ${p.tage} Tagen`];
  const titel = p.kunde + (p.nummer ? " · " + p.nummer : "");
  const zeile = rowPillLink(titel, `${fmtEur(p.betrag_eur)} · ${stand[2]}`,
                            stand[0], stand[1], p.lexware_link);
  // Erinnern gibt es nur, wenn die Rechnung wirklich beim Kunden ist.
  // Ein Entwurf muss erst raus — daran zu erinnern wäre absurd.
  if (!isInhaber || !p.versendet) return zeile;
  return zeile +
    `<div style="margin:-4px 0 10px"><button class="btn-sm btn-ghost" data-erinnern="zahlung" data-id="${esc(p.id)}" style="padding:7px 12px">✉️ Erinnern</button></div>`;
}

// Entwurf holen, zeigen, ändern lassen, senden. Ein Bildschirm für beides
// (Zahlungserinnerung + Angebot nachfassen) — der Ablauf ist derselbe.
async function zeigeErinnerung(typ, id) {
  const zurueck = () => navigate("buchhaltung", { mode: "none" });
  App.view.innerHTML = `<div class="loading">Q schreibt …</div>`;
  const res = await api("/app/api/erinnerung/entwurf", {
    method: "POST", body: JSON.stringify({ typ, id }),
  });
  const d = res ? await res.json().catch(() => null) : null;
  if (!d || !d.ok) {
    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="er-back" style="margin-bottom:10px">← Buchhaltung</button>` +
      `<div class="card">${emptyRow((d && d.error) || "Konnte keinen Entwurf schreiben.")}</div>`;
    document.getElementById("er-back").addEventListener("click", zurueck);
    return;
  }
  const inp = "width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px;min-width:0";
  const istZahlung = typ === "zahlung";
  const toene = [["freundlich", "Freundlich"], ["bestimmt", "Bestimmt"], ["letzte", "Letzte Frist"]];
  App.view.innerHTML =
    `<button class="btn-sm btn-ghost" id="er-back" style="margin-bottom:10px">← Buchhaltung</button>` +
    `<h1 style="font-size:22px;margin:4px 4px 4px">${istZahlung ? "Zahlungserinnerung" : "Angebot nachfassen"}</h1>` +
    `<p class="muted" style="margin:0 4px 14px">${esc(d.kunde)} · ${esc(d.betrag)} · seit ${d.tage} Tagen</p>` +
    (istZahlung
      ? `<div class="card"><label class="sub">Tonfall</label>
           <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:6px">
             ${toene.map(([w, l]) => `<button class="btn-sm ${w === d.ton ? "" : "btn-ghost"}" data-ton="${w}" style="flex:1 1 30%;padding:10px 6px">${l}</button>`).join("")}
           </div></div>` : "") +
    `<div class="card">
       <label class="sub">Empfänger</label>
       <input type="email" id="er-mail" value="${esc(d.empfaenger || "")}" placeholder="kunde@example.de" style="${inp}" />
       <label class="sub">Betreff</label>
       <input type="text" id="er-betreff" value="${esc(d.betreff || "")}" style="${inp}" />
       <label class="sub">Text</label>
       <textarea id="er-text" rows="10" style="${inp};font-family:inherit">${esc(d.text || "")}</textarea>
       <p class="muted" style="margin:0 0 10px;font-size:12px">Grußformel und Kontaktdaten hängt das System selbst an.</p>
       <button class="btn-sm" id="er-send" style="width:100%">Senden</button>
       <p class="muted" id="er-status" style="margin:10px 0 0;min-height:18px"></p>
     </div>`;
  document.getElementById("er-back").addEventListener("click", zurueck);
  // Tonfall wechseln = neu schreiben lassen. Was der Nutzer schon selbst
  // getippt hat, geht dabei verloren — deshalb erst fragen.
  document.querySelectorAll("[data-ton]").forEach((b) =>
    b.addEventListener("click", async () => {
      if (b.dataset.ton === d.ton) return;
      const feld = document.getElementById("er-text");
      if (feld.value.trim() !== (d.text || "").trim() &&
          !confirm("Neu schreiben lassen? Deine Änderungen am Text gehen verloren.")) return;
      feld.value = "Q schreibt …";
      const r = await api("/app/api/erinnerung/entwurf", {
        method: "POST", body: JSON.stringify({ typ, id, ton: b.dataset.ton }),
      });
      const n = r ? await r.json().catch(() => null) : null;
      if (n && n.ok) {
        d.ton = n.ton; d.text = n.text;
        feld.value = n.text;
        document.querySelectorAll("[data-ton]").forEach((x) =>
          x.classList.toggle("btn-ghost", x.dataset.ton !== n.ton));
      } else { feld.value = d.text || ""; }
    }));
  const send = document.getElementById("er-send");
  const stat = document.getElementById("er-status");
  send.addEventListener("click", async () => {
    const empfaenger = document.getElementById("er-mail").value.trim();
    if (!empfaenger) { stat.textContent = "Bitte die Empfänger-Adresse eintragen."; return; }
    send.disabled = true; stat.textContent = "Sende …";
    const r = await api("/app/api/erinnerung/senden", {
      method: "POST",
      body: JSON.stringify({
        empfaenger,
        betreff: document.getElementById("er-betreff").value.trim(),
        text: document.getElementById("er-text").value.trim(),
      }),
    });
    const j = r ? await r.json().catch(() => null) : null;
    if (j && j.ok) {
      App.view.innerHTML =
        `<div class="card"><h2>✓ Verschickt</h2>
           <p class="muted" style="margin:6px 0 0">${esc(d.kunde)} hat die Nachricht bekommen.</p></div>` +
        `<button class="btn-sm btn-ghost" id="er-back2" style="width:100%;margin-top:10px">← Buchhaltung</button>`;
      document.getElementById("er-back2").addEventListener("click", zurueck);
    } else {
      send.disabled = false;
      stat.textContent = (j && j.error) || "Konnte nicht senden.";
    }
  });
}
// Beleg-Vorkontierung: Gemini liest den eben hochgeladenen Beleg und
// schlaegt Händler/Datum/Betrag/Steuersatz/Kategorie vor. Alles bleibt
// änderbar — gebucht wird erst auf Tipp. Scheitert irgendetwas, verschwindet
// die Karte einfach; der Beleg liegt trotzdem in Lexware.
async function zeigeKontierung(belegId) {
  const box = document.getElementById("bl-kontierung");
  if (!box) return;
  box.innerHTML = `<div class="card"><div class="loading" style="padding:6px 0">Q liest den Beleg …</div></div>`;
  let j = null;
  try {
    const res = await api(`/app/api/belege/${encodeURIComponent(belegId)}/vorschlag`,
                          { method: "POST", body: "{}" });
    j = res && res.ok ? await res.json() : null;
  } catch (e) { /* egal — s.o. */ }
  if (!j || !j.ok) { box.innerHTML = ""; return; }
  if (!j.ist_beleg) {
    box.innerHTML = `<div class="card"><p class="muted" style="margin:0">${esc(j.hinweis || "Konnte den Beleg nicht lesen.")}</p></div>`;
    return;
  }
  const v = j.vorschlag || {};
  const kats = j.kategorien || [];
  const inp = "width:100%;padding:11px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px;min-width:0";
  const unsicher = v.sicherheit !== "hoch";
  box.innerHTML =
    `<div class="card">
       <h2>Vorschlag von Q</h2>
       ${unsicher ? `<p class="muted" style="margin:0 0 10px">Bitte kurz prüfen — ich war mir nicht ganz sicher.</p>` : ""}
       <label class="sub">Händler</label>
       <input type="text" id="kt-haendler" value="${esc(v.haendler || "")}" style="${inp}" />
       <label class="sub">Belegdatum</label>
       <input type="date" id="kt-datum" value="${esc(v.datum || "")}" style="${inp}" />
       <div style="display:flex;gap:8px">
         <div style="flex:1;min-width:0">
           <label class="sub">Betrag brutto</label>
           <input type="number" step="0.01" inputmode="decimal" id="kt-betrag" value="${esc(v.betrag_brutto_eur != null ? v.betrag_brutto_eur : "")}" style="${inp}" />
         </div>
         <div style="flex:0 0 96px;min-width:0">
           <label class="sub">MwSt</label>
           <select id="kt-mwst" style="${inp}">
             ${[19, 7, 0].map((s) => `<option value="${s}"${(v.mwst_prozent === s || (v.mwst_prozent == null && s === 19)) ? " selected" : ""}>${s} %</option>`).join("")}
           </select>
         </div>
       </div>
       <label class="sub">Buchungskategorie</label>
       <select id="kt-kategorie" style="${inp}">
         <option value="">— keine —</option>
         ${kats.map((k) => `<option value="${esc(k)}"${k === v.kategorie ? " selected" : ""}>${esc(k)}</option>`).join("")}
       </select>
       <button class="btn-sm" id="kt-save" style="width:100%"${j.kontierbar ? "" : " disabled"}>In Lexware übernehmen</button>
       <p class="muted" id="kt-status" style="margin:10px 0 0;min-height:18px"></p>
     </div>`;
  const btn = document.getElementById("kt-save");
  const stat = document.getElementById("kt-status");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const betrag = parseFloat(document.getElementById("kt-betrag").value);
    const datum = document.getElementById("kt-datum").value;
    if (!(betrag > 0)) { stat.textContent = "Bitte den Betrag eintragen."; return; }
    if (!datum) { stat.textContent = "Bitte das Belegdatum eintragen."; return; }
    btn.disabled = true; stat.textContent = "Übernehme …";
    const res = await api(`/app/api/belege/${encodeURIComponent(belegId)}/kontieren`, {
      method: "POST",
      body: JSON.stringify({
        haendler: document.getElementById("kt-haendler").value.trim(),
        datum,
        betrag_brutto_eur: betrag,
        mwst_prozent: parseInt(document.getElementById("kt-mwst").value, 10),
        kategorie: document.getElementById("kt-kategorie").value,
      }),
    });
    const r = res ? await res.json().catch(() => null) : null;
    if (r && r.ok) {
      box.innerHTML = `<div class="card"><h2>✓ Beleg gebucht</h2>
        <p class="muted" style="margin:6px 0 0">Händler, Datum, Betrag und Kategorie stehen in Lexware.</p></div>`;
    } else {
      btn.disabled = false;
      stat.textContent = (r && r.error) || "Konnte nicht übernehmen.";
    }
  });
}

// Ausgaben (Eingangsrechnungen aus Lexware) in ihre Karte nachladen.
// Fehlschlag ist kein Drama: dann steht dort eine Zeile statt einer Liste,
// der Rest des Bereichs bleibt benutzbar.
async function ladeAusgaben() {
  const box = document.getElementById("bu-ausgaben");
  if (!box) return;
  const res = await api("/app/api/buchhaltung/ausgaben");
  const j = res && res.ok ? await res.json().catch(() => null) : null;
  if (!j || !j.ok) {
    box.innerHTML = `<h2>Ausgaben</h2>` +
      emptyRow((j && j.error) || "Ausgaben gerade nicht abrufbar.");
    return;
  }
  const k = j.kennzahlen || {};
  const posten = j.posten || [];
  const kopf =
    `<div class="row"><div><div><b>Letzte 30 Tage</b></div>` +
    `<div class="sub">${k.ausgaben_30t_anzahl || 0} Eingangsrechnung(en)</div></div>` +
    `<b>${esc(fmtEur(k.ausgaben_30t_eur))}</b></div>` +
    ((k.offen_anzahl || 0)
      ? `<div class="row"><div><div>Davon noch nicht bezahlt</div>` +
        `<div class="sub">${k.offen_anzahl} offen</div></div>` +
        `<span class="pill warn">${esc(fmtEur(k.offen_eur))}</span></div>`
      : "");
  const liste = posten.slice(0, 6).map((p) =>
    rowPillLink(p.lieferant + (p.nummer ? " · " + p.nummer : ""),
                `${fmtEur(p.betrag_eur)} · vor ${p.tage} Tagen`,
                p.offen ? "offen" : "bezahlt", p.offen ? "warn" : "ok",
                p.lexware_link)).join("");
  box.innerHTML = `<h2>Ausgaben</h2>` + kopf +
    (liste || emptyRow("Keine Eingangsrechnungen in Lexware"));
}

// Karte mit Ueberschrift, den ersten `max` Zeilen und optional einem
// "Alle anzeigen"-Knopf, der auf den Vollbild-Screen fuehrt.
function abschnitt(titel, items, max, alleId, rowFn, leerText) {
  const sichtbar = items.slice(0, max);
  const rest = items.length - sichtbar.length;
  const alle = (rest > 0 && alleId)
    ? `<button class="btn-sm btn-ghost" id="${esc(alleId)}" style="width:100%;margin-top:8px">Alle ${items.length} anzeigen</button>`
    : "";
  return `<div class="card"><h2>${esc(titel)}</h2>` +
    (sichtbar.length ? sichtbar.map(rowFn).join("") : emptyRow(leerText)) +
    alle + `</div>`;
}
function rowTap(a, b, c, id) {
  return `<button class="row menu-item" data-aufnahme="${esc(id)}" style="align-items:flex-start">` +
    `<div style="text-align:left"><div>${esc(a)}</div>${b ? `<div class="sub">${esc(b)}</div>` : ""}</div>` +
    `<span class="sub">${esc(c)} ›</span></button>`;
}
function emptyRow(txt) { return `<div class="empty">${esc(txt)}</div>`; }
// Echter Fehlerzustand — klar unterscheidbar von "nichts zu tun". Wichtig,
// weil ein verschluckter Server-/Netzfehler sonst wie eine leere Liste
// aussieht ("Keine offenen Rueckrufe") und der Nutzer echte Arbeit uebersieht.
// Retry rendert den aktuellen Screen neu (App.current haelt den Schluessel).
function errorScreen(txt) {
  return `<div class="card" style="text-align:center">
    <p class="empty">${esc(txt || "Konnte gerade nicht laden.")}</p>
    <button class="btn" onclick="navigate(App.current)" style="margin-top:8px">Erneut versuchen</button>
  </div>`;
}

// Kurzer, nicht-blockierender Hinweis unten am Bildschirm — ersetzt native
// alert()-Dialoge fuer Erfolgs-/Info-Meldungen. In der installierten iOS-PWA
// erscheinen alert()s ohne App-Namen und wirken wie Systemfehler; ein Toast
// unterbricht ausserdem nicht den Ablauf. Fehler bleiben bei alert()/confirm().
let _toastTimer = null;
function toast(msg, kind) {
  let host = document.getElementById("app-toast");
  if (!host) {
    host = document.createElement("div");
    host.id = "app-toast";
    host.style.cssText =
      "position:fixed;left:50%;bottom:calc(72px + env(safe-area-inset-bottom,0px));" +
      "transform:translateX(-50%);z-index:9999;max-width:88%;padding:12px 18px;" +
      "border-radius:12px;font-size:15px;font-weight:600;color:#fff;text-align:center;" +
      "box-shadow:0 6px 24px rgba(0,0,0,.25);opacity:0;transition:opacity .18s;pointer-events:none";
    document.body.appendChild(host);
  }
  host.style.background = kind === "err" ? "#c0392b" : "#1e8e4e";
  host.textContent = msg;
  requestAnimationFrame(() => { host.style.opacity = "1"; });
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { host.style.opacity = "0"; }, 2600);
}

// =================== Angebot / Rechnung Composer ===================
//
// Beide Composer teilen die gleiche Positionen-UI + KI-Extract-Optik.
// Rechnung hat zusaetzlich einen Pauschal-Modus (1 Titel + Brutto-Betrag),
// weil das im Handwerker-Alltag dominiert.

let _composerPositionen = [];
let _composerMode = "angebot"; // "angebot" | "rechnung"
let _rechnungInputMode = "pauschal"; // pauschal | positionen

// min-width:0 an den drei Feldern der Mengenzeile: Flex-Kinder schrumpfen
// sonst nie unter ihre Eigenbreite, und drei nebeneinanderliegende <input>
// sind zusammen breiter als ein Handy-Display — die ganze Seite liess sich
// dadurch waagerecht schieben (auf 390 px: 657 px Inhalt).
function _composerPositionRow(p, idx) {
  return `<div class="card" style="padding:12px;margin-bottom:8px" data-pos="${idx}">
    <div class="row" style="align-items:flex-start">
      <div style="flex:1;min-width:0">
        <input type="text" data-fld="name" value="${esc(p.name || "")}" placeholder="Position-Name (z.B. Parkett verlegen)"
          style="width:100%;padding:8px;border:1px solid var(--line);border-radius:8px;margin-bottom:6px;font-size:15px" />
        <input type="text" data-fld="beschreibung" value="${esc(p.beschreibung || "")}" placeholder="Beschreibung (optional)"
          style="width:100%;padding:8px;border:1px solid var(--line);border-radius:8px;margin-bottom:6px;font-size:14px" />
        <div style="display:flex;gap:6px">
          <input type="number" data-fld="menge" value="${esc(p.menge || 1)}" step="0.01" min="0.01"
            style="flex:1;min-width:0;padding:8px;border:1px solid var(--line);border-radius:8px;font-size:14px" placeholder="Menge" />
          <input type="text" data-fld="einheit" value="${esc(p.einheit || 'Stueck')}"
            style="flex:1;min-width:0;padding:8px;border:1px solid var(--line);border-radius:8px;font-size:14px" placeholder="Einheit" />
          <input type="number" data-fld="preis_brutto_eur" value="${esc(p.preis_brutto_eur || '')}" step="0.01" min="0"
            style="flex:1.2;min-width:0;padding:8px;border:1px solid var(--line);border-radius:8px;font-size:14px" placeholder="EUR brutto" />
        </div>
      </div>
      <button class="btn-sm btn-ghost" data-del-pos="${idx}" style="padding:4px 8px;margin-left:6px" title="Entfernen">✕</button>
    </div>
  </div>`;
}

function _renderPositionen() {
  const wrap = document.getElementById("pos-list");
  if (!wrap) return;
  wrap.innerHTML = _composerPositionen.map((p, i) => _composerPositionRow(p, i)).join("");
  // Klick-Handler für Delete
  wrap.querySelectorAll("[data-del-pos]").forEach((b) =>
    b.addEventListener("click", () => {
      _composerPositionen.splice(parseInt(b.dataset.delPos, 10), 1);
      _renderPositionen();
      _updateSumme();
    }));
  // Input-Sync zurück in _composerPositionen
  wrap.querySelectorAll("[data-pos]").forEach((card) => {
    const idx = parseInt(card.dataset.pos, 10);
    card.querySelectorAll("[data-fld]").forEach((inp) =>
      inp.addEventListener("input", () => {
        const k = inp.dataset.fld;
        let v = inp.value;
        if (k === "menge" || k === "preis_brutto_eur") v = parseFloat(v) || 0;
        _composerPositionen[idx][k] = v;
        _updateSumme();
      }));
  });
  _updateSumme();
}

function _updateSumme() {
  const el = document.getElementById("pos-summe");
  if (!el) return;
  const summe = _composerPositionen.reduce((s, p) =>
    s + (parseFloat(p.menge) || 0) * (parseFloat(p.preis_brutto_eur) || 0), 0);
  el.textContent = summe.toLocaleString("de-DE", { style: "currency", currency: "EUR" });
}

function _composerKundenFields() {
  return `<div class="card"><h2>Kunde</h2>
    <label class="sub">Name *</label>
    <input type="text" id="c-kunde-name" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
    <label class="sub">E-Mail (für PDF-Versand)</label>
    <input type="email" id="c-kunde-mail" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
    <label class="sub">Straße + Nr.</label>
    <input type="text" id="c-kunde-str" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
    <div style="display:flex;gap:8px">
      <input type="text" id="c-kunde-plz" placeholder="PLZ" style="flex:0 0 30%;min-width:0;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
      <input type="text" id="c-kunde-ort" placeholder="Ort" style="flex:1;min-width:0;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
    </div>
  </div>`;
}

function _kiExtractCard(prefilledLabel) {
  return `<div class="card"><h2>KI-Hilfe (optional)</h2>
    <p class="muted" style="margin-top:0;font-size:13px">Tippe oder diktiere frei — die KI extrahiert ${esc(prefilledLabel)} und füllt die Felder unten vor.</p>
    <textarea id="ki-text" rows="3" placeholder="z.B. «Müller Bad Schwalbach Heizung reparieren 350 Euro»"
      style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;font-family:inherit;font-size:16px"></textarea>
    <button class="btn-sm btn-ghost" id="ki-extract" style="margin-top:8px">KI ausfüllen lassen</button>
  </div>`;
}

function _bindKiExtract(endpoint, applyFn) {
  const btn = document.getElementById("ki-extract");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const text = document.getElementById("ki-text").value.trim();
    if (text.length < 5) { alert("Bitte mehr Text eingeben."); return; }
    btn.disabled = true; btn.textContent = "KI denkt nach …";
    const r = await api(endpoint, { method: "POST", body: JSON.stringify({ text }) });
    if (r && r.ok) {
      const j = await r.json();
      if (j.ok && j.extracted) applyFn(j.extracted);
      else alert("KI: " + (j.error || "keine Daten"));
    } else {
      alert("KI-Aufruf fehlgeschlagen.");
    }
    btn.disabled = false; btn.textContent = "KI ausfüllen lassen";
  });
}

function _applyExtractedToKunde(ex) {
  if (ex.kunde_name) document.getElementById("c-kunde-name").value = ex.kunde_name;
  if (ex.kunde_email) document.getElementById("c-kunde-mail").value = ex.kunde_email;
  if (ex.kunde_strasse) document.getElementById("c-kunde-str").value = ex.kunde_strasse;
  if (ex.kunde_plz) document.getElementById("c-kunde-plz").value = ex.kunde_plz;
  if (ex.kunde_ort) document.getElementById("c-kunde-ort").value = ex.kunde_ort;
}

function showAngebotForm() {
  _composerMode = "angebot";
  _composerPositionen = [];
  App.view.innerHTML =
    `<button class="btn-sm btn-ghost" id="back-buero" style="margin-bottom:10px">← Zurück</button>` +
    `<h1 style="font-size:22px;margin:4px 4px 14px">Neues Angebot</h1>` +
    _kiExtractCard("Kunde + Positionen mit Preisen") +
    _composerKundenFields() +
    `<div class="card"><h2>Positionen</h2>
       <div id="pos-list"></div>
       <button class="btn-sm btn-ghost" id="pos-add" style="margin-top:6px;width:100%">+ Position</button>
       <div class="row" style="margin-top:12px;padding-top:10px;border-top:1px solid var(--line)">
         <b>Gesamt brutto</b><b id="pos-summe">0,00 €</b>
       </div>
     </div>` +
    `<div class="card"><h2>Texte (optional)</h2>
       <label class="sub">Anschreiben</label>
       <textarea id="c-intro" rows="3" placeholder="z.B. Sehr geehrte Frau Müller, vielen Dank für Ihre Anfrage …"
         style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;font-family:inherit;font-size:16px"></textarea>
       <label class="sub" style="margin-top:8px;display:block">Schluss-Bemerkung</label>
       <textarea id="c-remark" rows="2" placeholder="z.B. Wir freuen uns auf Ihren Auftrag!"
         style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;font-family:inherit;font-size:16px"></textarea>
     </div>` +
    `<button class="btn-sm" id="c-save" style="width:100%;margin-top:8px">Angebot anlegen</button>`;

  document.getElementById("back-buero").addEventListener("click", () => navigate(App.lastScreen || "buchhaltung"));
  App.lastScreen = null;
  _renderPositionen();
  document.getElementById("pos-add").addEventListener("click", () => {
    _composerPositionen.push({ name: "", menge: 1, einheit: "Stueck", preis_brutto_eur: 0 });
    _renderPositionen();
  });

  _bindKiExtract("/app/api/angebote/extrahieren", (ex) => {
    _applyExtractedToKunde(ex);
    if (Array.isArray(ex.positionen)) {
      _composerPositionen = ex.positionen.map((p) => ({
        name: p.name || "", beschreibung: p.beschreibung || "",
        menge: p.menge || 1, einheit: p.einheit || "Stueck",
        preis_brutto_eur: p.preis_brutto_eur || 0,
        mwst_prozent: p.mwst_prozent || 19,
      }));
      _renderPositionen();
    }
  });

  document.getElementById("c-save").addEventListener("click", _submitAngebot);
}

async function _submitAngebot() {
  const body = {
    kunde_name: document.getElementById("c-kunde-name").value.trim(),
    kunde_email: document.getElementById("c-kunde-mail").value.trim() || null,
    kunde_strasse: document.getElementById("c-kunde-str").value.trim() || null,
    kunde_plz: document.getElementById("c-kunde-plz").value.trim() || null,
    kunde_ort: document.getElementById("c-kunde-ort").value.trim() || null,
    intro_text: document.getElementById("c-intro").value.trim() || null,
    remark_text: document.getElementById("c-remark").value.trim() || null,
    positionen: _composerPositionen,
  };
  if (!body.kunde_name) { alert("Kundenname ist Pflicht."); return; }
  if (!body.positionen.length) { alert("Mindestens 1 Position hinzufügen."); return; }
  const btn = document.getElementById("c-save");
  btn.disabled = true; btn.textContent = "Lege an + Lexware …";
  const r = await api("/app/api/angebote/anlegen", { method: "POST", body: JSON.stringify(body) });
  if (r && r.ok) {
    const j = await r.json();
    if (j.ok) { _showAccountingResult("Angebot", j, "angebote"); return; }
    alert("Konnte nicht anlegen: " + (j.error || "unbekannt"));
  } else {
    alert("Konnte nicht anlegen.");
  }
  btn.disabled = false; btn.textContent = "Angebot anlegen";
}

async function _submitAuftragNeu() {
  // Leere Positions-Zeilen (die Vorlage beim Öffnen, ein versehentliches
  // „+ Position") fliegen raus, statt den Nutzer zu maßregeln.
  const positionen = _composerPositionen.filter((p) => (p.name || "").trim());
  const body = {
    kunde_name: document.getElementById("c-kunde-name").value.trim(),
    kunde_email: document.getElementById("c-kunde-mail").value.trim() || null,
    kunde_strasse: document.getElementById("c-kunde-str").value.trim() || null,
    kunde_plz: document.getElementById("c-kunde-plz").value.trim() || null,
    kunde_ort: document.getElementById("c-kunde-ort").value.trim() || null,
    status: document.getElementById("an-status").value,
    positionen,
  };
  if (!body.kunde_name) { alert("Kundenname ist Pflicht."); return; }
  if (!positionen.length) { alert("Mindestens 1 Position mit Bezeichnung."); return; }
  const btn = document.getElementById("an-save");
  btn.disabled = true; btn.textContent = "Lege an …";
  const r = await api("/app/api/auftraege/neu", { method: "POST", body: JSON.stringify(body) });
  if (r) {
    let j = null;
    try { j = await r.json(); } catch (e) {}
    if (j && j.ok) { showAuftragDetail(j.id, "auftraege_page"); return; }
    alert((j && j.error) || "Konnte den Auftrag nicht anlegen.");
  } else {
    alert("Konnte den Auftrag nicht anlegen.");
  }
  btn.disabled = false; btn.textContent = "Auftrag anlegen";
}

function showRechnungForm() {
  _composerMode = "rechnung";
  _composerPositionen = [];
  _rechnungInputMode = "pauschal";
  App.view.innerHTML =
    `<button class="btn-sm btn-ghost" id="back-buero" style="margin-bottom:10px">← Zurück</button>` +
    `<h1 style="font-size:22px;margin:4px 4px 14px">Neue Rechnung</h1>` +
    _kiExtractCard("Kunde + Leistung + Betrag") +
    _composerKundenFields() +
    `<div class="card">
       <h2>Leistung</h2>
       <div style="display:flex;gap:6px;margin-bottom:10px">
         <button class="btn-sm" data-rmode="pauschal" id="rmode-pauschal" style="flex:1">Pauschal</button>
         <button class="btn-sm btn-ghost" data-rmode="positionen" id="rmode-pos" style="flex:1">Positionen</button>
       </div>
       <div id="rmode-pauschal-body">
         <label class="sub">Leistungs-Titel *</label>
         <input type="text" id="r-titel" placeholder="z.B. Heizungsreparatur" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
         <label class="sub">Beschreibung (optional)</label>
         <textarea id="r-besch" rows="2" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;font-family:inherit;font-size:16px"></textarea>
         <label class="sub" style="margin-top:8px;display:block">Brutto-Betrag (EUR) *</label>
         <input type="number" id="r-betrag" step="0.01" min="0" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 0;font-size:16px" />
       </div>
       <div id="rmode-pos-body" style="display:none">
         <div id="pos-list"></div>
         <button class="btn-sm btn-ghost" id="pos-add" style="margin-top:6px;width:100%">+ Position</button>
         <div class="row" style="margin-top:12px;padding-top:10px;border-top:1px solid var(--line)">
           <b>Gesamt brutto</b><b id="pos-summe">0,00 €</b>
         </div>
       </div>
     </div>` +
    `<button class="btn-sm" id="c-save" style="width:100%;margin-top:8px">Rechnung anlegen</button>`;

  document.getElementById("back-buero").addEventListener("click", () => navigate(App.lastScreen || "buchhaltung"));
  App.lastScreen = null;

  document.querySelectorAll("[data-rmode]").forEach((b) =>
    b.addEventListener("click", () => {
      _rechnungInputMode = b.dataset.rmode;
      document.getElementById("rmode-pauschal-body").style.display =
        _rechnungInputMode === "pauschal" ? "" : "none";
      document.getElementById("rmode-pos-body").style.display =
        _rechnungInputMode === "positionen" ? "" : "none";
      document.getElementById("rmode-pauschal").className =
        "btn-sm " + (_rechnungInputMode === "pauschal" ? "" : "btn-ghost");
      document.getElementById("rmode-pos").className =
        "btn-sm " + (_rechnungInputMode === "positionen" ? "" : "btn-ghost");
      if (_rechnungInputMode === "positionen") _renderPositionen();
    }));

  const addBtn = document.getElementById("pos-add");
  if (addBtn) addBtn.addEventListener("click", () => {
    _composerPositionen.push({ name: "", menge: 1, einheit: "Stueck", preis_brutto_eur: 0 });
    _renderPositionen();
  });

  _bindKiExtract("/app/api/rechnungen/extrahieren", (ex) => {
    _applyExtractedToKunde(ex);
    if (ex.leistung_titel) document.getElementById("r-titel").value = ex.leistung_titel;
    if (ex.leistung_beschreibung) document.getElementById("r-besch").value = ex.leistung_beschreibung;
    if (ex.betrag_brutto_eur) document.getElementById("r-betrag").value = ex.betrag_brutto_eur;
  });

  document.getElementById("c-save").addEventListener("click", _submitRechnung);
}

async function _submitRechnung() {
  const body = {
    kunde_name: document.getElementById("c-kunde-name").value.trim(),
    kunde_email: document.getElementById("c-kunde-mail").value.trim() || null,
    kunde_strasse: document.getElementById("c-kunde-str").value.trim() || null,
    kunde_plz: document.getElementById("c-kunde-plz").value.trim() || null,
    kunde_ort: document.getElementById("c-kunde-ort").value.trim() || null,
  };
  if (_rechnungInputMode === "pauschal") {
    body.leistung_titel = document.getElementById("r-titel").value.trim();
    body.leistung_beschreibung = document.getElementById("r-besch").value.trim() || null;
    body.betrag_brutto_eur = parseFloat(document.getElementById("r-betrag").value || 0);
    if (!body.leistung_titel || !body.betrag_brutto_eur) {
      alert("Leistungs-Titel und Brutto-Betrag sind Pflicht."); return;
    }
  } else {
    body.positionen = _composerPositionen;
    if (!body.positionen.length) { alert("Mindestens 1 Position hinzufügen."); return; }
  }
  if (!body.kunde_name) { alert("Kundenname ist Pflicht."); return; }
  const btn = document.getElementById("c-save");
  btn.disabled = true; btn.textContent = "Lege an + Lexware …";
  const r = await api("/app/api/rechnungen/anlegen", { method: "POST", body: JSON.stringify(body) });
  if (r && r.ok) {
    const j = await r.json();
    if (j.ok) { _showAccountingResult("Rechnung", j, "rechnungen"); return; }
    alert("Konnte nicht anlegen: " + (j.error || "unbekannt"));
  } else {
    alert("Konnte nicht anlegen.");
  }
  btn.disabled = false; btn.textContent = "Rechnung anlegen";
}

function _showAccountingResult(typ, j, sendPath) {
  // Quittungs-Screen: zeigt Lexware-Status, Deeplink, Sende-Button
  const lex = j.lexware_voucher_number
    ? `<div class="row"><span>Lexware-Nummer</span><span class="sub">${esc(j.lexware_voucher_number)}</span></div>
       <a class="btn-sm btn-ghost" href="${esc(j.lexware_deeplink || '#')}" target="_blank" rel="noopener" style="text-decoration:none;margin-top:8px;display:inline-block">→ In Lexware öffnen</a>`
    : `<p class="muted">${esc(j.warning || 'Nicht in Lexware angelegt — bitte später nachreichen.')}</p>`;
  App.view.innerHTML =
    `<div class="card">
       <h2 style="margin-top:0">✓ ${esc(typ)} angelegt</h2>
       ${lex}
     </div>
     ${j.lexware_voucher_number ? `<div class="card">
       <h2>Per Mail an Kunden senden</h2>
       <p class="muted" style="font-size:13px;margin-top:0">Schickt das PDF aus Lexware an die hinterlegte Mail-Adresse.</p>
       <button class="btn-sm" id="send-pdf" style="width:100%">PDF jetzt senden</button>
     </div>` : ""}
     <button class="btn-sm btn-ghost" id="back-buero" style="width:100%;margin-top:8px">Zurück</button>`;
  const _backTarget = App.lastScreen || "buchhaltung";
  App.lastScreen = null;
  document.getElementById("back-buero").addEventListener("click", () => navigate(_backTarget));
  const sendBtn = document.getElementById("send-pdf");
  if (sendBtn) sendBtn.addEventListener("click", async () => {
    sendBtn.disabled = true; sendBtn.textContent = "Sende …";
    const r = await api(`/app/api/${sendPath}/${encodeURIComponent(j.id)}/senden`,
      { method: "POST", body: "{}" });
    if (r && r.ok) {
      const k = await r.json();
      if (k.ok) {
        alert("Mail erfolgreich gesendet.");
        navigate(_backTarget); return;
      }
      alert("Versand fehlgeschlagen: " + (k.error || "unbekannt"));
    } else {
      alert("Versand fehlgeschlagen.");
    }
    sendBtn.disabled = false; sendBtn.textContent = "PDF jetzt senden";
  });
}

async function showNewMaterialForm() {
  App.view.innerHTML =
    `<button class="btn-sm btn-ghost" id="back-material" style="margin-bottom:10px">← Zurück</button>` +
    `<h1 style="font-size:22px;margin:4px 4px 14px">Neues Material</h1>` +
    `<div class="card">
       <label class="sub">Name *</label>
       <input type="text" id="mat-name" placeholder="z.B. Kupferrohr 22 mm" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
       <label class="sub">Bestell-Link *</label>
       <input type="url" id="mat-link" placeholder="https://…" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
       <label class="sub">Lieferant (optional)</label>
       <input type="text" id="mat-lief" placeholder="z.B. Wilhelm Mauder" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
       <label class="sub">Einheit</label>
       <input type="text" id="mat-einheit" value="Stück" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
       <label class="sub">Standard-Menge</label>
       <input type="number" id="mat-menge" value="1" min="1" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
       <label class="sub">Notiz (optional)</label>
       <textarea id="mat-notes" rows="2" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;font-family:inherit;font-size:16px"></textarea>
       <button class="btn-sm" id="mat-save" style="margin-top:12px;width:100%">Material anlegen</button>
    </div>`;
  document.getElementById("back-material").addEventListener("click", () => navigate("material"));
  document.getElementById("mat-save").addEventListener("click", async () => {
    const name = document.getElementById("mat-name").value.trim();
    const link = document.getElementById("mat-link").value.trim();
    if (!name || !link) { alert("Name und Bestell-Link sind Pflicht."); return; }
    const body = {
      name, bestell_link: link,
      lieferant: document.getElementById("mat-lief").value.trim() || null,
      einheit: document.getElementById("mat-einheit").value.trim() || "Stück",
      standard_menge: parseInt(document.getElementById("mat-menge").value || "1", 10),
      notes: document.getElementById("mat-notes").value.trim() || null,
    };
    const btn = document.getElementById("mat-save");
    btn.disabled = true; btn.textContent = "Speichere …";
    const res = await api("/app/api/material/anlegen", { method: "POST", body: JSON.stringify(body) });
    if (res && res.ok) {
      const j = await res.json();
      if (j.ok) { navigate("material"); return; }
      alert("Konnte nicht anlegen: " + (j.error || "unbekannt"));
    } else {
      alert("Konnte nicht anlegen.");
    }
    btn.disabled = false; btn.textContent = "Material anlegen";
  });
}

function showFormularLinkModal(typ) {
  const inpStyle = "width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px";
  const html =
    `<div id="flink-modal" style="position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;z-index:1000;padding:16px">
       <div class="card" style="max-width:440px;width:100%;margin:0">
         <h2 style="margin-top:0">Kunden-Link generieren</h2>
         <div id="flink-form">
           <label class="sub">Kundenname *</label>
           <input type="text" id="flink-name" placeholder="z.B. Max Muster" style="${inpStyle}">
           <label class="sub">E-Mail des Kunden (optional)</label>
           <input type="email" id="flink-email" placeholder="kunde@beispiel.de" style="${inpStyle}">
           <label class="sub">Telefon (optional)</label>
           <input type="tel" id="flink-tel" placeholder="+49 …" style="${inpStyle}">
           <label class="sub">Gültig für</label>
           <select id="flink-days" style="${inpStyle}">
             <option value="3">3 Tage</option>
             <option value="7" selected>7 Tage</option>
             <option value="14">14 Tage</option>
             <option value="30">30 Tage</option>
           </select>
           <p id="flink-err" style="color:var(--err,#b42318);font-size:13px;margin:0 0 8px;display:none"></p>
           <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:4px">
             <button class="btn-sm btn-ghost" id="flink-cancel">Abbrechen</button>
             <button class="btn-sm" id="flink-save">Link erstellen</button>
           </div>
         </div>
         <div id="flink-result" style="display:none">
           <p class="sub" style="margin:0 0 6px">Link für <strong id="flink-res-name"></strong> (gültig bis <span id="flink-res-exp"></span>):</p>
           <div style="display:flex;gap:6px;align-items:center;margin-bottom:14px">
             <input type="text" id="flink-res-url" readonly style="flex:1;padding:9px 10px;border:1px solid var(--line);border-radius:10px;font-size:14px;background:var(--bg2,#f8f8f8)">
             <button class="btn-sm btn-ghost" id="flink-res-copy" style="white-space:nowrap">Kopieren</button>
           </div>
           <button class="btn-sm btn-ghost" id="flink-close" style="width:100%">Schließen</button>
         </div>
       </div>
     </div>`;
  document.body.insertAdjacentHTML("beforeend", html);
  const modal = () => document.getElementById("flink-modal");
  const close = () => modal()?.remove();
  document.getElementById("flink-cancel").addEventListener("click", close);
  document.getElementById("flink-save").addEventListener("click", async () => {
    const name = (document.getElementById("flink-name").value || "").trim();
    const email = (document.getElementById("flink-email").value || "").trim() || null;
    const tel = (document.getElementById("flink-tel").value || "").trim() || null;
    const days = parseInt(document.getElementById("flink-days").value, 10) || 7;
    const errEl = document.getElementById("flink-err");
    if (!name) { errEl.textContent = "Kundenname ist Pflicht."; errEl.style.display = ""; return; }
    errEl.style.display = "none";
    const btn = document.getElementById("flink-save");
    btn.disabled = true; btn.textContent = "Erstelle …";
    const res = await api(`/app/api/formulare/${encodeURIComponent(typ)}/link`,
      { method: "POST", body: JSON.stringify({ kunde_name: name, kunde_email: email, kunde_telefon: tel, valid_days: days }) });
    const j = res ? await res.json().catch(() => null) : null;
    btn.disabled = false; btn.textContent = "Link erstellen";
    if (!j || !j.ok) {
      errEl.textContent = (j && j.error) || "Konnte Link nicht erstellen.";
      errEl.style.display = "";
      return;
    }
    document.getElementById("flink-form").style.display = "none";
    const result = document.getElementById("flink-result");
    result.style.display = "";
    document.getElementById("flink-res-name").textContent = j.kunde_name || name;
    document.getElementById("flink-res-exp").textContent = j.expires_fmt || "";
    document.getElementById("flink-res-url").value = j.url || "";
    document.getElementById("flink-res-copy").addEventListener("click", () => {
      navigator.clipboard.writeText(j.url || "").then(() => {
        const b = document.getElementById("flink-res-copy");
        if (b) { b.textContent = "Kopiert!"; setTimeout(() => { b.textContent = "Kopieren"; }, 2000); }
      });
    });
    document.getElementById("flink-close").addEventListener("click", close);
  });
}

function showAbsenceDialog(slug, name, typ) {
  // Mini-Modal als Overlay — vermeidet Navigation away aus dem Team-Screen.
  const todayIso = new Date().toISOString().slice(0, 10);
  const typLabel = typ === "krank" ? "Krankmelden" : (typ === "urlaub" ? "Urlaub eintragen" : "Abwesenheit");
  const html =
    `<div id="abs-modal" style="position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;z-index:1000;padding:16px">
       <div class="card" style="max-width:420px;width:100%;margin:0">
         <h2 style="margin-top:0">${esc(typLabel)} — ${esc(name)}</h2>
         <label class="sub">Start</label>
         <input type="date" id="abs-start" value="${todayIso}" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
         <label class="sub">Ende (leer = unbestimmt)</label>
         <input type="date" id="abs-ende" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
         <label class="sub">Notiz (optional)</label>
         <input type="text" id="abs-notes" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
         <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:10px">
           <button class="btn-sm btn-ghost" id="abs-cancel">Abbrechen</button>
           <button class="btn-sm" id="abs-save">Speichern</button>
         </div>
       </div>
     </div>`;
  document.body.insertAdjacentHTML("beforeend", html);
  const close = () => document.getElementById("abs-modal")?.remove();
  document.getElementById("abs-cancel").addEventListener("click", close);
  document.getElementById("abs-save").addEventListener("click", async () => {
    const start = document.getElementById("abs-start").value;
    const ende = document.getElementById("abs-ende").value || null;
    const notes = document.getElementById("abs-notes").value.trim() || null;
    if (!start) { alert("Start-Datum fehlt."); return; }
    const btn = document.getElementById("abs-save");
    btn.disabled = true; btn.textContent = "Speichere …";
    const res = await api(`/app/api/team/${encodeURIComponent(slug)}/abwesenheit`,
      { method: "POST", body: JSON.stringify({ typ, start, ende, notes }) });
    if (res && res.ok) {
      const j = await res.json();
      if (j.ok) { close(); navigate("team"); return; }
      alert("Konnte nicht speichern: " + (j.error || "unbekannt"));
    } else {
      alert("Konnte nicht speichern.");
    }
    btn.disabled = false; btn.textContent = "Speichern";
  });
}

async function showNewEmployeeForm() {
  App.view.innerHTML =
    `<button class="btn-sm btn-ghost" id="back-team" style="margin-bottom:10px">← Zurück</button>` +
    `<h1 style="font-size:22px;margin:4px 4px 14px">Mitarbeiter anlegen</h1>` +
    `<div class="card">
       <label class="sub">Name *</label>
       <input type="text" id="emp-name" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
       <label class="sub">Job-Titel (optional)</label>
       <input type="text" id="emp-job" placeholder="z.B. Geselle, Auszubildender" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
       <label class="sub">E-Mail (optional, für App-Login)</label>
       <input type="email" id="emp-mail" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
       <label class="sub">Skills (komma-getrennt, optional)</label>
       <input type="text" id="emp-skills" placeholder="z.B. Heizung, Sanitär, Elektro" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
       <label class="sub" style="display:block;margin-top:6px">Rolle</label>
       <div id="emp-rolle-wahl" style="display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 4px">
         <button type="button" class="btn-sm btn-ghost" data-rolle="buero" style="padding:8px 14px">Büro</button>
         <button type="button" class="btn-sm" data-rolle="monteur" style="padding:8px 14px">Monteur</button>
       </div>
       <p class="muted" id="emp-rolle-hint" style="margin:0 0 10px;font-size:12px"></p>
       <button class="btn-sm" id="emp-save" style="margin-top:12px;width:100%">Mitarbeiter anlegen</button>
       <p class="muted" style="margin-top:8px;font-size:12px">Nach dem Anlegen bekommst du einen einmaligen Aktivierungs-Link zum Weitergeben. Einzelne Rechte kannst du danach über „Rechte" anpassen.</p>
    </div>`;
  document.getElementById("back-team").addEventListener("click", () => navigate("team"));

  // Rollen-Auswahl. Default ist die restriktivste Rolle — niemand startet
  // versehentlich mit Zugriff auf die Buchhaltung.
  const ROLLE_HINT = {
    buero: "Sieht Aufträge, Kunden und Anfragen — nicht die Buchhaltung.",
    monteur: "Sieht nur die eigenen Aufträge, Termine und Material.",
  };
  let gewaehlteRolle = "monteur";
  const hint = document.getElementById("emp-rolle-hint");
  const malen = () => {
    document.querySelectorAll("#emp-rolle-wahl [data-rolle]").forEach((b) => {
      b.className = b.dataset.rolle === gewaehlteRolle ? "btn-sm" : "btn-sm btn-ghost";
    });
    hint.textContent = ROLLE_HINT[gewaehlteRolle] || "";
  };
  document.querySelectorAll("#emp-rolle-wahl [data-rolle]").forEach((b) =>
    b.addEventListener("click", () => { gewaehlteRolle = b.dataset.rolle; malen(); }));
  malen();
  document.getElementById("emp-save").addEventListener("click", async () => {
    const name = document.getElementById("emp-name").value.trim();
    if (!name) { alert("Name ist Pflicht."); return; }
    const body = {
      name,
      job_title: document.getElementById("emp-job").value.trim() || null,
      contact_email: document.getElementById("emp-mail").value.trim() || null,
      skills: document.getElementById("emp-skills").value.trim() || null,
      rolle: gewaehlteRolle,
    };
    const btn = document.getElementById("emp-save");
    btn.disabled = true; btn.textContent = "Speichere …";
    const res = await api("/app/api/team/anlegen",
      { method: "POST", body: JSON.stringify(body) });
    if (res && res.ok) {
      const j = await res.json();
      if (j.ok) {
        showEmployeeActivationLink(j, name);
        return;
      }
      alert("Konnte nicht anlegen: " + (j.error || "unbekannt"));
    } else {
      alert("Konnte nicht anlegen.");
    }
    btn.disabled = false; btn.textContent = "Mitarbeiter anlegen";
  });
}

function showEmployeeActivationLink(j, name) {
  // Eigene Erfolgs-Seite mit dem Aktivierungs-Link prominent als Quasi-
  // Quittung. Inhaber kopiert + schickt den Link via WhatsApp / SMS.
  const expires = j.expires_at ? new Date(j.expires_at) : null;
  const expiresFmt = expires ? expires.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit", year: "numeric" }) : "";
  App.view.innerHTML =
    `<div class="card">
       <h2 style="margin-top:0">✓ ${esc(name)} angelegt</h2>
       <p>Schicke ${esc(name)} diesen einmaligen Aktivierungs-Link — der Account ist erst aktiv, sobald der Mitarbeiter ihn geöffnet und ein Passwort gesetzt hat.</p>
       <label class="sub">Aktivierungs-Link</label>
       <input type="text" id="act-url" readonly value="${esc(j.activation_url)}" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 8px;font-size:13px;font-family:monospace" />
       <div style="display:flex;gap:8px;flex-wrap:wrap">
         <button class="btn-sm" id="copy-url">Link kopieren</button>
         <a class="btn-sm btn-ghost" href="https://wa.me/?text=${encodeURIComponent('Hier ist dein Aktivierungs-Link für die Gewerbeagent-App: ' + j.activation_url)}" target="_blank" rel="noopener">WhatsApp</a>
         <a class="btn-sm btn-ghost" href="sms:?body=${encodeURIComponent('Aktivierungs-Link: ' + j.activation_url)}">SMS</a>
       </div>
       ${j.activation_short_code ? `<p class="muted" style="margin-top:10px">Kurzcode als Alternative: <b>${esc(j.activation_short_code)}</b></p>` : ""}
       ${expiresFmt ? `<p class="muted">Gültig bis ${esc(expiresFmt)}.</p>` : ""}
       <button class="btn-sm btn-ghost" id="back-team-2" style="margin-top:12px;width:100%">Zurück zum Team</button>
    </div>`;
  document.getElementById("copy-url").addEventListener("click", () => {
    const inp = document.getElementById("act-url");
    inp.select(); inp.setSelectionRange(0, 99999);
    navigator.clipboard.writeText(inp.value).catch(() => { document.execCommand("copy"); });
    document.getElementById("copy-url").textContent = "Kopiert!";
  });
  document.getElementById("back-team-2").addEventListener("click", () => navigate("team"));
}

// ---------- Sprach-Diktat (Browser-Aufnahme → WAV → Gemini) ----------
// Wir nehmen per Web-Audio auf und kodieren CLIENT-SEITIG zu WAV 16 kHz
// mono. Grund: Gemini akzeptiert wav/ogg/mp3/flac/aac nativ, aber NICHT das
// webm/opus, das Chrome-MediaRecorder per Default liefert. WAV vermeidet
// jede Server-Konvertierung (kein ffmpeg im Stack) und laeuft auf Chrome
// (Android) wie iOS-Safari.
const DIKTAT_TARGET_RATE = 16000;
const DIKTAT_MAX_SECONDS = 15 * 60; // Auto-Stopp; 15 min WAV ≈ 28 MB < 50 MB

// Beschriftungen fuer den Sprech-Knopf bei Q. Ein Tipp startet die Aufnahme
// und laesst sie laufen, ein zweiter Tipp sendet sie.
const SPRECH_TITEL = "Tippen zum Sprechen";
const SPRECH_HINT_REC = "🔴 Aufnahme läuft — tippen zum Senden";

const Diktat = {
  ctx: null, source: null, node: null, zero: null, stream: null,
  analyser: null, levelBuf: null,
  chunks: [], length: 0, inRate: DIKTAT_TARGET_RATE,
  recording: false, startTs: 0, tick: null, autostop: null,
};

function _diktatFlatten() {
  const out = new Float32Array(Diktat.length);
  let o = 0;
  for (const c of Diktat.chunks) { out.set(c, o); o += c.length; }
  return out;
}

function _diktatResample(input, inRate, outRate) {
  if (inRate === outRate) return input;
  const ratio = inRate / outRate;
  const outLen = Math.floor(input.length / ratio);
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const idx = i * ratio;
    const lo = Math.floor(idx);
    const hi = Math.min(lo + 1, input.length - 1);
    out[i] = input[lo] + (input[hi] - input[lo]) * (idx - lo);
  }
  return out;
}

function _diktatEncodeWav(samples, rate) {
  const buf = new ArrayBuffer(44 + samples.length * 2);
  const v = new DataView(buf);
  const ws = (off, s) => { for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)); };
  ws(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); ws(8, "WAVE");
  ws(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true);
  v.setUint16(22, 1, true); v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  ws(36, "data"); v.setUint32(40, samples.length * 2, true);
  let off = 44;
  for (let i = 0; i < samples.length; i++, off += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}

async function _diktatStartRecording() {
  Diktat.stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });
  const Ctx = window.AudioContext || window.webkitAudioContext;
  Diktat.ctx = new Ctx();
  if (Diktat.ctx.state === "suspended") await Diktat.ctx.resume();
  Diktat.inRate = Diktat.ctx.sampleRate;
  Diktat.source = Diktat.ctx.createMediaStreamSource(Diktat.stream);
  Diktat.node = Diktat.ctx.createScriptProcessor(4096, 1, 1);
  Diktat.chunks = []; Diktat.length = 0;
  Diktat.node.onaudioprocess = (e) => {
    if (!Diktat.recording) return;
    const d = e.inputBuffer.getChannelData(0);
    Diktat.chunks.push(new Float32Array(d));
    Diktat.length += d.length;
  };
  // ScriptProcessor feuert in Chrome nur, wenn er bis zur destination
  // verdrahtet ist — ueber eine Gain=0-Node, damit nichts hoerbar
  // zurueckgespielt wird (sonst Rueckkopplung).
  Diktat.zero = Diktat.ctx.createGain();
  Diktat.zero.gain.value = 0;
  // Zweiter Abgriff nur fuer die Pegel-Anzeige (Wellenform in der Aufnahme-
  // Leiste). Haengt parallel am Source, beeinflusst die Aufnahme nicht.
  Diktat.analyser = Diktat.ctx.createAnalyser();
  Diktat.analyser.fftSize = 1024;
  Diktat.analyser.smoothingTimeConstant = 0.5;
  Diktat.levelBuf = new Uint8Array(Diktat.analyser.fftSize);
  Diktat.source.connect(Diktat.analyser);
  Diktat.source.connect(Diktat.node);
  Diktat.node.connect(Diktat.zero);
  Diktat.zero.connect(Diktat.ctx.destination);
  Diktat.recording = true;
  Diktat.startTs = Date.now();
}

// Momentaner Eingangspegel 0..1 (RMS) — Futter fuer die Wellenform.
function _diktatLevel() {
  if (!Diktat.analyser || !Diktat.levelBuf) return 0;
  try { Diktat.analyser.getByteTimeDomainData(Diktat.levelBuf); } catch (e) { return 0; }
  let sum = 0;
  for (let i = 0; i < Diktat.levelBuf.length; i++) {
    const v = (Diktat.levelBuf[i] - 128) / 128;
    sum += v * v;
  }
  return Math.sqrt(sum / Diktat.levelBuf.length);
}

function _diktatTeardown() {
  Diktat.recording = false;
  try { if (Diktat.node) { Diktat.node.disconnect(); Diktat.node.onaudioprocess = null; } } catch (e) {}
  try { if (Diktat.analyser) Diktat.analyser.disconnect(); } catch (e) {}
  Diktat.analyser = null; Diktat.levelBuf = null;
  try { if (Diktat.zero) Diktat.zero.disconnect(); } catch (e) {}
  try { if (Diktat.source) Diktat.source.disconnect(); } catch (e) {}
  try { if (Diktat.stream) Diktat.stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
  try { if (Diktat.ctx && Diktat.ctx.state !== "closed") Diktat.ctx.close(); } catch (e) {}
  if (Diktat.tick) { clearInterval(Diktat.tick); Diktat.tick = null; }
  if (Diktat.autostop) { clearTimeout(Diktat.autostop); Diktat.autostop = null; }
}

function _diktatFinish() {
  const durationSec = Math.round((Date.now() - Diktat.startTs) / 1000);
  const raw = _diktatFlatten();
  const resampled = _diktatResample(raw, Diktat.inRate, DIKTAT_TARGET_RATE);
  _diktatTeardown();
  return { blob: _diktatEncodeWav(resampled, DIKTAT_TARGET_RATE), durationSec };
}

// ---------- Sprechen: Aufnahme-Steuerung (Assistent + Q-Overlay) ----------
// Wie bei WhatsApp: ein Tipp startet die Aufnahme und laesst sie laufen, ein
// zweiter sendet sie. Wer den Knopf gedrueckt HAELT, sendet beim Loslassen.
// Beide Q-Oberflaechen benutzen dieselbe Steuerung — die Wellenform und die
// Halten-Erkennung gibt es deshalb nur einmal.
//
// opts: { bar, wave, time, onUi(on), onHint(text), onText(text), onError(msg) }
//   bar/wave/time  Elemente der Aufnahme-Leiste (duerfen fehlen)
//   onUi           schaltet die bildschirm-eigene Optik um (Globus, Mikro,
//                  Eingabezeile aus-/einblenden)
//   onHint         Statustext waehrend der Mikrofon-Freigabe (optional)
//   onText         fertiger Transkript-Text
//   onError        Fehlertext fuer den Nutzer
const REC_MAX_SEC = 120;   // harte Obergrenze, danach wird automatisch gesendet
const REC_HOLD_MS = 450;   // laenger gedrueckt = Halte-Geste statt Tipp

function createVoiceRecorder(opts) {
  const bar = opts.bar || null;
  const wave = opts.wave || null;
  const timeEl = opts.time || null;
  let rec = null;      // { raf, levels, lastPush, busy } — sonst nicht am Aufnehmen
  let downTs = 0;      // pointerdown, der die laufende Aufnahme gestartet hat

  function setUi(on) {
    if (bar) bar.hidden = !on;
    if (timeEl && on) { timeEl.textContent = "0:00"; timeEl.classList.remove("warn"); }
    if (opts.onUi) opts.onUi(on);
  }

  // Wellenform: je Tick ein Balken aus dem aktuellen Pegel, aeltere wandern
  // nach links raus — die Bewegung ist der Beweis, dass Ton ankommt.
  function draw() {
    if (!wave || !rec) return;
    const w = wave.clientWidth, h = wave.clientHeight;
    if (!w || !h) return;
    const dpr = window.devicePixelRatio || 1;
    if (wave.width !== Math.round(w * dpr)) {
      wave.width = Math.round(w * dpr);
      wave.height = Math.round(h * dpr);
    }
    const ctx = wave.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const now = performance.now();
    if (now - rec.lastPush >= 55) { rec.lastPush = now; rec.levels.push(_diktatLevel()); }
    const barW = 3, step = 5;
    const maxBars = Math.max(1, Math.floor(w / step));
    if (rec.levels.length > maxBars) rec.levels.splice(0, rec.levels.length - maxBars);
    ctx.fillStyle = getComputedStyle(wave).color;
    for (let i = 0; i < rec.levels.length; i++) {
      // Wurzel-Kennlinie: leise Sprache bewegt den Balken sichtbar, laute
      // laeuft nicht sofort oben an.
      const lvl = Math.min(1, Math.sqrt(rec.levels[i]) * 1.9);
      const bh = Math.max(3, lvl * (h - 4));
      const x = w - (rec.levels.length - i) * step;
      const y = (h - bh) / 2;
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(x, y, barW, bh, barW / 2);
      else ctx.rect(x, y, barW, bh);
      ctx.fill();
    }
  }

  function tick() {
    if (!rec) return;
    const secs = Math.max(0, Math.floor((Date.now() - Diktat.startTs) / 1000));
    if (timeEl) {
      timeEl.textContent = Math.floor(secs / 60) + ":" + String(secs % 60).padStart(2, "0");
      timeEl.classList.toggle("warn", secs >= REC_MAX_SEC - 15);
    }
    draw();
    rec.raf = requestAnimationFrame(tick);
  }

  // Erster Druck: einmalig die Browser-Mikrofon-Freigabe einholen. Sagt der
  // Nutzer ja, laeuft die Aufnahme direkt los — der Tipp war ja als "jetzt
  // sprechen" gemeint.
  async function primeMic(startAfter) {
    if (opts.onHint) opts.onHint("Mikrofon erlauben …");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((t) => t.stop());
      App.micReady = true;
      if (opts.onHint) opts.onHint(SPRECH_TITEL);
      if (startAfter) start();
    } catch (er) {
      if (opts.onHint) opts.onHint((er && er.name === "NotAllowedError")
        ? "Mikrofon abgelehnt — im Browser erlauben"
        : "Mikrofon nicht verfügbar");
    }
  }

  async function start() {
    if (rec) return;
    rec = { raf: 0, levels: [], lastPush: 0, busy: false };
    setUi(true);
    try {
      await _diktatStartRecording();
    } catch (er) {
      rec = null;
      setUi(false);
      _diktatTeardown();
      if (opts.onError) opts.onError((er && er.name === "NotAllowedError")
        ? "Mikrofon-Zugriff abgelehnt. Bitte im Browser erlauben."
        : "Mikrofon nicht verfügbar.");
      return;
    }
    Diktat.autostop = setTimeout(() => { if (rec) stop("send"); }, REC_MAX_SEC * 1000);
    rec.raf = requestAnimationFrame(tick);
  }

  // mode: "send" = transkribieren und weitergeben, "cancel" = wegwerfen.
  async function stop(mode) {
    if (!rec || rec.busy) return;
    rec.busy = true;
    if (rec.raf) cancelAnimationFrame(rec.raf);
    const out = Diktat.recording ? _diktatFinish() : null;
    _diktatTeardown();
    rec = null;
    downTs = 0;
    setUi(false);
    if (mode !== "send" || !out) return;
    if (out.durationSec < 1 || out.blob.size < 2000) {
      if (opts.onError) opts.onError("Aufnahme war zu kurz — bitte nochmal.");
      return;
    }
    let res, j = null;
    try {
      res = await fetch("/app/api/assistent/transkript", {
        method: "POST",
        headers: { "X-CSRF-Token": App.me.csrf, "Content-Type": "audio/wav" },
        body: out.blob,
      });
    } catch (e) {
      if (opts.onError) opts.onError("Netzwerkfehler.");
      return;
    }
    if (res.status === 303 || res.status === 401 || res.redirected) { location.href = "/app/login"; return; }
    try { j = await res.json(); } catch (e) {}
    if (res.ok && j && j.ok && j.text) opts.onText(j.text);
    else if (opts.onError) opts.onError((j && j.error) || "Nichts verstanden — bitte nochmal.");
  }

  // Ein Druck auf den Sprech-Knopf: laeuft nichts → starten; laeuft etwas →
  // senden. Ob es ein Tipp oder ein Halten war, entscheidet erst pointerUp.
  function pointerDown() {
    if (rec) { stop("send"); return; }
    if (!App.micReady) { primeMic(true); return; }
    downTs = Date.now();
    start();
  }

  function pointerUp() {
    if (!downTs) return;
    const held = Date.now() - downTs;
    downTs = 0;
    // Gedrueckt gehalten = Sprechtaste: Loslassen sendet. Kurzer Tipp:
    // Aufnahme laeuft freihaendig weiter.
    if (held >= REC_HOLD_MS) stop("send");
  }

  function pointerCancel() {
    if (!downTs) return;
    const held = Date.now() - downTs;
    downTs = 0;
    if (held >= REC_HOLD_MS) stop("cancel");
  }

  // Fuer Tastatur-Bedienung und die Knoepfe der Leiste.
  function toggle() {
    if (rec) stop("send");
    else if (!App.micReady) primeMic(true);
    else start();
  }

  return {
    start, stop, toggle, pointerDown, pointerUp, pointerCancel,
    isActive: () => !!rec,
    refreshUi: () => { if (rec) setUi(true); },
  };
}

// Mikrofon-Freigabe: War sie in diesem Browser schon erteilt, darf der erste
// Tipp sofort aufnehmen. Die Abfrage blockiert nicht (Permissions-API).
function primeMicPermissionState() {
  if (!navigator.permissions || !navigator.permissions.query) return;
  navigator.permissions.query({ name: "microphone" })
    .then((st) => { if (st.state === "granted") App.micReady = true; })
    .catch(() => {}); // Safari/iOS kennt 'microphone' nicht — dann fragt der erste Druck
}

// ---------- Kunden-Profil (gebündelte Historie) ----------
// Anzeigenamen der Rechte-Rollen. Spiegel von ROLLEN_LABELS in
// core/features/permissions.py — beides zusammen aendern.
const ROLLEN_LABEL = { inhaber: "Inhaber", buero: "Büro", monteur: "Monteur" };

// ---------------------------------------------------------------------
// Rechte im Frontend
// ---------------------------------------------------------------------
//
// Das ist KOSMETIK: durchgesetzt wird serverseitig (Router-Gate in
// core/security/app_auth.py). Hier geht es nur darum, niemandem Knoepfe
// zu zeigen, die ohnehin mit 403 antworten wuerden.
//
// Die Schluessel spiegeln core/features/permissions.py.

let _perms = new Set();
// Liefert der Server noch keine Rechte? Dann laeuft eine aeltere
// Version — Static ist ueber den Bind-Mount SOFORT live, der
// Python-Code erst nach dem Container-Neustart. In diesem Fenster darf
// die App nicht plotzlich halb leer sein, also faellt sie auf das alte
// Verhalten zurueck: Inhaber darf alles, Mitarbeiter das Uebliche.
let _permsUnbekannt = true;

function setPermissions(liste) {
  _permsUnbekannt = !Array.isArray(liste);
  _perms = new Set(liste || []);
}

function can(key) {
  if (_permsUnbekannt) {
    // Alt-Verhalten: alles, was frueher an is_inhaber hing.
    return !!(App.me && App.me.employee && App.me.employee.is_inhaber);
  }
  return _perms.has(key);
}

// Fehlertext aus einer JSON-Antwort ziehen, mit Rueckfall. Die
// Rechte-Endpunkte liefern sprechende Meldungen ("Die eigene Rolle
// kannst du nicht ändern") — die sollen beim Nutzer ankommen.
async function fehlerText(res, fallback) {
  try {
    const j = await res.json();
    if (j && j.error) return j.error;
  } catch (e) {}
  return fallback;
}

// =====================================================================
// Rechte eines Mitarbeiters (Rolle + Feinjustierung)
// =====================================================================
//
// Die Rolle setzt den Standard, einzelne Schalter weichen davon ab.
// Darum zeigt jede Zeile, WOHER ihr Zustand kommt: grau = aus der Rolle,
// hervorgehoben = einzeln gesetzt. Nur so ist spaeter nachvollziehbar,
// warum jemand etwas sieht oder nicht.

async function showTeamRechte(slug) {
  App.view.innerHTML = `<div class="loading">Lädt …</div>`;
  const res = await api(`/app/api/team/${encodeURIComponent(slug)}/rechte`);
  const d = res && res.ok ? await res.json() : null;
  if (!d || !d.ok) {
    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-team" style="margin-bottom:10px">← Zurück</button>` +
      `<div class="card"><p class="empty">Rechte konnten nicht geladen werden.</p></div>`;
    document.getElementById("back-team").addEventListener("click", () => navigate("team"));
    return;
  }

  // Rollen-Auswahl
  const rollenBtns = (d.rollen || [])
    .filter((r) => r.key !== "inhaber")
    .map((r) => `
      <button class="btn-sm ${r.key === d.rolle ? "" : "btn-ghost"}"
              data-rolle="${esc(r.key)}" style="padding:8px 14px">
        ${esc(r.label)}
      </button>`).join("");

  const aktuelleRolle = (d.rollen || []).find((r) => r.key === d.rolle);

  // Rechte nach Gruppe
  const gruppen = {};
  (d.rechte || []).forEach((r) => { (gruppen[r.gruppe] = gruppen[r.gruppe] || []).push(r); });

  const gruppenHtml = Object.keys(gruppen).map((g) => {
    const zeilen = gruppen[g].map((r) => {
      const abweichend = r.override !== null && r.override !== undefined;
      const gesperrt = !r.delegierbar;
      const status = gesperrt
        ? `<span class="sub">nur Inhaber</span>`
        : `<button class="btn-sm ${r.effektiv ? "" : "btn-ghost"}"
                   data-recht="${esc(r.key)}" data-an="${r.effektiv ? "1" : "0"}"
                   style="padding:6px 12px;min-width:64px">${r.effektiv ? "An" : "Aus"}</button>`;
      const herkunft = gesperrt ? ""
        : abweichend
          ? `<button class="btn-sm btn-ghost" data-reset="${esc(r.key)}" style="padding:4px 8px;font-size:12px">↺ Standard</button>`
          : `<span class="sub" style="font-size:12px">aus der Rolle</span>`;
      return `<div class="row" style="display:block;${abweichend ? "" : "opacity:.85"}">
        <div style="display:flex;justify-content:space-between;gap:10px;align-items:center">
          <div><b>${esc(r.label)}</b></div>
          <div style="flex-shrink:0">${status}</div>
        </div>
        <div class="sub" style="word-break:break-word">${esc(r.beschreibung)}</div>
        <div style="margin-top:4px">${herkunft}</div>
      </div>`;
    }).join("");
    return `<div class="card"><div class="section-title" style="margin:0 0 8px">${esc(g)}</div>${zeilen}</div>`;
  }).join("");

  App.view.innerHTML =
    `<button class="btn-sm btn-ghost" id="back-team" style="margin-bottom:10px">← Zurück</button>` +
    `<h1 style="font-size:22px;margin:4px 4px 4px">Rechte · ${esc(d.name)}</h1>` +
    `<div class="sub" style="margin:0 4px 14px">Die Rolle setzt den Standard. Einzelne Schalter weichen davon ab.</div>` +
    `<div class="card">
       <div class="section-title" style="margin:0 0 8px">Rolle</div>
       <div style="display:flex;gap:8px;flex-wrap:wrap">${rollenBtns}</div>
       ${aktuelleRolle ? `<div class="sub" style="margin-top:8px">${esc(aktuelleRolle.beschreibung)}</div>` : ""}
     </div>` +
    gruppenHtml;

  document.getElementById("back-team").addEventListener("click", () => navigate("team"));

  const neuLaden = () => showTeamRechte(slug);

  document.querySelectorAll("[data-rolle]").forEach((b) =>
    b.addEventListener("click", async () => {
      if (b.dataset.rolle === d.rolle) return;
      b.disabled = true;
      const r = await api(`/app/api/team/${encodeURIComponent(slug)}/rolle`,
        { method: "POST", body: JSON.stringify({ rolle: b.dataset.rolle }) });
      if (r && r.ok) { toast("Rolle geändert"); neuLaden(); }
      else { b.disabled = false; toast(await fehlerText(r, "Rolle konnte nicht geändert werden.")); }
    }));

  document.querySelectorAll("[data-recht]").forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      const r = await api(`/app/api/team/${encodeURIComponent(slug)}/recht`,
        { method: "POST", body: JSON.stringify({
            key: b.dataset.recht, allowed: b.dataset.an !== "1",
          }) });
      if (r && r.ok) neuLaden();
      else { b.disabled = false; toast(await fehlerText(r, "Konnte nicht speichern.")); }
    }));

  document.querySelectorAll("[data-reset]").forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      const r = await api(`/app/api/team/${encodeURIComponent(slug)}/recht`,
        { method: "POST", body: JSON.stringify({ key: b.dataset.reset, allowed: null }) });
      if (r && r.ok) neuLaden();
      else { b.disabled = false; toast(await fehlerText(r, "Konnte nicht zurücksetzen.")); }
    }));
}


async function showKundenProfil(name, kundeId) {
  App.screenContext = { screen: "kunden_profil", kunde: name };
  App.view.innerHTML = `<div class="loading">Lädt …</div>`;
  // Mit kunde_id ist das Profil präzise (namensgleiche Kunden bleiben
  // getrennt, Phase 6); ohne fällt es auf den Namen zurück.
  const res = await api("/app/api/kunden/profil?" + (kundeId
    ? "kunde_id=" + encodeURIComponent(kundeId)
    : "name=" + encodeURIComponent(name)));
  const d = res && res.ok ? await res.json() : null;
  if (!d || !d.ok) {
    App.view.innerHTML = `<button class="btn-sm btn-ghost" id="back-kunden" style="margin-bottom:10px">← Zurück</button><div class="card"><p class="empty">Konnte Profil nicht laden.</p></div>`;
    document.getElementById("back-kunden").addEventListener("click", () => navigate("kunden"));
    return;
  }
  const parts = [`<button class="btn-sm btn-ghost" id="back-kunden" style="margin-bottom:10px">← Zurück</button>`];
  parts.push(`<div class="card"><h2>👤 ${esc(d.name)}</h2>`
    + (d.email ? `<div class="row"><span>E-Mail</span><span class="sub">${esc(d.email)}</span></div>` : "")
    + (d.telefon ? `<div class="row"><span>Telefon</span><span class="sub">${esc(d.telefon)}</span></div>` : "")
    + (d.adresse ? `<div class="row"><span>Adresse</span><span class="sub">${esc(d.adresse)}</span></div>` : "")
    + ((d.namensgleiche_kunden || 0) > 1 && !kundeId
      ? `<p class="muted" style="font-size:12px;margin:8px 0 0">⚠️ ${d.namensgleiche_kunden} Kunden tragen diesen Namen — Ansicht zeigt alle zusammen. Über die Kundensuche lassen sie sich getrennt öffnen.</p>` : "")
    + `</div>`);
  // Zusammenführen-Karte (Inhaber-only, wie der Server-Endpoint): weitere
  // Kunden mit exakt diesem Namen. Dieses Profil ist das ZIEL (bleibt),
  // die gewählte Dublette die Quelle — Regeln wie scripts/merge_kunden.py.
  // Nur im präzisen Modus (kunde_id): sonst gäbe es keine klare Richtung.
  const istInhaber = can("kunden.pflegen");
  if (istInhaber && d.kunde_id && (d.dubletten || []).length) {
    const n = d.dubletten.length;
    parts.push(`<div class="card" id="merge-card"><h2>⚠️ Doppelte Kunden?</h2>
      <p class="muted" style="font-size:13px;margin-top:0">Es gibt ${n === 1 ? "einen weiteren Eintrag" : n + " weitere Einträge"} namens „${esc(d.name)}“. Falls das dieselbe Person ist, kannst du sie in dieses Profil${d.merkmal ? " (" + esc(d.merkmal) + ")" : ""} übernehmen — Gespräche, Angebote und Rechnungen wandern mit.</p>
      ${d.dubletten.map((k) => `<div class="row"><span>👤 ${esc(d.name)} <span class="sub">(${esc(k.merkmal)})</span></span><button class="btn-sm" data-merge="${esc(k.id)}" data-merkmal="${esc(k.merkmal)}">Zusammenführen</button></div>`).join("")}
      <p class="muted" id="merge-msg" style="font-size:13px;margin:8px 0 0"></p>
    </div>`);
  }
  // Ablage-Karte: das Hochladen wohnt nicht mehr in einer eigenen Karte,
  // sondern hinter dem + im Kopf (oeffnet zeigeArchivUploadDialog). Die Karte
  // erscheint darum auch OHNE vorhandenen Drive-Ordner, sobald das Feature da
  // ist — sonst haette der erste Upload keinen Einstiegspunkt.
  const hasDriveArchiv = (App.me.features || []).includes("drive_archiv");
  if (d.drive || hasDriveArchiv) {
    parts.push(`<div class="card" id="archiv-card">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:2px">
        <h2 style="flex:1;margin:0">Ablage</h2>
        ${d.drive ? `<a href="${esc(d.drive.url)}" target="_blank" rel="noopener"
          style="font-size:13px;color:var(--accent,#0066cc);text-decoration:none">Drive ↗</a>` : ""}
        ${hasDriveArchiv ? `<button class="btn-sm" id="archiv-add-btn" title="Zum Archiv hinzufügen" aria-label="Zum Archiv hinzufügen"
          style="width:30px;height:30px;padding:0;border-radius:50%;font-size:19px;line-height:1;display:flex;align-items:center;justify-content:center">+</button>` : ""}
      </div>
      ${hasDriveArchiv ? `<div id="archiv-dateien-list" style="min-height:90px"><p class="muted" style="font-size:13px;text-align:center;margin:10px 0">Lädt …</p></div>` : ""}
    </div>`);
  }
  parts.push(`<div class="card"><h2>Gespräche (${(d.gespraeche || []).length})</h2>${
    (d.gespraeche || []).length ? d.gespraeche.map((x) => rowTap(x.briefing || "Aufnahme", "", x.zeit, x.id)).join("") : emptyRow("Keine Gespräche")
  }</div>`);
  parts.push(`<div class="card"><h2>Angebote / Aufträge (${(d.angebote || []).length})</h2>${
    (d.angebote || []).length ? d.angebote.map((x) => rowPill(x.betrag, x.zeit, x.status, x.pill)).join("") : emptyRow("Keine Angebote")
  }</div>`);
  parts.push(`<div class="card"><h2>Rechnungen (${(d.rechnungen || []).length})</h2>${
    (d.rechnungen || []).length ? d.rechnungen.map((x) => rowPill(x.betrag + (x.nummer ? " · " + esc(x.nummer) : ""), x.zeit, x.status, x.pill)).join("") : emptyRow("Keine Rechnungen")
  }</div>`);
  // Manueller Merge (Inhaber): findet die Fälle, die die Dubletten-Karte
  // nicht sieht — derselbe Kunde unter ANDEREM Namen (z. B. nach Heirat).
  // Immer genau zwei auf einmal: gewählter Eintrag -> dieses Profil.
  if (istInhaber && d.kunde_id) {
    parts.push(`<div class="card" id="merge-pick-card"><h2>🔗 Kunden zusammenführen</h2>
      <p class="muted" style="font-size:12px;margin-top:0">Existiert ${esc(d.name)} noch unter anderem Namen (z. B. nach Heirat)? Eintrag suchen und in dieses Profil übernehmen.</p>
      <input type="text" id="merge-pick-q" placeholder="Namen oder E-Mail suchen …" autocomplete="off" style="width:100%">
      <div id="merge-pick-list"></div>
    </div>`);
  }
  App.view.innerHTML = parts.join("");
  document.getElementById("back-kunden").addEventListener("click", () => navigate("kunden"));
  document.querySelectorAll("[data-merge]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const dub = (d.dubletten || []).find((k) => k.id === btn.dataset.merge);
      if (dub) zeigeMergeDialog(d, dub);
    });
  });
  const pickQ = document.getElementById("merge-pick-q");
  if (pickQ) {
    let pickTimer = null;
    pickQ.addEventListener("input", () => {
      clearTimeout(pickTimer);
      pickTimer = setTimeout(async () => {
        const list = document.getElementById("merge-pick-list");
        if (!list) return;
        const q = pickQ.value.trim();
        if (q.length < 2) { list.innerHTML = ""; return; }
        const res = await api("/app/api/kunden?q=" + encodeURIComponent(q));
        const j = res ? await res.json().catch(() => null) : null;
        // Das eigene Profil ist als Ziel gesetzt und fliegt raus —
        // so bleiben es immer genau zwei Kunden pro Merge.
        const treffer = ((j && j.kunden) || [])
          .filter((k) => k.id !== d.kunde_id).slice(0, 8);
        list.innerHTML = treffer.length
          ? treffer.map((k) => `<button class="row menu-item" data-pick="${esc(k.id)}"><span>👤 ${esc(k.name)}${k.merkmal ? ` <span class="sub">(${esc(k.merkmal)})</span>` : ""}</span></button>`).join("")
          : `<p class="muted" style="font-size:13px;margin:8px 0 0">Kein anderer Kunde gefunden.</p>`;
        list.querySelectorAll("[data-pick]").forEach((b) => {
          b.addEventListener("click", async () => {
            // Kontaktdaten des Kandidaten fürs Hauptdaten-Wählen holen
            // (email_stamm/telefon/adresse liefert das Profil präzise).
            const pres = await api("/app/api/kunden/profil?kunde_id="
              + encodeURIComponent(b.dataset.pick));
            const p = pres ? await pres.json().catch(() => null) : null;
            if (!p || !p.ok) return;
            zeigeMergeDialog(d, {
              id: p.kunde_id, name: p.name, merkmal: p.merkmal || "",
              email: p.email_stamm, telefon: p.telefon, adresse: p.adresse,
            }, document.getElementById("merge-pick-card"));
          });
        });
      }, 300);
    });
  }
  if (hasDriveArchiv) {
    // Auch ohne Drive-Ordner laden: der Endpoint liefert dann eine leere
    // Liste und die drei Kacheln stehen einheitlich mit 0 da.
    loadArchivDateien(d.name);
    document.getElementById("archiv-add-btn")
      .addEventListener("click", () => zeigeArchivUploadDialog(d));
  }
  bindAufnahmen();
}

// Merge-Dialog in der Dubletten-Karte: bei abweichenden Kontaktdaten
// entscheidet der Inhaber pro Feld, welcher Wert die Hauptdaten stellt
// (Default: dieses Profil = Ziel, wie im CLI-Skript). Der nicht gewählte
// Wert wird verworfen — das sagt der Dialog auch dazu. Felder, die nur
// der andere Eintrag hat, werden ohne Nachfrage ergänzt.
function zeigeMergeDialog(d, dub, cardEl) {
  const card = cardEl || document.getElementById("merge-card");
  if (!card) return;
  // email_stamm statt d.email: d.email kann ein Angebots-Fallback sein
  // und stünde dann fälschlich als "vorhandener" Ziel-Wert zur Wahl.
  // name läuft mit: beim Merge über Namensgrenzen (Heirat) ist der
  // Name selbst eine Hauptdaten-Entscheidung.
  const stamm = { name: d.name, email: d.email_stamm, telefon: d.telefon, adresse: d.adresse };
  const LABELS = { name: "Name", email: "E-Mail", telefon: "Telefon", adresse: "Adresse" };
  const quellName = dub.name || d.name;
  const wahlFelder = [];
  const ergaenzt = [];
  for (const feld of ["name", "email", "telefon", "adresse"]) {
    const z = (stamm[feld] || "").trim();
    const q = (dub[feld] || "").trim();
    if (z && q && z !== q) wahlFelder.push(feld);
    else if (q && !z) ergaenzt.push(feld);
  }
  card.innerHTML = `<h2>Zusammenführen</h2>
    <p class="muted" style="font-size:13px;margin-top:0">„${esc(quellName)}${dub.merkmal ? " (" + esc(dub.merkmal) + ")" : ""}“ geht in dieses Profil auf — Gespräche, Angebote und Rechnungen wandern mit.${wahlFelder.length ? " Bei abweichenden Daten wählst du, welcher Wert künftig gilt; der andere wird verworfen." : ""}</p>
    ${wahlFelder.map((f) => `
      <div style="margin:10px 0">
        <div class="muted" style="font-size:12px;margin-bottom:4px">${LABELS[f]}</div>
        <label class="row" style="gap:8px"><input type="radio" name="mf-${f}" value="ziel" checked><span style="flex:1">${esc(stamm[f])} <span class="sub">(dieses Profil)</span></span></label>
        <label class="row" style="gap:8px"><input type="radio" name="mf-${f}" value="quelle"><span style="flex:1">${esc(dub[f])} <span class="sub">(anderer Eintrag)</span></span></label>
      </div>`).join("")}
    ${ergaenzt.length ? `<p class="muted" style="font-size:12px">Wird ergänzt (hier bisher leer): ${ergaenzt.map((f) => `${LABELS[f]} ${esc(dub[f])}`).join(", ")}</p>` : ""}
    <p class="muted" style="font-size:12px">Nicht per Klick rückgängig zu machen.</p>
    <button class="btn-sm" id="merge-go" style="width:100%;margin-top:4px">Jetzt zusammenführen</button>
    <button class="btn-sm btn-ghost" id="merge-cancel" style="width:100%;margin-top:8px">Abbrechen</button>
    <p class="muted" id="merge-msg" style="font-size:13px;margin:8px 0 0"></p>`;
  document.getElementById("merge-cancel").addEventListener("click",
    () => showKundenProfil(d.name, d.kunde_id));
  document.getElementById("merge-go").addEventListener("click", async () => {
    const felder = {};
    for (const f of wahlFelder) {
      const sel = card.querySelector(`input[name="mf-${f}"]:checked`);
      if (sel) felder[f] = sel.value;
    }
    const goBtn = document.getElementById("merge-go");
    goBtn.disabled = true;
    document.getElementById("merge-msg").textContent = "Führt zusammen …";
    const res = await api("/app/api/kunden/merge", {
      method: "POST",
      body: JSON.stringify({ quelle_id: dub.id, ziel_id: d.kunde_id, felder }),
    });
    const j = res ? await res.json().catch(() => null) : null;
    if (j && j.ok) {
      // Profil neu laden — Karte verschwindet, übernommene Einträge
      // tauchen in den Listen auf.
      showKundenProfil(d.name, d.kunde_id);
    } else {
      goBtn.disabled = false;
      document.getElementById("merge-msg").textContent =
        (j && j.error) || "Zusammenführen fehlgeschlagen.";
    }
  });
}

// Lädt Dateiliste aus Drive und rendert drei Kacheln (Bilder / PDFs / Notizen)
// im home-tile-Stil nebeneinander. Kacheln mit 0 Dateien sind ausgegraut.
async function loadArchivDateien(kundeName) {
  const container = document.getElementById("archiv-dateien-list");
  if (!container) return;
  const res = await api("/app/api/archiv/dateien?kunde_name=" + encodeURIComponent(kundeName));
  const j = res ? await res.json().catch(() => null) : null;
  const all = (j && j.ok && j.dateien) ? j.dateien : [];
  const fileMap = Object.fromEntries(all.map((f) => [f.id, f]));

  const groups = [
    { key: "bilder",  label: "Bilder",  ico: "📷", files: all.filter((f) => f.is_image) },
    { key: "pdfs",    label: "PDFs",    ico: "📄", files: all.filter((f) => !f.is_image && f.mime_type === "application/pdf") },
    { key: "notizen", label: "Notizen", ico: "📝", files: all.filter((f) => !f.is_image && f.mime_type === "text/plain") },
  ];

  let html = `<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:4px">`;
  for (const g of groups) {
    const empty = g.files.length === 0;
    html += `<button class="home-tile" data-archivgroup="${esc(g.key)}"
      ${empty ? 'disabled style="opacity:.35;cursor:default"' : ""}>
      <span class="tile-ico">${g.ico}</span>
      <span class="tile-label">${g.label}</span>
      <span class="tile-count">${g.files.length}</span>
    </button>`;
  }
  html += `</div>`;
  container.innerHTML = html;

  container.querySelectorAll("[data-archivgroup]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const g = groups.find((x) => x.key === btn.dataset.archivgroup);
      if (g && g.files.length) showArchivKategorie(kundeName, g, fileMap);
    });
  });
}

// Zeigt eine Kategorie (Bilder / PDFs / Notizen) als eigene Unterseite.
// Bilder erscheinen als zusammenhängendes Album-Grid, alles andere als Liste.
function showArchivKategorie(kundeName, group, fileMap) {
  let contentHtml;
  if (group.key === "bilder") {
    contentHtml = `<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:2px;margin-top:12px;border-radius:12px;overflow:hidden">`;
    for (const f of group.files) {
      contentHtml += `<button data-archivfid="${esc(f.id)}" title="${esc(f.name)}"
        style="display:block;aspect-ratio:1;overflow:hidden;background:var(--line,#eee);border:none;padding:0;cursor:pointer">
        <img src="/app/api/archiv/datei/${esc(f.id)}?thumb=1" alt="${esc(f.name)}" loading="lazy"
          style="width:100%;height:100%;object-fit:cover;pointer-events:none">
      </button>`;
    }
    contentHtml += `</div>`;
  } else {
    contentHtml = `<div class="card" style="margin-top:12px">`;
    for (const f of group.files) {
      contentHtml += `<button data-archivfid="${esc(f.id)}" class="row"
        style="background:transparent;border:none;cursor:pointer;width:100%;text-align:left;color:inherit">
        <div><div>${group.ico} ${esc(f.name)}</div></div>
        <span class="sub">›</span>
      </button>`;
    }
    contentHtml += `</div>`;
  }

  App.view.innerHTML =
    `<button class="btn-sm btn-ghost" id="back-archiv-kat" style="margin-bottom:10px">← Zurück</button>
     <div class="card"><h2>${group.ico} ${group.label} · ${esc(kundeName)}</h2></div>
     ${contentHtml}`;

  document.getElementById("back-archiv-kat").addEventListener("click", () => showKundenProfil(kundeName));
  App.view.querySelectorAll("[data-archivfid]").forEach((btn) =>
    btn.addEventListener("click", () => showArchivPreview(fileMap[btn.dataset.archivfid]))
  );
}

// Zeigt eine Datei aus dem Kunden-Archiv direkt in der App an (Bild, PDF oder Notiz-Text).
function showArchivPreview(f) {
  if (!f) return;
  document.getElementById("archiv-preview-modal")?.remove();

  const proxyUrl = `/app/api/archiv/datei/${encodeURIComponent(f.id)}`;
  let contentHtml;
  if (f.is_image) {
    contentHtml = `<img src="${esc(proxyUrl)}" alt="${esc(f.name)}"
      style="max-width:100%;max-height:65vh;object-fit:contain;border-radius:8px;display:block;margin:auto">`;
  } else if (f.mime_type === "application/pdf") {
    contentHtml = `<iframe src="${esc(proxyUrl)}"
      style="width:100%;height:65vh;border:none;border-radius:8px;display:block"></iframe>`;
  } else {
    contentHtml = `<pre id="archiv-note-text"
      style="margin:0;white-space:pre-wrap;font-size:14px;line-height:1.6;font-family:inherit;color:var(--fg,#111)">Lädt …</pre>`;
  }

  const hasViz = f.is_image && (App.me.features || []).includes("visualisierung");
  const actionsHtml = f.is_image && hasViz
    ? `<div id="archiv-modal-actions" style="border-top:1px solid var(--line,#eee);padding:10px 16px;display:flex;gap:8px;flex-shrink:0">
         <button id="archiv-modal-viz-btn" class="btn-sm" style="flex:1">🎨 Visualisieren</button>
       </div>`
    : "";

  const modal = document.createElement("div");
  modal.id = "archiv-preview-modal";
  modal.style.cssText = "position:fixed;inset:0;z-index:9;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;padding:16px 16px calc(60px + env(safe-area-inset-bottom)) 16px";
  modal.innerHTML = `
    <div style="background:var(--bg,#fff);border-radius:16px;width:100%;max-width:720px;max-height:92vh;overflow:hidden;display:flex;flex-direction:column;box-shadow:0 8px 40px rgba(0,0,0,.3)">
      <div style="display:flex;align-items:center;gap:10px;padding:14px 16px;border-bottom:1px solid var(--line,#eee);flex-shrink:0">
        <span style="flex:1;font-weight:600;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(f.name)}</span>
        <a href="${esc(f.web_link)}" target="_blank" rel="noopener"
          style="font-size:13px;color:var(--accent,#0066cc);white-space:nowrap;text-decoration:none;flex-shrink:0">Drive ↗</a>
        <button id="archiv-modal-close" aria-label="Schließen"
          style="border:none;background:transparent;font-size:22px;line-height:1;cursor:pointer;padding:0 2px;color:var(--fg,#111);flex-shrink:0">✕</button>
      </div>
      <div style="flex:1;overflow:auto;padding:16px">${contentHtml}</div>
      ${actionsHtml}
    </div>`;

  if (f.is_image) App._archivPreviewFile = { proxyUrl, name: f.name };

  function closeModal() { App._archivPreviewFile = null; modal.remove(); }

  document.body.appendChild(modal);
  modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });
  modal.querySelector("#archiv-modal-close").addEventListener("click", closeModal);

  modal.querySelector("#archiv-modal-viz-btn")?.addEventListener("click", () => {
    const actionsDiv = document.getElementById("archiv-modal-actions");
    if (!actionsDiv) return;
    actionsDiv.innerHTML =
      `<input id="archiv-viz-input" type="text" placeholder="Was soll verändert werden?" autocomplete="off"
         style="flex:1;padding:10px 12px;border:1px solid var(--line,#eee);border-radius:10px;font-size:15px;font-family:inherit" />
       <button id="archiv-viz-go" class="btn-sm" style="flex-shrink:0">Los</button>`;
    actionsDiv.style.alignItems = "center";
    const vizInput = document.getElementById("archiv-viz-input");
    const vizGo = document.getElementById("archiv-viz-go");
    vizInput.focus();

    async function startViz() {
      const prompt = (vizInput.value || "").trim();
      if (!prompt) { vizInput.focus(); return; }
      vizInput.disabled = true; vizGo.disabled = true; vizGo.textContent = "Lädt …";
      let blob;
      try { const r = await fetch(proxyUrl); blob = await r.blob(); } catch (_) {
        vizInput.disabled = false; vizGo.disabled = false; vizGo.textContent = "Los"; return;
      }
      const file = new File([blob], f.name, { type: blob.type || "image/jpeg" });
      App.qPendingFile = file;
      App.qPendingPreviewUrl = proxyUrl;
      App._autoVizPrompt = prompt;
      closeModal();
      navigate("assistent");
    }

    vizGo.addEventListener("click", startViz);
    vizInput.addEventListener("keydown", (e) => { if (e.key === "Enter") startViz(); });
  });

  if (!f.is_image && f.mime_type !== "application/pdf") {
    fetch(proxyUrl)
      .then((r) => r.text())
      .then((txt) => { const el = document.getElementById("archiv-note-text"); if (el) el.textContent = txt; })
      .catch(() => { const el = document.getElementById("archiv-note-text"); if (el) el.textContent = "Fehler beim Laden."; });
  }
}

// Archiv-Upload im Kunden-Profil: oeffnet als Dialog ueber das + in der
// Ablage-Karte. Foto/PDF (roher fetch mit Datei-MIME, wie Beleg-Upload)
// + optionale/eigenständige Notiz. Nach Erfolg schliesst der Dialog und das
// Profil lädt neu (Datei-Zähler/Ordner-Link aktualisieren).
function zeigeArchivUploadDialog(d) {
  document.getElementById("archiv-upload-modal")?.remove();
  const modal = document.createElement("div");
  modal.id = "archiv-upload-modal";
  // Gleiche Optik wie das Archiv-Vorschau-Modal (z-index 9: unter der Tabbar,
  // das Padding unten haelt den Inhalt oberhalb der Leiste).
  modal.style.cssText = "position:fixed;inset:0;z-index:9;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;padding:16px 16px calc(60px + env(safe-area-inset-bottom)) 16px";
  modal.innerHTML = `
    <div style="background:var(--bg,#fff);border-radius:16px;width:100%;max-width:480px;max-height:92vh;overflow:hidden;display:flex;flex-direction:column;box-shadow:0 8px 40px rgba(0,0,0,.3)">
      <div style="display:flex;align-items:center;gap:10px;padding:14px 16px;border-bottom:1px solid var(--line,#eee);flex-shrink:0">
        <span style="flex:1;font-weight:600;font-size:14px">📎 Zum Archiv hinzufügen</span>
        <button id="archiv-upload-close" aria-label="Schließen"
          style="border:none;background:transparent;font-size:22px;line-height:1;cursor:pointer;padding:0 2px;color:var(--fg,#111);flex-shrink:0">✕</button>
      </div>
      <div style="flex:1;overflow:auto;padding:16px">
        <p class="muted" style="font-size:12px;margin-top:0">Foto, PDF oder Notiz landet im Google-Drive-Ordner von ${esc(d.name)}.</p>
        <input type="file" id="arch-file" accept="image/jpeg,image/png,image/webp,application/pdf" capture="environment" style="display:none">
        <button class="btn-sm" id="arch-file-btn" style="width:100%;margin-bottom:8px">📷 Foto / PDF hochladen</button>
        <textarea id="arch-note" rows="2" placeholder="Notiz (optional, wird beim Hochladen mitgespeichert) …" style="width:100%;padding:10px;border:1px solid var(--line);border-radius:10px;font-size:15px;margin-bottom:8px"></textarea>
        <button class="btn-sm btn-ghost" id="arch-note-btn" style="width:100%">📝 Nur Notiz speichern</button>
        <p class="muted" id="arch-msg" style="font-size:13px;margin-top:8px;text-align:center"></p>
      </div>
    </div>`;
  document.body.appendChild(modal);
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.remove(); });
  modal.querySelector("#archiv-upload-close").addEventListener("click", () => modal.remove());

  const fileBtn = document.getElementById("arch-file-btn");
  const fileEl = document.getElementById("arch-file");
  const noteBtn = document.getElementById("arch-note-btn");
  const noteEl = document.getElementById("arch-note");
  const msg = document.getElementById("arch-msg");
  const setM = (t, ok) => { msg.textContent = t; msg.style.color = ok ? "var(--ok,#1a7f37)" : "var(--err,#b42318)"; };
  const q = (k, v) => (v ? `&${k}=${encodeURIComponent(v)}` : "");
  const reload = () => setTimeout(() => { modal.remove(); showKundenProfil(d.name, d.kunde_id); }, 700);

  fileBtn.addEventListener("click", () => fileEl.click());
  fileEl.addEventListener("change", async () => {
    const f = fileEl.files && fileEl.files[0];
    if (!f) return;
    if (f.size > 25 * 1024 * 1024) { setM("Datei zu groß (max 25 MB).", false); fileEl.value = ""; return; }
    const orig = fileBtn.textContent;
    fileBtn.disabled = true; fileBtn.textContent = "Lädt hoch …"; setM("", true);
    const caption = (noteEl.value || "").trim();
    const url = "/app/api/archiv/upload?kunde_name=" + encodeURIComponent(d.name)
      + q("kunde_email", d.email) + q("filename", f.name) + q("caption", caption);
    let j = null;
    try {
      const r = await fetch(url, {
        method: "POST",
        headers: { "X-CSRF-Token": App.me.csrf, "Content-Type": f.type || "application/octet-stream" },
        body: f,
      });
      if (r.status === 303 || r.status === 401) { location.href = "/app/login"; return; }
      j = await r.json().catch(() => null);
    } catch (e) { /* Netzfehler -> j bleibt null */ }
    fileBtn.disabled = false; fileBtn.textContent = orig; fileEl.value = "";
    if (j && j.ok) { noteEl.value = ""; setM("✓ Hochgeladen.", true); reload(); }
    else { setM((j && j.error) || "Upload fehlgeschlagen.", false); }
  });

  noteBtn.addEventListener("click", async () => {
    const text = (noteEl.value || "").trim();
    if (text.length < 2) { setM("Notiz ist leer.", false); return; }
    const orig = noteBtn.textContent;
    noteBtn.disabled = true; noteBtn.textContent = "Speichert …"; setM("", true);
    const r = await api("/app/api/archiv/notiz", {
      method: "POST",
      body: JSON.stringify({ kunde_name: d.name, text, kunde_email: d.email || "" }),
    });
    const j = r ? await r.json().catch(() => null) : null;
    noteBtn.disabled = false; noteBtn.textContent = orig;
    if (j && j.ok) { noteEl.value = ""; setM("✓ Notiz gespeichert.", true); reload(); }
    else { setM((j && j.error) || "Konnte Notiz nicht speichern.", false); }
  });
}

// ---------- Material-Bestellverlauf ----------
async function showMaterialBestellungen() {
  App.view.innerHTML = `<div class="loading">Lädt …</div>`;
  const res = await api("/app/api/material/bestellungen");
  const d = res && res.ok ? await res.json() : { bestellungen: [] };
  const list = (d.bestellungen || []).map((o) =>
    row(o.material, `${o.menge} ${o.einheit}`, o.zeit)).join("");
  App.view.innerHTML =
    `<button class="btn-sm btn-ghost" id="back-material" style="margin-bottom:10px">← Zurück</button>` +
    `<h1 style="font-size:22px;margin:4px 4px 14px">Bestellverlauf</h1>` +
    `<div class="card"><h2>Letzte Bestellungen</h2>${
      list || emptyRow("Noch keine Bestellungen")
    }</div>`;
  document.getElementById("back-material").addEventListener("click", () => navigate("material"));
}

// ---------- Aufträge-Lifecycle-Board ----------
// Vorwaerts-Schritt je Status. arbeit_fertig -> rechnung_gesendet fehlt
// bewusst: das ist der Geld-Pfad (Lexware finalisieren + Rechnung mailen),
// der über den Rechnungs-Flow läuft, nicht hier.
const AUFTRAG_NEXT = {
  rechnung_erstellt: { status: "accepted", label: "✅ Angenommen" },
  accepted: { status: "arbeit_laeuft", label: "🔨 Arbeit läuft" },
  arbeit_laeuft: { status: "arbeit_fertig", label: "🏁 Fertig" },
};

// Der Fortschritts-Regler gehört an den Schritt, der die Arbeit
// symbolisiert („🔨 Arbeit läuft"). Er ist der einzige Schritt, der einen
// Zwischenstand hat — alle anderen sind an/aus. Bei 100 % meldet der
// Server den Auftrag fertig und wir leiten in den Rechnungs-Flow.
// ohneStunden: in der Detailansicht hat die Stunden-Karte das Eingabefeld.
// Zweimal dasselbe `data-std-*` im Dokument waere ein echter Fehler — die
// Buchung liest ihr Feld per Attribut und erwischte sonst das falsche.
function fortschrittsRegler(a, ohneStunden) {
  if (!a.in_arbeit) return "";
  const pct = Math.max(0, Math.min(100, a.fortschritt || 0));
  return `<div class="fortschritt">
    <div class="fortschritt-head"><span>Fortschritt</span>
      <b data-slider-val="${esc(a.id)}">${pct}%</b></div>
    <input type="range" min="0" max="100" step="5" value="${pct}"
      data-slider="${esc(a.id)}" aria-label="Arbeits-Fortschritt in Prozent">
    ${ohneStunden ? "" : stundenFeld(a, false)}
  </div>`;
}

// Stunden auf den Auftrag buchen — direkt am Regler, weil der Handwerker
// genau dort steht, wenn er den Tag abschließt. Gebucht wird immer auf den
// angemeldeten Mitarbeiter; die Zeile darunter zeigt, wer schon wie lange
// dran war. Das ist Auftragszeit für die Nachkalkulation, keine
// Anwesenheitserfassung (siehe core/models/auftrag_stunden.py).
function stundenFeld(a, mitNotiz) {
  const id = esc(a.id);
  const feld = "min-width:0;padding:8px;border:1px solid var(--line);border-radius:8px;font-size:15px";
  return `<div style="margin-top:10px">
    <div style="display:flex;gap:6px;align-items:center">
      <input type="text" inputmode="decimal" data-std-input="${id}" placeholder="Std."
        aria-label="Gearbeitete Stunden" style="width:72px;${feld}">
      ${mitNotiz ? `<input type="text" data-std-notiz="${id}" placeholder="wofür (optional)"
        aria-label="Wofür" style="flex:1;${feld}">` : ""}
      <button class="btn-sm btn-ghost" data-std-add="${id}" style="padding:8px 10px">Buchen</button>
    </div>
    <div class="sub" data-std-summe="${id}" style="margin-top:6px">${esc(a.stunden_text || "")}</div>
  </div>`;
}

function _stundenSummeText(st) {
  return ((st && st.je_mitarbeiter) || []).map((x) => `${x.name} ${x.text}`).join(" · ");
}

// onUpdate(uebersicht) läuft nach einer erfolgreichen Buchung — die
// Detailansicht zeichnet damit ihre Aufschlüsselung neu, die Listenkarte
// begnügt sich mit der aktualisierten Zeile darunter.
function bindStundenBuchung(onUpdate) {
  // Die Auftragskarte öffnet beim Klick das Detail — Eingaben dürfen das
  // nicht auslösen.
  document.querySelectorAll("[data-std-input],[data-std-notiz]").forEach((el) =>
    el.addEventListener("click", (ev) => ev.stopPropagation()));

  document.querySelectorAll("[data-std-add]").forEach((btn) => {
    const id = btn.dataset.stdAdd;
    btn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const feld = document.querySelector(`[data-std-input="${CSS.escape(id)}"]`);
      const notizFeld = document.querySelector(`[data-std-notiz="${CSS.escape(id)}"]`);
      const wert = ((feld && feld.value) || "").trim();
      if (!wert) { if (feld) feld.focus(); return; }
      btn.disabled = true;
      const res = await api(`/app/api/auftraege/${encodeURIComponent(id)}/stunden`,
        { method: "POST", body: JSON.stringify({
          stunden: wert, notiz: notizFeld ? notizFeld.value : "" }) });
      if (!res) return;                       // api() hat schon umgeleitet
      let j = null;
      try { j = await res.json(); } catch (e) {}
      btn.disabled = false;
      if (!j || !j.ok) { alert((j && j.error) || "Stunden konnten nicht gebucht werden."); return; }
      if (feld) feld.value = "";
      if (notizFeld) notizFeld.value = "";
      const summe = document.querySelector(`[data-std-summe="${CSS.escape(id)}"]`);
      if (summe) summe.textContent = _stundenSummeText(j.stunden);
      if (onUpdate) onUpdate(j.stunden);
    });
  });
}

// Bindet alle Regler im aktuellen Screen. onFertig() läuft, wenn der
// Handwerker auf 100 % zieht (Auftrag ist damit fertiggemeldet).
function bindFortschrittsRegler(onFertig) {
  document.querySelectorAll("[data-slider]").forEach((sl) => {
    const id = sl.dataset.slider;
    const val = document.querySelector(`[data-slider-val="${CSS.escape(id)}"]`);
    sl.addEventListener("click", (ev) => ev.stopPropagation());
    sl.addEventListener("input", () => { if (val) val.textContent = sl.value + "%"; });
    sl.addEventListener("change", async () => {
      sl.disabled = true;
      const res = await api(`/app/api/auftraege/${encodeURIComponent(id)}/fortschritt`,
        { method: "POST", body: JSON.stringify({ fortschritt: parseInt(sl.value, 10) }) });
      if (!res) return;                       // api() hat schon umgeleitet
      let j = null;
      try { j = await res.json(); } catch (e) {}
      if (j && j.ok && j.fertig) { (onFertig || openRechnungInQ)(id); return; }
      if (!j || !j.ok) alert((j && j.error) || "Speichern fehlgeschlagen.");
      sl.disabled = false;
    });
  });
}

function auftragCard(a, isInhaber) {
  const pill = a.abgebrochen ? "danger" : (a.status === "rechnung_gesendet" ? "ok" : "warn");
  const progress = (a.schritt != null) ? ` · Schritt ${a.schritt + 1}/${a.schritte_gesamt}` : "";
  let actions = "";
  if (isInhaber && !a.abgebrochen && a.status !== "rechnung_gesendet") {
    const next = AUFTRAG_NEXT[a.status];
    const btns = [];
    if (next) {
      btns.push(`<button class="btn-sm" data-auftrag="${esc(a.id)}" data-status="${next.status}" style="padding:6px 10px">${next.label}</button>`);
    } else if (a.status === "arbeit_fertig") {
      btns.push(`<button class="btn-sm" data-rechnung="${esc(a.id)}" style="padding:6px 10px">🧾 Rechnung stellen</button>`);
    }
    btns.push(`<button class="btn-sm btn-ghost" data-auftrag="${esc(a.id)}" data-status="abgebrochen" style="padding:6px 10px">Abbrechen</button>`);
    actions = `<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;align-items:center">${btns.join("")}</div>`;
  }
  // Die ganze Karte öffnet die Detailansicht; die Buttons darin stoppen
  // die Weitergabe, damit ein Klick auf „Abbrechen" nicht zusätzlich das
  // Detail aufblättert.
  return `<div class="card" data-auftrag-open="${esc(a.id)}" style="cursor:pointer">
    <div class="row"><div><div><b>${esc(a.kunde)}</b></div><div class="sub">${esc(a.betrag)}${esc(progress)} · ${esc(a.zeit)}</div></div>
    <span class="pill ${pill}">${esc(a.status_label)} ›</span></div>
    ${fortschrittsRegler(a)}
    ${actions}
  </div>`;
}

// refresh(): womit der Screen nach einer Status-Änderung neu gezeichnet wird.
function bindAuftragActions(refresh) {
  const neuLaden = refresh || (() => navigate(App.current || "auftraege_page"));
  document.querySelectorAll("[data-auftrag]").forEach((b) =>
    b.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const status = b.dataset.status;
      if (status === "abgebrochen" && !confirm("Auftrag wirklich abbrechen?")) return;
      b.disabled = true;
      const res = await api("/app/api/auftraege/" + encodeURIComponent(b.dataset.auftrag) + "/status",
        { method: "POST", body: JSON.stringify({ status }) });
      if (res && res.ok) {
        const j = await res.json();
        if (j.ok) { neuLaden(); return; }
        alert(j.error || "Konnte Status nicht setzen.");
      } else {
        alert("Konnte Status nicht setzen.");
      }
      b.disabled = false;
    }));
  document.querySelectorAll("[data-rechnung]").forEach((b) =>
    b.addEventListener("click", (ev) => {
      ev.stopPropagation();
      openRechnungInQ(b.dataset.rechnung);
    }));
}

// Karten-Klick -> Detailansicht. Getrennt gebunden, weil die Karten in
// mehreren Screens (laufend, abgeschlossen, Büro) auftauchen.
function bindAuftragOeffnen(zurueck) {
  document.querySelectorAll("[data-auftrag-open]").forEach((c) =>
    c.addEventListener("click", () => showAuftragDetail(c.dataset.auftragOpen, zurueck)));
}

// ---------- Auftragsprozess-Editor (Aktivitätsdiagramm) ----------
// Vollbild-Overlay: der Ablauf eines Auftrags als Boxen mit Pfeilen. Die
// fünf Kern-Schritte sind gesperrt (an ihnen hängen Rechnungsversand und
// Fortschritts-Regler), dazwischen legt der Betrieb eigene Aktivitäten an,
// zieht sie an die richtige Stelle und löscht sie wieder.
//
// Drag & Drop läuft über Pointer-Events statt der HTML5-Drag-API: die
// funktioniert auf Touch-Geräten nicht — und die App ist eine PWA.

async function showProzessEditor(zurueck) {
  const res = await api("/app/api/auftragsprozess");
  if (!res || !res.ok) { alert("Konnte den Prozess nicht laden."); return; }
  const d = await res.json();
  let entwurf = (d.schritte || []).map((s) => ({ ...s }));
  let schmutzig = false;

  const ov = el(`<div class="proc-overlay">
    <div class="proc-head">
      <button class="btn-sm" id="pz-zu">← Zurück</button>
      <span class="titel">Auftragsprozess</span>
      <button class="btn-sm" id="pz-save">Speichern</button>
    </div>
    <div class="proc-canvas" id="pz-canvas"></div>
    <div class="proc-foot">
      <div class="zeile">
        <input type="text" id="pz-neu" maxlength="60" placeholder="Neue Aktivität, z.B. Aufmaß nehmen">
        <button class="btn-sm" id="pz-add">+ Hinzufügen</button>
      </div>
      <p class="muted" style="margin:8px 0 0;font-size:12px">Am Griff ⠿ ziehen, um eine Aktivität zwischen andere zu schieben. Die blauen Schritte sind fest.</p>
    </div>
  </div>`);
  document.body.appendChild(ov);
  const canvas = ov.querySelector("#pz-canvas");

  const zeichnen = () => {
    canvas.innerHTML = "";
    entwurf.forEach((s, i) => {
      if (i > 0) canvas.appendChild(el(`<div class="proc-pfeil"></div>`));
      const box = el(`<div class="proc-box ${s.typ === "kern" ? "kern" : ""}" data-idx="${i}">
        ${s.typ === "kern" ? `<span class="schloss">🔒</span>` : `<span class="griff">⠿</span>`}
        <span class="lbl">${esc(s.label)}</span>
        ${s.typ === "kern" ? "" : `<button class="weg" title="Aktivität löschen">✕</button>`}
      </div>`);
      const weg = box.querySelector(".weg");
      if (weg) weg.addEventListener("click", () => {
        if (!confirm(`„${s.label}" aus dem Prozess entfernen?`)) return;
        entwurf.splice(i, 1); schmutzig = true; zeichnen();
      });
      const griff = box.querySelector(".griff");
      if (griff) griff.addEventListener("pointerdown", (ev) => zieheStart(ev, i, box));
      canvas.appendChild(box);
    });
  };

  // --- Ziehen ---
  let zug = null;
  function zieheStart(ev, idx, box) {
    ev.preventDefault();
    const rest = entwurf.filter((_, i) => i !== idx);
    const geist = box.cloneNode(true);
    geist.classList.add("proc-geist");
    geist.style.width = box.offsetWidth + "px";
    document.body.appendChild(geist);
    box.classList.add("zieht");
    const marke = el(`<div class="proc-marke"></div>`);
    zug = { idx, rest, geist, marke, ziel: null };
    ev.target.setPointerCapture(ev.pointerId);
    ev.target.addEventListener("pointermove", zieheBewegen);
    ev.target.addEventListener("pointerup", zieheEnde);
    ev.target.addEventListener("pointercancel", zieheEnde);
    zieheBewegen(ev);
  }

  function zieheBewegen(ev) {
    if (!zug) return;
    zug.geist.style.left = "16px";
    zug.geist.style.top = ev.clientY + "px";

    // Nahe am Rand mitscrollen, damit auch lange Prozesse erreichbar sind.
    const cr = canvas.getBoundingClientRect();
    if (ev.clientY < cr.top + 60) canvas.scrollTop -= 12;
    else if (ev.clientY > cr.bottom - 60) canvas.scrollTop += 12;

    // Einfügestelle = vor der ersten Box, deren Mitte unter dem Finger liegt.
    const boxen = [...canvas.querySelectorAll(".proc-box:not(.zieht)")];
    let ziel = boxen.length;
    for (let i = 0; i < boxen.length; i++) {
      const r = boxen[i].getBoundingClientRect();
      if (ev.clientY < r.top + r.height / 2) { ziel = i; break; }
    }
    if (ziel === zug.ziel) return;
    zug.ziel = ziel;
    if (ziel < boxen.length) canvas.insertBefore(zug.marke, boxen[ziel]);
    else canvas.appendChild(zug.marke);
  }

  function zieheEnde(ev) {
    if (!zug) return;
    const { idx, rest, ziel } = zug;
    try { ev.target.releasePointerCapture(ev.pointerId); } catch (e) {}
    ev.target.removeEventListener("pointermove", zieheBewegen);
    ev.target.removeEventListener("pointerup", zieheEnde);
    ev.target.removeEventListener("pointercancel", zieheEnde);
    zug.geist.remove();
    zug.marke.remove();
    zug = null;
    if (ziel != null) {
      const bewegt = entwurf[idx];
      rest.splice(ziel, 0, bewegt);
      entwurf = rest;
      schmutzig = true;
    }
    zeichnen();
  }

  // --- Hinzufügen / Speichern / Schließen ---
  const hinzu = () => {
    const feld = ov.querySelector("#pz-neu");
    const name = feld.value.trim();
    if (!name) { feld.focus(); return; }
    entwurf.push({ id: "neu-" + Date.now(), typ: "eigen", label: name, kern_status: null });
    feld.value = "";
    schmutzig = true;
    zeichnen();
    canvas.scrollTop = canvas.scrollHeight;
  };
  ov.querySelector("#pz-add").addEventListener("click", hinzu);
  ov.querySelector("#pz-neu").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); hinzu(); }
  });

  const schliessen = () => {
    ov.remove();
    if (zurueck) navigate(zurueck, { mode: "none" });
  };
  ov.querySelector("#pz-zu").addEventListener("click", () => {
    if (schmutzig && !confirm("Änderungen verwerfen?")) return;
    schliessen();
  });
  ov.querySelector("#pz-save").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    const r = await api("/app/api/auftragsprozess",
      { method: "POST", body: JSON.stringify({ schritte: entwurf }) });
    if (!r) return;
    let j = null;
    try { j = await r.json(); } catch (err) {}
    if (j && j.ok) { schmutzig = false; schliessen(); return; }
    btn.disabled = false;
    alert((j && j.error) || "Speichern fehlgeschlagen.");
  });

  zeichnen();
}

// ---------- Auftrags-Detailansicht ----------
// Ein Auftrag mit allem, was zu ihm gehört, plus die Fortschrittszeile
// oben: jeder Schritt des Prozesses als Punkt, erledigte abgehakt.
// Jeder Punkt ist antippbar und zeigt darunter seine Infos.

function stepperHtml(schritte) {
  return `<div class="stepper">` + schritte.map((s, i) => {
    const zeichen = s.zustand === "erledigt" ? "✓" : String(i + 1);
    const cls = ["step", s.zustand, s.typ === "eigen" ? "eigen" : ""].join(" ");
    return `<button class="${cls}" data-step="${esc(s.id)}">
      <span class="step-dot">${zeichen}</span>
      <span class="step-label">${esc(s.label)}</span>
    </button>`;
  }).join("") + `</div><div class="step-info" id="step-info"></div>`;
}

const _ZUSTAND_TEXT = {
  erledigt: "✅ Erledigt", aktiv: "🔵 Läuft gerade", offen: "○ Steht noch aus",
};

// Wer wie lange an diesem Auftrag gearbeitet hat — Summe je Mitarbeiter,
// darunter die einzelnen Buchungen. Korrigiert wird durch Löschen und neu
// buchen; das ✕ steht nur an den eigenen Buchungen (der Inhaber sieht es
// überall, er muss den Nachweis geradeziehen können).
function stundenKarteHtml(d) {
  const st = d.stunden || {};
  const je = st.je_mitarbeiter || [];
  const eintraege = st.eintraege || [];
  const buchbar = !!(d.in_arbeit || d.fertig);
  if (!je.length && !buchbar) return "";
  const meineId = (App.me && App.me.employee && App.me.employee.id) || "";
  const isInhaber = can("auftraege.fuehren");

  const summen = je.length
    ? je.map((x) => `<div class="row"><span>${esc(x.name)}</span><span class="sub">${esc(x.text)}</span></div>`).join("")
      + `<div class="row"><span><b>Gesamt</b></span><span class="sub"><b>${esc(st.gesamt_text || "")}</b></span></div>`
    : emptyRow("Noch keine Stunden gebucht");

  const liste = eintraege.length
    ? `<div style="margin-top:12px">` + eintraege.map((e) => {
        const darf = isInhaber || (e.employee_id && e.employee_id === meineId);
        return `<div class="row"><div style="min-width:0">
            <div>${esc(e.datum)} · ${esc(e.name)}</div>
            ${e.notiz ? `<div class="sub">${esc(e.notiz)}</div>` : ""}
          </div>
          <div style="display:flex;gap:8px;align-items:center;flex-shrink:0">
            <span class="sub">${esc(e.text)}</span>
            ${darf ? `<button class="btn-sm btn-ghost" data-std-del="${esc(e.id)}"
                        aria-label="Buchung löschen" style="padding:4px 8px">✕</button>` : ""}
          </div></div>`;
      }).join("") + `</div>`
    : "";

  // Abgleich gebuchte vs. angebotene Stunden — nur ein Hinweis. Warnt vor dem
  // klassischen Geldverlust (mehr gearbeitet als abgerechnet). Q entscheidet
  // nichts; der Betrieb sieht die Zahlen und rechnet ggf. nach.
  const ab = d.stunden_abgleich;
  const abgleich = (ab && ab.hinweis)
    ? `<div class="banner" style="margin-top:12px;${ab.mehr ? "background:#fff4e0;border-color:#f0c674" : ""}">
         ⏱️ ${esc(ab.hinweis)}
         <div class="sub" style="margin-top:4px">Angebot: ${esc(ab.angeboten)} · Gebucht: ${esc(ab.gebucht)}</div>
       </div>`
    : "";

  return `<div class="card" id="stunden-karte"><h2>Arbeitsstunden</h2>${summen}${abgleich}${
    buchbar ? stundenFeld(d, true) : ""}${liste}</div>`;
}

async function showAuftragDetail(id, zurueck) {
  App.view.innerHTML = `<div class="loading">Lädt …</div>`;
  const res = await api("/app/api/auftraege/" + encodeURIComponent(id) + "/detail");
  if (!res || !res.ok) {
    App.view.innerHTML = `<div class="card"><p class="empty">Konnte den Auftrag nicht laden.</p></div>`;
    return;
  }
  const d = await res.json();
  const isInhaber = can("auftraege.fuehren");
  // Q darf mitreden können, worüber der Nutzer gerade schaut.
  App.screenContext = { screen: "auftrag_detail", kunde: d.kunde || "" };

  const zeile = (label, wert) => wert
    ? `<div class="row"><span>${esc(label)}</span><span class="sub">${esc(wert)}</span></div>` : "";
  const positionen = (d.positionen || []).length
    ? `<div class="card"><h2>Positionen</h2>` + d.positionen.map((p) =>
        `<div class="row"><div><div>${esc(p.name)}</div>${
          p.beschreibung ? `<div class="sub">${esc(p.beschreibung)}</div>` : ""
        }<div class="sub">${esc(p.menge)}</div></div><span class="sub">${esc(p.preis)}</span></div>`
      ).join("") + `</div>`
    : "";
  const archiv = d.archiv_url
    ? `<div class="card"><a href="${esc(d.archiv_url)}" target="_blank" rel="noopener">📁 Auftragsordner im Drive öffnen</a></div>`
    : "";

  let aktionen = "";
  if (isInhaber && !d.abgebrochen && d.status !== "rechnung_gesendet") {
    const next = AUFTRAG_NEXT[d.status];
    const btns = [];
    if (next) btns.push(`<button class="btn-sm" data-auftrag="${esc(d.id)}" data-status="${next.status}">${next.label}</button>`);
    else if (d.status === "arbeit_fertig") btns.push(`<button class="btn-sm" data-rechnung="${esc(d.id)}">🧾 Rechnung stellen</button>`);
    btns.push(`<button class="btn-sm btn-ghost" data-auftrag="${esc(d.id)}" data-status="abgebrochen">Abbrechen</button>`);
    aktionen = `<div class="card"><h2>Nächster Schritt</h2><div style="display:flex;gap:8px;flex-wrap:wrap">${btns.join("")}</div></div>`;
  }

  App.view.innerHTML =
    `<button class="btn-sm btn-ghost" id="back-auftrag" style="margin-bottom:10px">← Zurück</button>` +
    `<h1 style="font-size:22px;margin:4px 4px 2px">${esc(d.kunde)}</h1>` +
    `<p class="muted" style="margin:0 4px 14px">${esc(d.status_label)}${d.betrag ? " · " + esc(d.betrag) : ""}</p>` +
    `<div class="card"><h2>Fortschritt</h2>${stepperHtml(d.schritte || [])}
       ${fortschrittsRegler(d, true)}</div>` +
    `<div class="card"><h2>Auftrag</h2>` +
      zeile("Kunde", d.kunde) + zeile("Anschrift", d.adresse) + zeile("E-Mail", d.email) +
      zeile("Betrag (brutto)", d.betrag) + zeile("Angebotsnummer", d.angebot_nr) +
      zeile("Angelegt", d.zeit) + zeile("Angebot versendet", d.angebot_versendet) +
      zeile("Angenommen", d.angenommen_am) + zeile("Abgeschlossen", d.abgeschlossen_am) +
    `</div>` +
    stundenKarteHtml(d) + positionen + archiv + aktionen;

  document.getElementById("back-auftrag").addEventListener("click",
    () => navigate(zurueck || "auftraege_page"));
  bindFortschrittsRegler(() => openRechnungInQ(d.id));
  // Nach einer Buchung die Karte neu zeichnen — Aufschlüsselung UND
  // Einzelbuchungen ändern sich, dafür reicht die Zeile unter dem Feld nicht.
  bindStundenBuchung((uebersicht) => {
    d.stunden = uebersicht;
    _stundenKarteNeu(d, id, zurueck);
  });
  _bindStundenLoeschen(d, id, zurueck);
  if (isInhaber) bindAuftragActions(() => showAuftragDetail(id, zurueck));
  bindStepper(d, id, zurueck);
}

// Zeichnet nur die Stunden-Karte neu, statt den ganzen Screen zu laden —
// der Handwerker verliert dabei weder Scroll-Position noch offenen Schritt.
function _stundenKarteNeu(d, id, zurueck) {
  const alt = document.getElementById("stunden-karte");
  if (!alt) return;
  const html = stundenKarteHtml(d);
  if (!html) { alt.remove(); return; }
  const huelle = document.createElement("div");
  huelle.innerHTML = html;
  const neu = huelle.firstElementChild;
  alt.replaceWith(neu);
  // Frisches DOM = frische Listener. Die alten sind mit der alten Karte weg.
  bindStundenBuchung((uebersicht) => {
    d.stunden = uebersicht;
    _stundenKarteNeu(d, id, zurueck);
  });
  _bindStundenLoeschen(d, id, zurueck);
}

function _bindStundenLoeschen(d, id, zurueck) {
  document.querySelectorAll("[data-std-del]").forEach((btn) =>
    btn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      if (!confirm("Diese Stundenbuchung löschen?")) return;
      btn.disabled = true;
      const res = await api(`/app/api/auftraege/${encodeURIComponent(id)}/stunden/${
        encodeURIComponent(btn.dataset.stdDel)}/loeschen`, { method: "POST", body: "{}" });
      if (!res) return;
      let j = null;
      try { j = await res.json(); } catch (e) {}
      btn.disabled = false;
      if (!j || !j.ok) { alert((j && j.error) || "Löschen fehlgeschlagen."); return; }
      d.stunden = j.stunden;
      _stundenKarteNeu(d, id, zurueck);
    }));
}

// Antippen eines Schritts blendet darunter seine Infos ein. Eigene
// Schritte lassen sich dort auch ab-/anhaken — Kern-Schritte nicht, die
// hängen am Auftrags-Status (und am Rechnungsversand).
function bindStepper(d, id, zurueck) {
  const info = document.getElementById("step-info");
  if (!info) return;
  const schritte = d.schritte || [];

  const zeigen = (sid) => {
    document.querySelectorAll(".step").forEach((b) =>
      b.classList.toggle("sel", b.dataset.step === sid));
    const s = schritte.find((x) => x.id === sid);
    if (!s) return;
    const wann = s.erledigt_am
      ? ` · ${new Date(s.erledigt_am).toLocaleDateString("de-DE")}` : "";
    const knopf = s.typ === "eigen"
      ? `<button class="btn-sm ${s.zustand === "erledigt" ? "btn-ghost" : ""}"
           id="step-toggle" style="margin-top:10px">${
             s.zustand === "erledigt" ? "Haken entfernen" : "✓ Erledigt"
           }</button>`
      : `<div class="sub" style="margin-top:6px">Fester Schritt — ändert sich mit dem Auftrags-Status.</div>`;
    info.innerHTML =
      `<div><b>${esc(s.label)}</b></div>` +
      `<div class="sub">${esc(_ZUSTAND_TEXT[s.zustand] || s.zustand)}${esc(wann)}</div>` +
      knopf;
    const t = document.getElementById("step-toggle");
    if (t) t.addEventListener("click", async () => {
      t.disabled = true;
      const r = await api("/app/api/auftraege/" + encodeURIComponent(id) + "/schritt",
        { method: "POST", body: JSON.stringify({
          schritt_id: s.id, erledigt: s.zustand !== "erledigt" }) });
      if (r && r.ok) { showAuftragDetail(id, zurueck); return; }
      t.disabled = false;
      alert("Konnte den Schritt nicht setzen.");
    });
  };

  document.querySelectorAll(".step").forEach((b) =>
    b.addEventListener("click", () => zeigen(b.dataset.step)));
  // Startbelegung: der Schritt, an dem der Auftrag gerade steht.
  const start = schritte.find((s) => s.zustand === "aktiv")
    || schritte.find((s) => s.zustand === "offen") || schritte[0];
  if (start) {
    zeigen(start.id);
    const btn = document.querySelector(`.step[data-step="${CSS.escape(start.id)}"]`);
    if (btn) btn.scrollIntoView({ block: "nearest", inline: "center" });
  }
}

// ---------- Belege (Foto/PDF → Lexware-Voucher) ----------
const _BELEG_STATUS = {
  uploaded: ["In Lexware", "ok"],
  uploading: ["Wird hochgeladen", "warn"],
  pending: ["Wartet", "warn"],
  error: ["Fehler", "danger"],
};
const BELEG_ALLOWED = ["image/jpeg", "image/png", "application/pdf"];
const BELEG_MAX_BYTES = 10 * 1024 * 1024;

function belegRow(b) {
  const meta = _BELEG_STATUS[b.status] || [b.status, ""];
  const title = b.caption || "Beleg";
  const sub = `${b.zeit} · ${b.groesse_kb} KB` + (b.fehler ? " · " + b.fehler : "");
  if (b.lexware_link) {
    return `<a class="row" href="${esc(b.lexware_link)}" target="_blank" rel="noopener" style="text-decoration:none;color:inherit">` +
      `<div><div>${esc(title)}</div><div class="sub">${esc(sub)}</div></div>` +
      `<span class="pill ${meta[1]}">${esc(meta[0])} ›</span></a>`;
  }
  return `<div class="row"><div><div>${esc(title)}</div><div class="sub">${esc(sub)}</div></div>` +
    `<span class="pill ${meta[1]}">${esc(meta[0])}</span></div>`;
}

async function showBelegUpload() {
  const inputStyle = "width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px";
  App.view.innerHTML =
    `<button class="btn-sm btn-ghost" id="back-buchhaltung" style="margin-bottom:10px">← Zurück</button>` +
    `<h1 style="font-size:22px;margin:4px 4px 6px">Beleg erfassen</h1>` +
    `<p class="muted" style="margin:0 4px 14px">Foto einer Quittung/Rechnung machen oder ein PDF wählen. Der Beleg landet unverbucht in Lexware — dort prüfst und buchst du ihn.</p>` +
    `<div class="card">
       <label class="sub">Beleg (JPEG, PNG oder PDF, max 10 MB)</label>
       <input type="file" id="bl-file" accept="image/jpeg,image/png,application/pdf" capture="environment" style="${inputStyle}" />
       <label class="sub">Notiz (optional)</label>
       <input type="text" id="bl-caption" placeholder="z.B. Bauhaus Schrauben" style="${inputStyle}" />
       <button class="btn-sm" id="bl-upload" style="width:100%;margin-top:8px" disabled>Beleg hochladen</button>
       <p class="muted" id="bl-status" style="margin-top:12px;min-height:20px"></p>
     </div>
     <div id="bl-result"></div>`;
  document.getElementById("back-buchhaltung").addEventListener("click", () => navigate("buchhaltung"));

  const fileEl = document.getElementById("bl-file");
  const upBtn = document.getElementById("bl-upload");
  const statusEl = document.getElementById("bl-status");
  const resultEl = document.getElementById("bl-result");

  fileEl.addEventListener("change", () => {
    const f = fileEl.files && fileEl.files[0];
    if (!f) { upBtn.disabled = true; statusEl.textContent = ""; return; }
    if (BELEG_ALLOWED.indexOf(f.type) === -1) {
      statusEl.textContent = "Nicht unterstützt: bitte JPEG, PNG oder PDF (kein HEIC).";
      upBtn.disabled = true; return;
    }
    if (f.size > BELEG_MAX_BYTES) {
      statusEl.textContent = `Datei zu groß (${Math.round(f.size / 1024 / 1024)} MB, max 10 MB).`;
      upBtn.disabled = true; return;
    }
    statusEl.textContent = `${f.name} · ${Math.round(f.size / 1024)} KB`;
    upBtn.disabled = false;
  });

  upBtn.addEventListener("click", async () => {
    const f = fileEl.files && fileEl.files[0];
    if (!f) return;
    const caption = document.getElementById("bl-caption").value.trim();
    upBtn.disabled = true;
    statusEl.textContent = "Lade an Lexware hoch …";
    let res;
    try {
      const qs = "?caption=" + encodeURIComponent(caption) + "&filename=" + encodeURIComponent(f.name);
      res = await fetch("/app/api/belege/upload" + qs, {
        method: "POST",
        headers: { "X-CSRF-Token": App.me.csrf, "Content-Type": f.type },
        body: f,
      });
    } catch (e) {
      statusEl.textContent = "Netzwerkfehler. Bitte erneut versuchen.";
      upBtn.disabled = false; return;
    }
    if (res.status === 303 || res.status === 401 || res.redirected) { location.href = "/app/login"; return; }
    let j = null;
    try { j = await res.json(); } catch (e) {}
    if (res.ok && j && j.ok) {
      statusEl.textContent = "";
      const dup = j.duplikat ? `<p class="muted" style="margin:6px 0 0">Dieser Beleg war schon in Lexware.</p>` : "";
      const link = j.lexware_link
        ? `<a class="btn-sm btn-ghost" href="${esc(j.lexware_link)}" target="_blank" rel="noopener" style="display:block;text-align:center;width:100%;margin-top:4px;text-decoration:none">In Lexware öffnen</a>` : "";
      resultEl.innerHTML =
        `<div class="card"><h2>✓ Beleg übergeben</h2>${dup}</div>` +
        `<div id="bl-kontierung"></div>` + link +
        `<button class="btn-sm btn-ghost" id="bl-again" style="width:100%;margin-top:8px">Nächsten Beleg</button>`;
      document.getElementById("bl-again").addEventListener("click", showBelegUpload);
      // Der Beleg liegt jetzt in Lexware. Ab hier ist alles Zugabe: Gemini
      // liest ihn und schlaegt die Buchung vor. Geht das schief, bleibt es
      // bei dem, was vorher auch passiert waere.
      if (j.id && !j.duplikat) zeigeKontierung(j.id);
    } else {
      statusEl.textContent = (j && j.error) || "Upload fehlgeschlagen. Bitte erneut versuchen.";
      upBtn.disabled = false;
    }
  });
}

async function showNewRueckrufForm() {
  App.view.innerHTML =
    `<button class="btn-sm btn-ghost" id="back-anrufe" style="margin-bottom:10px">← Zurück</button>` +
    `<h1 style="font-size:22px;margin:4px 4px 14px">Rückruf anlegen</h1>` +
    `<div class="card">
       <label class="sub">Kundenname *</label>
       <input type="text" id="rr-name" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
       <label class="sub">Telefon *</label>
       <input type="tel" id="rr-tel" placeholder="+49 …" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
       <label class="sub">E-Mail (optional)</label>
       <input type="email" id="rr-mail" placeholder="kunde@…" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
       <label class="sub">Anliegen</label>
       <textarea id="rr-anliegen" rows="4" placeholder="Worum geht's? z.B. Termin verschieben, Angebot besprechen …" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;font-family:inherit;font-size:16px"></textarea>
       <button class="btn-sm" id="rr-save" style="margin-top:12px;width:100%">Rückruf anlegen</button>
    </div>`;
  document.getElementById("back-anrufe").addEventListener("click", () => navigate("aktuelles"));
  document.getElementById("rr-save").addEventListener("click", async () => {
    const name = document.getElementById("rr-name").value.trim();
    const tel = document.getElementById("rr-tel").value.trim();
    if (!name || !tel) { alert("Name und Telefon sind Pflicht."); return; }
    const body = {
      kunde_name: name, kunde_telefon: tel,
      anliegen: document.getElementById("rr-anliegen").value.trim() || null,
      kunde_email: document.getElementById("rr-mail").value.trim() || null,
    };
    const btn = document.getElementById("rr-save");
    btn.disabled = true; btn.textContent = "Speichere …";
    const res = await api("/app/api/rueckrufe/anlegen",
      { method: "POST", body: JSON.stringify(body) });
    if (res && res.ok) {
      const j = await res.json();
      if (j.ok) { navigate("aktuelles"); return; }
      alert("Konnte nicht anlegen: " + (j.error || "unbekannt"));
    } else {
      alert("Konnte nicht anlegen.");
    }
    btn.disabled = false; btn.textContent = "Rückruf anlegen";
  });
}

async function showNewTerminForm() {
  // Erst freie Slots holen (schnell, lokal vom Plugin) — als Vorschlaege.
  App.view.innerHTML = `<div class="loading">Slots werden gesucht …</div>`;
  const slotsRes = await api("/app/api/termine/freie-slots?days=7");
  const slotsJson = slotsRes && slotsRes.ok ? await slotsRes.json() : { slots: [] };
  const suggestions = (slotsJson.slots || []).slice(0, 8);

  const todayIso = new Date().toISOString().slice(0, 10);
  const suggHtml = suggestions.length
    ? `<div class="card"><h2>Vorschlaege</h2>` +
      suggestions.map((s) =>
        `<button class="row menu-item" data-suggest='${esc(JSON.stringify(s))}' style="text-align:left">
          <div>${esc(s.datum || "")} · ${esc(s.uhrzeit || "")}</div>
          <span class="sub">${esc(s.dauer || "60 Min")} ›</span>
        </button>`).join("") +
      `</div>`
    : `<div class="card"><p class="muted">Keine freien Slots in den naechsten 7 Tagen — bitte unten manuell eingeben.</p></div>`;

  App.view.innerHTML =
    `<button class="btn-sm btn-ghost" id="back-termine" style="margin-bottom:10px">← Zurück</button>` +
    `<h1 style="font-size:22px;margin:4px 4px 14px">Neuer Termin</h1>` +
    suggHtml +
    `<div class="card"><h2>Manuell</h2>
       <label class="sub">Datum</label>
       <input type="date" id="t-datum" min="${todayIso}" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
       <label class="sub">Uhrzeit</label>
       <input type="time" id="t-uhrzeit" value="09:00" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
       <label class="sub">Dauer (Minuten)</label>
       <input type="number" id="t-dauer" value="60" min="15" step="15" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
       <label class="sub">Kundenname *</label>
       <input type="text" id="t-name" placeholder="z.B. Max Müller" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
       <label class="sub">Telefon (optional)</label>
       <input type="tel" id="t-tel" placeholder="+49 …" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
       <label class="sub">E-Mail (optional)</label>
       <input type="email" id="t-mail" placeholder="kunde@…" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
       <label class="sub">Adresse (optional)</label>
       <input type="text" id="t-adresse" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
       <label class="sub">Anliegen</label>
       <textarea id="t-anliegen" rows="3" style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;font-family:inherit;font-size:16px"></textarea>
       <button class="btn-sm" id="t-save" style="margin-top:12px;width:100%">Termin anlegen</button>
    </div>`;

  document.getElementById("back-termine").addEventListener("click", () => navigate("termine"));

  // Vorschlag-Klick fuellt Datum/Uhrzeit vor
  document.querySelectorAll("[data-suggest]").forEach((b) =>
    b.addEventListener("click", () => {
      try {
        const s = JSON.parse(b.dataset.suggest);
        // datum kommt als "DD.MM.YYYY" -> in ISO "YYYY-MM-DD"
        const m = String(s.datum || "").match(/^(\d{2})\.(\d{2})\.(\d{4})$/);
        if (m) document.getElementById("t-datum").value = `${m[3]}-${m[2]}-${m[1]}`;
        if (s.uhrzeit) document.getElementById("t-uhrzeit").value = s.uhrzeit;
        document.getElementById("t-name").focus();
      } catch (e) { /* malformed suggestion — egal, User tippt eh selbst */ }
    }));

  document.getElementById("t-save").addEventListener("click", async () => {
    const datumIso = document.getElementById("t-datum").value;
    const uhrzeit = document.getElementById("t-uhrzeit").value;
    const name = document.getElementById("t-name").value.trim();
    if (!datumIso || !uhrzeit || !name) {
      alert("Datum, Uhrzeit und Name sind Pflicht."); return;
    }
    // ISO → DD.MM.YYYY damit das Kalender-Plugin parsen kann
    const [Y, Mo, D] = datumIso.split("-");
    const datum = `${D}.${Mo}.${Y}`;
    const body = {
      datum, uhrzeit, name,
      dauer_minuten: parseInt(document.getElementById("t-dauer").value || "60", 10),
      telefon: document.getElementById("t-tel").value.trim() || null,
      kunde_email: document.getElementById("t-mail").value.trim() || null,
      adresse: document.getElementById("t-adresse").value.trim() || null,
      anliegen: document.getElementById("t-anliegen").value.trim() || null,
    };
    const btn = document.getElementById("t-save");
    btn.disabled = true; btn.textContent = "Lege an …";
    const res = await api("/app/api/termine/anlegen",
      { method: "POST", body: JSON.stringify(body) });
    if (res && res.ok) {
      const j = await res.json();
      if (j.ok) {
        alert(`Termin angelegt: ${j.datum} · ${j.uhrzeit}`);
        navigate("termine"); return;
      }
      alert("Konnte nicht anlegen: " + (j.error || "unbekannt"));
    } else {
      alert("Konnte nicht anlegen.");
    }
    btn.disabled = false; btn.textContent = "Termin anlegen";
  });
}

async function showAnfrage(id) {
  App.view.innerHTML = `<div class="loading">Lädt …</div>`;
  const r = await api("/app/api/anfragen/" + encodeURIComponent(id));
  if (!r || !r.ok) {
    App.view.innerHTML = `<div class="card"><p class="empty">Konnte nicht laden.</p></div>`;
    return;
  }
  const d = await r.json();

  // Slots-Block: wenn Q dem Kunden schon Termine vorgeschlagen hat, hier
  // sichtbar machen. Der Inhaber sieht sofort: "Ah, der Bot hat schon
  // diese 3 Slots vorgeschlagen, ich brauch nur zu warten" oder kann
  // alternativ direkt antworten.
  const slots = (d.proposed_slots || []);
  const slotsHtml = slots.length
    ? `<div class="card"><h2>Bot hat vorgeschlagen</h2>${slots.map(
        (s) => `<div class="row"><div>${esc(s.datum || "")} ${esc(s.uhrzeit || "")}</div></div>`).join("")}</div>`
    : "";

  // Klassifikations-Begruendung als zusammenklappbarer Block — fuer den
  // Inhaber spannend wenn er der KI hinterher schauen will.
  const reasonHtml = d.classification_reason
    ? `<details class="card"><summary style="cursor:pointer;font-weight:600">KI-Einschätzung (${esc(d.classification_label || "")}${d.classification_confidence ? " · " + esc(d.classification_confidence) : ""})</summary>
       <div class="sub" style="margin-top:8px;white-space:pre-wrap">${esc(d.classification_reason)}</div></details>`
    : "";

  // Drive-Link: wenn der Kunde das Anfrage-Formular ausgefuellt hat, gibt's
  // einen Google-Drive-Ordner mit Fotos/Uploads. Direkt-Link spart ein
  // separates Plugin-Hopping.
  const driveHtml = d.drive_folder_url
    ? `<div class="card"><div class="row"><span>📂 Kunden-Uploads</span><a href="${esc(d.drive_folder_url)}" target="_blank" rel="noopener">Drive öffnen ›</a></div></div>`
    : "";

  // Letzte Q-Antwort einklappbar — Kontext wenn der Inhaber pruefen will
  // was der Bot zuletzt geschrieben hat, bevor er selbst antwortet.
  const qReplyHtml = d.last_q_reply
    ? `<details class="card"><summary style="cursor:pointer;font-weight:600">Letzte Bot-Antwort an Kunden</summary>
       <div style="margin-top:8px;white-space:pre-wrap">${esc(d.last_q_reply)}</div></details>`
    : "";

  const lastMsgBlock = d.last_user_message
    ? `<div class="card"><h2>Letzte Nachricht vom Kunden</h2>
       <div style="white-space:pre-wrap">${esc(d.last_user_message)}</div></div>`
    : `<div class="card"><p class="empty">Noch keine Kunden-Nachricht in dieser Konversation.</p></div>`;

  // Quick-Actions: Anrufen + Mail-Adresse copy. Telefon kommt aus dem
  // AnfrageToken (Voice-/Formular-Eingang); wenn null, zeigen wir nur Mail.
  const phone = (d.kunde_telefon || "").trim();
  const telLink = phone
    ? `<a class="btn-sm" href="tel:${esc(phone)}" style="padding:10px 14px;text-decoration:none;display:inline-flex;align-items:center;gap:6px">📞 ${esc(phone)}</a>`
    : "";
  const mailLink = `<a class="btn-sm btn-ghost" href="mailto:${esc(d.kunde_email)}" style="padding:10px 14px;text-decoration:none;display:inline-flex;align-items:center;gap:6px">✉️ Mail</a>`;
  const quickActions = `<div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">${telLink}${mailLink}</div>`;

  App.view.innerHTML =
    `<button class="btn-sm btn-ghost" id="back-anfragen" style="margin-bottom:10px">← Zurück</button>` +
    `<div class="card">
      <div class="row"><div><b>${esc(d.kunde_name || d.kunde_email)}</b><div class="sub">${esc(d.kunde_email)}</div></div>
      <span class="pill ${d.state_style || ""}">${esc(d.state_label)}</span></div>
      <div class="sub" style="margin-top:6px">${esc(d.subject || "")} · ${esc(d.updated_at_fmt || "")}</div>
      ${quickActions}
    </div>` +
    lastMsgBlock +
    qReplyHtml +
    slotsHtml +
    driveHtml +
    reasonHtml +
    (d.closed
      ? `<div class="card"><p class="muted">Diese Anfrage ist als erledigt markiert. Antworten ist nicht mehr möglich.</p></div>`
      : `<div class="card"><h2>Antworten</h2>
         <textarea id="reply-body" rows="6" placeholder="Schreibe deine Antwort an den Kunden …"
           style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;font-size:16px;font-family:inherit"></textarea>
         <label style="display:flex;align-items:center;gap:8px;margin-top:10px;font-size:14px">
           <input type="checkbox" id="reply-close"> Nach dem Senden als erledigt markieren
         </label>
         <button class="btn-sm" id="reply-send" style="margin-top:12px;width:100%">Antwort senden</button>
         <p class="muted" style="margin-top:8px;font-size:12px">Die Mail wird im Namen deines Mailpostfachs versendet — der Kunde sieht deinen Absender.</p>
       </div>`);

  document.getElementById("back-anfragen").addEventListener("click", () => navigate("aktuelles"));
  const sendBtn = document.getElementById("reply-send");
  if (sendBtn) {
    sendBtn.addEventListener("click", async () => {
      const body = document.getElementById("reply-body").value.trim();
      const close = document.getElementById("reply-close").checked;
      if (body.length < 2) { alert("Bitte einen Antwort-Text eingeben."); return; }
      sendBtn.disabled = true;
      sendBtn.textContent = "Sende …";
      const res = await api("/app/api/anfragen/" + encodeURIComponent(id) + "/reply",
        { method: "POST", body: JSON.stringify({ body, close }) });
      if (res && res.ok) {
        const okJson = await res.json();
        if (okJson.ok) {
          toast(close ? "Antwort gesendet · Anfrage erledigt" : "Antwort gesendet");
          navigate("aktuelles");
          return;
        }
        alert("Mail-Versand fehlgeschlagen: " + (okJson.error || "unbekannter Fehler"));
      } else {
        alert("Mail-Versand fehlgeschlagen.");
      }
      sendBtn.disabled = false;
      sendBtn.textContent = "Antwort senden";
    });
  }
}

function bindAufnahmen() {
  document.querySelectorAll("[data-aufnahme]").forEach((b) =>
    b.addEventListener("click", () => showAufnahme(b.dataset.aufnahme)));
}

// Event-Bindung für den "Aktuelles"-Screen: Beratungs-Leads annehmen/ablehnen,
// Fortschritts-Regler, Rechnung-in-Q, Angebot-erstellen.
function bindAktuelles() {
  // Beratungs-Lead annehmen/ablehnen
  document.querySelectorAll("[data-lead-ja]").forEach((b) =>
    b.addEventListener("click", () => beratungEntscheidung(b.dataset.leadJa, "annehmen")));
  document.querySelectorAll("[data-lead-nein]").forEach((b) =>
    b.addEventListener("click", () => beratungEntscheidung(b.dataset.leadNein, "ablehnen")));

  // Fortschritts-Regler + Stundenbuchung (gemeinsame Implementierung,
  // siehe auftragCard). Beide genau EINMAL pro Screen binden — zweimal
  // hiesse zwei Listener am selben Knopf und damit eine Doppelbuchung.
  bindFortschrittsRegler();
  bindStundenBuchung();

  // Fertiger Auftrag -> Rechnung in Q vorbereiten
  document.querySelectorAll("[data-rechnung]").forEach((b) =>
    b.addEventListener("click", (ev) => {
      ev.stopPropagation();
      openRechnungInQ(b.dataset.rechnung);
    }));

  // Angenommener Lead -> Angebot über Q erstellen
  document.querySelectorAll("[data-angebot-neu]").forEach((b) =>
    b.addEventListener("click", () => { App.qSeed = "Mach ein Angebot für " + b.dataset.angebotNeu + ": "; navigate("assistent"); }));
}

async function beratungEntscheidung(id, entscheidung) {
  let res;
  try {
    res = await fetch(`/app/api/beratung/${encodeURIComponent(id)}/entscheidung`, {
      method: "POST", headers: { "X-CSRF-Token": App.me.csrf, "Content-Type": "application/json" },
      body: JSON.stringify({ entscheidung }) });
  } catch (e) { alert("Aktion fehlgeschlagen."); return; }
  if (res.status === 303 || res.status === 401 || res.redirected) { location.href = "/app/login"; return; }
  navigate("aktuelles");
}

// Lädt die Rechnungs-Vorschau (Positionen + KI-Anschreiben) und leitet in den
// Q-Chat weiter, wo der Handwerker alles prüft/anpasst und dann sendet.
async function openRechnungInQ(angebotId) {
  App.qchat = App.qchat || [];
  let res, j = null;
  try {
    res = await fetch("/app/api/rechnung/vorbereiten?angebot_id=" + encodeURIComponent(angebotId), {
      headers: { "X-CSRF-Token": App.me.csrf } });
  } catch (e) { alert("Konnte die Rechnung nicht vorbereiten."); return; }
  if (res.status === 303 || res.status === 401 || res.redirected) { location.href = "/app/login"; return; }
  try { j = await res.json(); } catch (e) {}
  if (!j || !j.ok) { alert((j && j.error) || "Konnte die Rechnung nicht vorbereiten."); return; }
  App.qchat.push({ role: "q", text: `Der Auftrag von ${j.kunde} ist fertig. Hier die Rechnung — prüf das Anschreiben und schick sie ab, wenn alles passt.` });
  App.qchat.push({ role: "rechnung", data: j });
  navigate("assistent");
}

// ---------- Kundengespräch: der Arbeitsbereich vor Ort ----------
// Oben der Kunde mit allem, was der Handwerker im Gespräch wissen will;
// darunter die vier Werkzeuge (Diktat, Notiz, Foto, Visualisierung) und
// alles, was schon zusammengekommen ist. Ganz unten die Kundenmail.

// Geplante Gespräche: was im Kalender steht, bevor es das Gespräch gibt.
// Zwei Sorten in einer Liste — ein schon angelegtes Gespräch öffnet direkt
// seinen Arbeitsbereich, ein reiner Kalendertermin startet ein neues.
let _geplanteGespraeche = [];

async function _geplanteGespraecheLaden() {
  const r = await api("/app/api/gespraeche/geplant");
  const d = r && r.ok ? await r.json().catch(() => null) : null;
  // Nach dem await kann der Screen längst gewechselt sein.
  const box = document.getElementById("gespr-geplant");
  if (!box) return;
  _geplanteGespraeche = (d && d.geplant) || [];
  if (!_geplanteGespraeche.length) { box.remove(); return; }
  box.innerHTML = `<h2>Geplant</h2>` + _geplanteGespraeche.map((x, i) => {
    const titel = x.quelle === "kalender" ? (x.titel || "Termin") : (x.kunde || "Gespräch");
    const unter = [x.ort, x.quelle === "kalender" ? "aus dem Kalender" : "Gespräch läuft"]
      .filter(Boolean).join(" · ");
    return `<button class="row menu-item" data-geplant="${i}" style="align-items:flex-start">` +
      `<div style="text-align:left"><div>${esc(titel)}</div><div class="sub">${esc(unter)}</div></div>` +
      `<span class="sub">${esc(x.zeit || "")} ›</span></button>`;
  }).join("");
  box.querySelectorAll("[data-geplant]").forEach((b) =>
    b.addEventListener("click", () => _geplantesOeffnen(_geplanteGespraeche[+b.dataset.geplant])));
}

function _geplantesOeffnen(x) {
  if (!x) return;
  if (x.quelle === "gespraech" && x.id) { showGespraech(x.id); return; }
  App.gespraechVorgabe = {
    name: x.kunde || "", event_id: x.event_id || "",
    termin_iso: x.termin_iso || "", ort: x.ort || "", zeit: x.zeit || "",
  };
  navigate("gespraech_neu");
}

function _gespraechKopf(k) {
  const zeile = (label, wert) => wert
    ? `<div class="row"><span>${esc(label)}</span><span class="sub">${esc(wert)}</span></div>` : "";
  const offen = [];
  if (k.auftraege_laufend) offen.push(`${k.auftraege_laufend} laufende(r) Auftrag/Aufträge`);
  if (k.rechnungen_offen) offen.push(`${k.rechnungen_offen} offene Rechnung(en)`);
  if (k.gespraeche_frueher) offen.push(`${k.gespraeche_frueher} frühere(s) Gespräch(e)`);
  // Der Name ist die Überschrift des Screens, kein Abschnitts-Label: <h2>
  // wird in der Karte per CSS zu grauen Großbuchstaben ("KUNDE") — für
  // einen Eigennamen falsch.
  return `<div class="card">
    <div style="font-size:20px;font-weight:600;margin-bottom:8px">${esc(k.name || "Kunde")}</div>
    ${zeile("Adresse", k.adresse)}
    ${zeile("Telefon", k.telefon)}
    ${zeile("E-Mail", k.email)}
    ${offen.length ? `<div class="row"><span>Beim Kunden offen</span><span class="sub">${esc(offen.join(" · "))}</span></div>` : ""}
    ${k.drive_url ? `<a class="row" href="${esc(k.drive_url)}" target="_blank" rel="noopener" style="text-decoration:none;color:inherit"><span>Kundenordner</span><span class="sub">📁 Drive ›</span></a>` : ""}
    ${k.kunde_id ? `<button class="row menu-item" id="gs-profil"><span>Ganzes Kundenprofil</span><span class="sub">›</span></button>` : ""}
  </div>`;
}

// `meldung` wird nach dem Rendern in die Statuszeile geschrieben — so
// überlebt die Rückmeldung eines Abschlusses das Neuladen des Screens.
async function showGespraech(id, meldung) {
  App.view.innerHTML = `<div class="loading">Lädt …</div>`;
  const r = await api("/app/api/gespraeche/" + encodeURIComponent(id));
  if (!r || !r.ok) { App.view.innerHTML = `<div class="card"><p class="empty">Konnte das Gespräch nicht laden.</p></div>`; return; }
  const d = await r.json();
  const k = d.kunde || {};
  // Q soll mitreden können, ohne dass der Handwerker den Kunden nochmal nennt.
  App.screenContext = {
    screen: "kundengespraech",
    kunde: k.name || "",
    notizen: (d.notizen || "").slice(0, 2000),
    briefing: (d.briefing || "").slice(0, 500),
  };

  const bilder = d.bilder || [];
  const galerie = bilder.length
    ? `<div class="card"><h2>Bilder</h2>
         <p class="muted" style="margin:0 0 10px;font-size:13px">Angehakte Bilder gehen mit in die Kundenmail.</p>
         <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:8px">
           ${bilder.map((b) => `<label style="position:relative;display:block;cursor:pointer">
              <img src="${esc(b.url)}" alt="${esc(b.name)}" loading="lazy"
                   style="width:100%;aspect-ratio:1;object-fit:cover;border-radius:10px;border:1px solid var(--line)" />
              <input type="checkbox" data-bild="${esc(b.id)}" checked
                     style="position:absolute;top:6px;left:6px;width:20px;height:20px" />
              ${b.typ === "visualisierung" ? `<span class="pill ok" style="position:absolute;bottom:6px;right:6px">🎨</span>` : ""}
            </label>`).join("")}
         </div>
       </div>`
    : "";

  const todos = (d.todos || []).length
    ? `<div class="card"><h2>To-dos</h2>${d.todos.map((t) => `<div class="row"><div>☐ ${esc(t)}</div></div>`).join("")}</div>` : "";
  const termin = d.termin
    ? `<div class="card"><h2>Termin</h2><div class="row"><span>${esc(d.termin)}</span><span class="sub">${esc(d.termin_ort || "")}</span></div></div>` : "";

  // Das Ende des Arbeitsbereichs: einpflegen oder wegwerfen. Der Text sagt
  // beim Namen, was passiert — beim unbekannten Kunden wird er angelegt.
  const abschluss = d.abgeschlossen
    ? `<div class="card"><h2>Eingepflegt</h2>
         <div class="row"><span>Beim Kunden abgelegt</span><span class="sub">${esc(d.abgeschlossen_am || "")}</span></div>
         ${d.protokoll_url ? `<a class="row" href="${esc(d.protokoll_url)}" target="_blank" rel="noopener" style="text-decoration:none;color:inherit"><span>Gesprächsprotokoll</span><span class="sub">📄 Drive ›</span></a>` : ""}
       </div>`
    : `<div class="card"><h2>Gespräch abschließen</h2>
         <p class="muted" style="margin:0 0 10px;font-size:13px">${
           k.kunde_id
             ? "Protokoll, Notizen und Bilder landen im Kundenordner."
             : `„${esc(k.name || "Der Kunde")}" wird als Kunde angelegt; Protokoll, Notizen und Bilder landen in seinem Ordner.`
         }</p>
         <button class="btn-sm" id="gs-fertig" style="width:100%;padding:14px 10px">✅ ${
           k.kunde_id ? "Fertig — beim Kunden einpflegen" : "Fertig — Kunde anlegen & einpflegen"
         }</button>
         <button class="btn-sm btn-ghost" id="gs-verwerfen" style="width:100%;margin-top:8px">🗑 Gespräch verwerfen</button>
       </div>`;

  App.view.innerHTML =
    `<button class="btn-sm btn-ghost" id="gs-back" style="margin-bottom:10px">← Gespräche</button>` +
    _gespraechKopf(k) +
    `<div class="card">
       <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
         <button class="btn-sm btn-ghost" id="gs-diktat" style="padding:14px 8px">🎤 Diktat</button>
         <button class="btn-sm btn-ghost" id="gs-foto" style="padding:14px 8px">📷 Foto</button>
         <button class="btn-sm btn-ghost" id="gs-viz" style="padding:14px 8px">🎨 Visualisieren</button>
         <button class="btn-sm btn-ghost" id="gs-mail" style="padding:14px 8px">✉️ Kundenmail</button>
       </div>
       <div id="gs-rec" style="margin-top:10px"></div>
       <input type="file" id="gs-fotofile" accept="image/jpeg,image/png,image/webp" capture="environment" style="display:none" />
       <p class="muted" id="gs-status" style="margin:10px 0 0;min-height:18px"></p>
     </div>` +
    (d.briefing ? `<div class="card"><h2>Zusammenfassung</h2><div style="white-space:pre-wrap">${esc(d.briefing)}</div></div>` : "") +
    (d.notizen ? `<div class="card"><h2>Notizen aus dem Diktat</h2><div style="white-space:pre-wrap">${esc(d.notizen)}</div></div>` : "") +
    todos + termin + galerie +
    `<div class="card"><h2>Eigene Notiz</h2>
       <p class="muted" style="margin:0 0 8px;font-size:13px">Nur für dich — geht nie an den Kunden.</p>
       <textarea id="gs-notiz" rows="4" placeholder="z.B. Zufahrt eng, Material selbst mitbringen"
         style="width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;font-family:inherit;font-size:16px">${esc(d.handnotiz || "")}</textarea>
       <button class="btn-sm btn-ghost" id="gs-notiz-save" style="margin-top:8px;width:100%">Notiz speichern</button>
     </div>` +
    abschluss +
    (d.transkript ? `<div class="card"><h2>Transkript</h2><div class="sub" style="white-space:pre-wrap">${esc(d.transkript)}</div></div>` : "");

  document.getElementById("gs-back").addEventListener("click", () => navigate("gespraeche"));
  const profilBtn = document.getElementById("gs-profil");
  if (profilBtn) profilBtn.addEventListener("click", () => showKundenProfil(k.name, k.kunde_id));

  const statusEl = document.getElementById("gs-status");
  const gewaehlteBilder = () =>
    [...document.querySelectorAll("[data-bild]")].filter((c) => c.checked).map((c) => c.dataset.bild);

  // --- Notiz ---
  document.getElementById("gs-notiz-save").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    const res = await api("/app/api/gespraeche/" + encodeURIComponent(id) + "/notiz", {
      method: "POST", body: JSON.stringify({ text: document.getElementById("gs-notiz").value }) });
    btn.disabled = false;
    statusEl.textContent = (res && res.ok) ? "Notiz gespeichert." : "Notiz konnte nicht gespeichert werden.";
  });

  // --- Foto ---
  const fileEl = document.getElementById("gs-fotofile");
  document.getElementById("gs-foto").addEventListener("click", () => fileEl.click());
  fileEl.addEventListener("change", async () => {
    const f = fileEl.files && fileEl.files[0];
    if (!f) return;
    statusEl.textContent = "Lade Foto hoch …";
    let res;
    try {
      res = await fetch("/app/api/gespraeche/" + encodeURIComponent(id) + "/foto", {
        method: "POST", headers: { "X-CSRF-Token": App.me.csrf, "Content-Type": f.type }, body: f });
    } catch (err) { statusEl.textContent = "Netzwerkfehler beim Hochladen."; return; }
    if (res.status === 303 || res.status === 401 || res.redirected) { location.href = "/app/login"; return; }
    const j = await res.json().catch(() => null);
    fileEl.value = "";
    if (j && j.ok) { showGespraech(id); return; }
    statusEl.textContent = (j && j.error) || "Foto konnte nicht abgelegt werden.";
  });

  // --- Visualisierung: gerendert wird im Q-Chat, das Ergebnis kommt hierher ---
  document.getElementById("gs-viz").addEventListener("click", () => {
    App.vizGespraech = { id, kunde: k.name || "" };
    App.qchat = App.qchat || [];
    App.qchat.push({ role: "q", text: `Visualisierung für ${k.name || "den Kunden"}: häng unten ein Foto an (📎) und beschreib, was daraus werden soll. Das fertige Bild landet automatisch wieder im Gespräch.` });
    navigate("assistent");
  });

  // --- Kundenmail ---
  document.getElementById("gs-mail").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    statusEl.textContent = "Schreibe den Entwurf …";
    const res = await api("/app/api/gespraeche/" + encodeURIComponent(id) + "/mail", {
      method: "POST", body: JSON.stringify({ bild_ids: gewaehlteBilder() }) });
    const j = res ? await res.json().catch(() => null) : null;
    btn.disabled = false;
    if (j && j.ok && j.entwurf) {
      statusEl.textContent = "";
      App.qchat = App.qchat || [];
      App.qchat.push({ role: "q", text: `Entwurf für ${k.name || "den Kunden"} — prüf ihn und schick ihn ab. Transkript, To-dos und deine Notiz bleiben hier.` });
      App.qchat.push(mailDraftMsg(j.entwurf));
      navigate("assistent");
      return;
    }
    statusEl.textContent = (j && j.error) || "Konnte keinen Entwurf bauen.";
  });

  // --- Diktat direkt im Gespräch ---
  document.getElementById("gs-diktat").addEventListener("click", () => {
    const recEl = document.getElementById("gs-rec");
    if (Diktat.recording) { _gespraechDiktatStop(id, recEl, statusEl); return; }
    _gespraechDiktatStart(id, recEl, statusEl);
  });

  // --- Abschluss: einpflegen oder verwerfen ---
  const fertigBtn = document.getElementById("gs-fertig");
  if (fertigBtn) fertigBtn.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    statusEl.textContent = "Pflege das Gespräch beim Kunden ein …";
    const res = await api("/app/api/gespraeche/" + encodeURIComponent(id) + "/abschliessen", {
      method: "POST", body: "{}" });
    const j = res ? await res.json().catch(() => null) : null;
    btn.disabled = false;
    if (j && j.ok) {
      const teile = [j.kunde_neu
        ? `${j.kunde_name} als Kunde angelegt.`
        : "Beim Kunden eingepflegt."];
      if (j.hinweis) teile.push(j.hinweis);
      else if (j.protokoll_url) teile.push("Protokoll liegt im Kundenordner.");
      showGespraech(id, teile.join(" "));
      return;
    }
    statusEl.textContent = (j && j.error) || "Konnte das Gespräch nicht einpflegen.";
  });

  const verwerfenBtn = document.getElementById("gs-verwerfen");
  if (verwerfenBtn) verwerfenBtn.addEventListener("click", async (e) => {
    if (!confirm("Gespräch verwerfen? Es verschwindet aus allen Listen.")) return;
    const btn = e.currentTarget;
    btn.disabled = true;
    const res = await api("/app/api/gespraeche/" + encodeURIComponent(id) + "/verwerfen", {
      method: "POST", body: "{}" });
    const j = res ? await res.json().catch(() => null) : null;
    btn.disabled = false;
    if (j && j.ok) { navigate("gespraeche"); return; }
    statusEl.textContent = (j && j.error) || "Konnte das Gespräch nicht verwerfen.";
  });

  if (meldung) statusEl.textContent = meldung;
}

async function _gespraechDiktatStart(id, recEl, statusEl) {
  try {
    await _diktatStartRecording();
  } catch (e) {
    statusEl.textContent = (e && e.name === "NotAllowedError")
      ? "Mikrofon-Zugriff wurde abgelehnt. Bitte in den Browser-Einstellungen erlauben."
      : "Mikrofon nicht verfügbar.";
    _diktatTeardown();
    return;
  }
  recEl.innerHTML = `<div style="text-align:center">
      <div id="gs-timer" style="font-size:28px;font-variant-numeric:tabular-nums">0:00</div>
      <button class="btn-sm" id="gs-stop" style="margin-top:8px;width:100%">⏹ Stoppen & analysieren</button>
    </div>`;
  statusEl.textContent = "Aufnahme läuft …";
  const timerEl = document.getElementById("gs-timer");
  Diktat.tick = setInterval(() => {
    const s = Math.round((Date.now() - Diktat.startTs) / 1000);
    timerEl.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }, 500);
  Diktat.autostop = setTimeout(() => {
    if (Diktat.recording) _gespraechDiktatStop(id, recEl, statusEl);
  }, DIKTAT_MAX_SECONDS * 1000);
  document.getElementById("gs-stop").addEventListener("click",
    () => _gespraechDiktatStop(id, recEl, statusEl));
}

async function _gespraechDiktatStop(id, recEl, statusEl) {
  if (!Diktat.recording) return;
  const { blob, durationSec } = _diktatFinish();
  recEl.innerHTML = "";
  if (durationSec < 1 || blob.size < 2000) {
    statusEl.textContent = "Aufnahme war zu kurz. Bitte erneut versuchen.";
    return;
  }
  statusEl.textContent = "Analysiere das Gespräch … (kann 30–60 Sek dauern)";
  let res;
  try {
    res = await fetch("/app/api/gespraeche/" + encodeURIComponent(id) + "/diktat", {
      method: "POST",
      headers: {
        "X-CSRF-Token": App.me.csrf,
        "Content-Type": "audio/wav",
        "X-Audio-Duration": String(durationSec),
      },
      body: blob,
    });
  } catch (e) { statusEl.textContent = "Netzwerkfehler. Bitte erneut versuchen."; return; }
  if (res.status === 303 || res.status === 401 || res.redirected) { location.href = "/app/login"; return; }
  const j = await res.json().catch(() => null);
  if (j && j.ok) { showGespraech(id); return; }
  statusEl.textContent = (j && j.error) || "Konnte nicht verarbeiten. Bitte erneut versuchen.";
}

// Alter Einstiegspunkt (Listen-Zeilen, Q-Links) — führt in den neuen Bereich.
async function showAufnahme(id) { await showGespraech(id); }

function bindStorno() {
  document.querySelectorAll('[data-action="storno"]').forEach((b) =>
    b.addEventListener("click", async () => {
      if (!confirm("Diesen Termin wirklich stornieren? Der Kunde wird benachrichtigt.")) return;
      b.disabled = true;
      const res = await api("/app/api/termine/storno", { method: "POST", body: JSON.stringify({ id: b.dataset.id }) });
      if (res && res.ok) navigate("termine"); else { b.disabled = false; alert("Storno fehlgeschlagen."); }
    }));
}
function bindRueckrufDone() {
  const backTo = App.current === "rueckrufe_page" ? "rueckrufe_page" : "aktuelles";
  document.querySelectorAll('[data-action="rueckruf-done"]').forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      const res = await api("/app/api/rueckrufe/erledigt", { method: "POST", body: JSON.stringify({ id: b.dataset.id }) });
      if (res && res.ok) navigate(backTo); else { b.disabled = false; alert("Konnte nicht abhaken."); }
    }));
}

// ---------- Web-Push ----------
function urlBase64ToUint8Array(base64) {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

async function enablePush() {
  try {
    if (!notifSupported() || !("serviceWorker" in navigator) || !("PushManager" in window)) {
      alert("Dein Gerät unterstützt hier keine Push-Benachrichtigungen. Auf dem iPhone die App erst zum Home-Bildschirm hinzufügen."); return;
    }
    if (!App.me.vapid_public_key) { alert("Push ist serverseitig noch nicht konfiguriert."); return; }
    const perm = await Notification.requestPermission();
    if (perm !== "granted") return;
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(App.me.vapid_public_key),
    });
    await api("/app/api/push/subscribe", { method: "POST", body: JSON.stringify({ subscription: sub }) });
    navigate(App.current);
  } catch (e) { console.error(e); alert("Konnte Benachrichtigungen nicht aktivieren."); }
}

// ---------- Q-Sphere (Netzwerk-Globus wie auf der Website) ----------
// Wireframe-Ikosaeder + Partikelwolke + Energie-Bögen via Three.js (LOKAL unter
// /app/static/vendor gehostet — kein CDN). Im leeren Chat mittig; beim
// Tab-Wechsel/erster Nachricht gestoppt. Bei fehlendem/instabilem WebGL
// Fallback auf das statische SVG-Ring-Muster.
const _sphereMobile = window.innerWidth < 768;
const _sphereReducedMotion = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

let _sphereSupportCache = null;
function sphereSupported() {
  // Ergebnis cachen: jeder Aufruf legt sonst einen eigenen WebGL-Kontext an.
  if (_sphereSupportCache !== null) return _sphereSupportCache;
  try {
    const c = document.createElement("canvas");
    _sphereSupportCache = !!(c.getContext("webgl") || c.getContext("experimental-webgl"));
  } catch (e) { _sphereSupportCache = false; }
  return _sphereSupportCache;
}

function loadThree() {
  if (window.THREE) return Promise.resolve();
  if (App._threeP) return App._threeP;
  App._threeP = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "/app/static/vendor/three.min.js"; // lokal gehostet, kein CDN
    s.onload = () => resolve();
    s.onerror = reject;
    document.head.appendChild(s);
  });
  return App._threeP;
}

// Mountet eine kleine Kopie der Q-Sphere in den Thinking-Orb neben der Typing-Bubble.
async function _mountMiniSphere(orbId, canvasId, storeKey) {
  if (App[storeKey]) { try { App[storeKey](); } catch (e) {} App[storeKey] = null; }
  const orb = document.getElementById(orbId);
  const canvas = document.getElementById(canvasId);
  if (!orb || !canvas || !sphereSupported()) return;
  try {
    await loadThree();
    if (!document.body.contains(canvas)) return; // Typing-Bubble bereits weg
    const wasWorking = App.qWorking;
    App.qWorking = true;                          // Sphere dreht sich immer schnell während Denken
    App[storeKey] = buildQSphere(orb, canvas);
    App.qWorking = wasWorking;
    App.qSphereActive = null;                     // Mini-Sphere läuft autark, kein externer Stop
  } catch (e) {}
}

// Mountet die Sphere als Tab-Icon (läuft immer langsam im Hintergrund).
async function _mountTabSphere() {
  if (App.qTabSphereStop) { try { App.qTabSphereStop(); } catch (e) {} App.qTabSphereStop = null; }
  const canvas = document.getElementById("q-tab-sphere-canvas");
  if (!canvas || !sphereSupported()) return;
  const ico = canvas.parentElement; // .ico span
  try {
    await loadThree();
    if (!document.body.contains(canvas)) return;
    const prevWorking = App.qWorking;
    App.qWorking = false; // Tab-Sphere dreht sich immer langsam/ruhig
    App.qTabSphereStop = buildQSphere(ico, canvas);
    App.qWorking = prevWorking;
    // App.qSphereActive bleibt für die Haupt-Sphere reserviert
  } catch (e) {}
}

// Baut die Sphere in wrap/canvas und gibt eine stop()-Funktion zum Aufräumen zurück.
function buildQSphere(wrap, canvas) {
  const THREE = window.THREE;
  const isMobile = _sphereMobile;
  let W = wrap.clientWidth || 280, H = wrap.clientHeight || 280;

  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: !isMobile });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(W, H, false);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 100);
  camera.position.z = 4.6;

  const detail = isMobile ? 2 : 3;
  const sphereGeom = new THREE.IcosahedronGeometry(1.55, detail);
  const wireGeom = new THREE.WireframeGeometry(sphereGeom);
  const lineMat = new THREE.LineBasicMaterial({ color: 0x0066cc, transparent: true, opacity: 0.55 });
  const wireSphere = new THREE.LineSegments(wireGeom, lineMat);
  scene.add(wireSphere);

  const particleCount = isMobile ? 80 : 240;
  const positions = new Float32Array(particleCount * 3);
  const velocities = new Float32Array(particleCount * 3);
  for (let i = 0; i < particleCount; i++) {
    const r = Math.cbrt(Math.random()) * 1.4;
    const theta = Math.random() * Math.PI * 2;
    const phi = Math.acos(2 * Math.random() - 1);
    positions[i*3]   = r * Math.sin(phi) * Math.cos(theta);
    positions[i*3+1] = r * Math.sin(phi) * Math.sin(theta);
    positions[i*3+2] = r * Math.cos(phi);
    velocities[i*3]   = (Math.random() - 0.5) * 0.0035;
    velocities[i*3+1] = (Math.random() - 0.5) * 0.0035;
    velocities[i*3+2] = (Math.random() - 0.5) * 0.0035;
  }
  const particleGeom = new THREE.BufferGeometry();
  particleGeom.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  const particleMat = new THREE.PointsMaterial({
    color: 0x9ec5ff, size: 0.025, transparent: true, opacity: 0.9,
    sizeAttenuation: true, depthWrite: false });
  const particles = new THREE.Points(particleGeom, particleMat);
  scene.add(particles);

  const energyLines = [];
  const energyCount = isMobile ? 5 : 7;
  for (let i = 0; i < energyCount; i++) {
    const curveR = 1.55, arcAngle = Math.random() * Math.PI * 2, points = [], segs = 60;
    for (let j = 0; j <= segs; j++) {
      const t = j / segs, a = (t - 0.5) * Math.PI;
      points.push(new THREE.Vector3(
        curveR * Math.sin(a) * Math.cos(arcAngle + t * 0.4),
        curveR * Math.cos(a),
        curveR * Math.sin(a) * Math.sin(arcAngle + t * 0.4)));
    }
    const g = new THREE.BufferGeometry().setFromPoints(points);
    const m = new THREE.LineBasicMaterial({ color: 0x3b82f6, transparent: true, opacity: 0.35 });
    const line = new THREE.Line(g, m);
    line.userData.speed = 0.0015 + Math.random() * 0.003;
    line.userData.axis = ["x","y","z"][Math.floor(Math.random()*3)];
    scene.add(line); energyLines.push(line);
  }

  scene.add(new THREE.AmbientLight(0xffffff, 0.5));
  const dirLight = new THREE.DirectionalLight(0xffffff, 0.8);
  dirLight.position.set(2, 3, 4); scene.add(dirLight);
  const corePoint = new THREE.PointLight(0x3b82f6, 0.6, 4); scene.add(corePoint);

  const totalEdges = wireGeom.attributes.position.count / 2;
  let buildComplete = false;
  wireGeom.setDrawRange(0, 0);

  let targetRotX = 0, targetRotY = 0, pulseScale = 1, pulseTarget = 1;
  const onPointer = (e) => {
    const rect = wrap.getBoundingClientRect();
    const isTouch = e.type.startsWith("touch");
    const cx = isTouch ? e.touches[0].clientX : e.clientX;
    const cy = isTouch ? e.touches[0].clientY : e.clientY;
    targetRotX = (((cy - rect.top) / rect.height) * 2 - 1) * 0.18;
    targetRotY = (((cx - rect.left) / rect.width) * 2 - 1) * 0.18;
  };
  const onLeave = () => { targetRotX = 0; targetRotY = 0; };
  const onTap = () => { pulseTarget = 1.15; setTimeout(() => { pulseTarget = 1; }, 140); };
  wrap.addEventListener("mousemove", onPointer);
  wrap.addEventListener("touchmove", onPointer, { passive: true });
  wrap.addEventListener("mouseleave", onLeave);
  wrap.addEventListener("click", onTap);

  const onResize = () => {
    W = wrap.clientWidth; H = wrap.clientHeight;
    renderer.setSize(W, H, false);
    camera.aspect = 1; camera.updateProjectionMatrix();
  };
  window.addEventListener("resize", onResize);

  let baseRotY = 0, baseRotX = 0, raf = 0, stopped = false, active = !!App.qWorking;
  // Robustheit: Verliert der Browser den WebGL-Kontext (Mobil, GPU-Speicher
  // knapp), sauber stoppen + auf das SVG-Muster zurückfallen statt pro Frame
  // zu werfen (kann den Tab abschießen).
  const fbEl = wrap.querySelector(".q-sphere-fallback");
  const showFallback = () => { try { canvas.style.display = "none"; if (fbEl) fbEl.style.display = "block"; } catch (e) {} };
  const onContextLost = (ev) => { ev.preventDefault(); stopped = true; if (raf) cancelAnimationFrame(raf); showFallback(); };
  canvas.addEventListener("webglcontextlost", onContextLost, false);
  // Während Q einen angetippten Flow bearbeitet, dreht/pulsiert die Sphere
  // energischer. Anfangszustand aus App.qWorking; Setter erlaubt Umschalten.
  App.qSphereActive = (on) => { active = !!on; };
  const t0 = performance.now(); let lastFrame = t0, renderedFrames = 0;
  function animate(now) {
    if (stopped) return;
    raf = requestAnimationFrame(animate);
    try {
    if (isMobile && now - lastFrame < 24) return;
    lastFrame = now;
    // Watchdog: rendert es viel zu langsam (Software-Rasterizer), nach 30
    // Frames die Rate prüfen und ggf. auf das SVG-Muster zurückfallen.
    if (++renderedFrames === 30) {
      const fps = 30 / ((now - t0) / 1000);
      if (fps < 12) {
        console.warn("Q-Sphere zu langsam (" + fps.toFixed(1) + " fps) — Fallback");
        stopped = true; if (raf) cancelAnimationFrame(raf); showFallback(); return;
      }
    }
    const dt = (now - t0) / 1000;
    if (!buildComplete) {
      const bt = Math.min(dt / 1.6, 1);
      wireGeom.setDrawRange(0, Math.floor(bt * totalEdges) * 2);
      if (bt >= 1) buildComplete = true;
    }
    if (!_sphereReducedMotion) { baseRotY += active ? 0.006 : 0.0014; baseRotX = Math.sin(dt * (active ? 1.1 : 0.45)) * 0.06; }
    wireSphere.rotation.y += (baseRotY + targetRotY - wireSphere.rotation.y) * 0.08;
    wireSphere.rotation.x += (baseRotX + targetRotX - wireSphere.rotation.x) * 0.08;
    particles.rotation.copy(wireSphere.rotation);
    const phasePeriod = active ? 1.4 : 3.5;
    const phase = _sphereReducedMotion ? 0 : (1 - Math.cos(dt * 2 * Math.PI / phasePeriod)) * 0.5;
    pulseScale += (pulseTarget * (1 + phase * (active ? 0.12 : 0.05)) - pulseScale) * 0.22;
    wireSphere.scale.setScalar(pulseScale);
    lineMat.opacity = 0.6 + phase * 0.4;
    corePoint.intensity = (active ? 0.7 : 0.45) + phase * (active ? 0.8 : 0.5);
    const pArr = particles.geometry.attributes.position.array;
    for (let i = 0; i < particleCount; i++) {
      pArr[i*3]   += velocities[i*3];
      pArr[i*3+1] += velocities[i*3+1];
      pArr[i*3+2] += velocities[i*3+2];
      const dx = pArr[i*3], dy = pArr[i*3+1], dz = pArr[i*3+2];
      if (Math.sqrt(dx*dx + dy*dy + dz*dz) > 1.45) {
        velocities[i*3] *= -1; velocities[i*3+1] *= -1; velocities[i*3+2] *= -1;
      }
      if (Math.random() < 0.005) {
        velocities[i*3]   = (Math.random() - 0.5) * 0.0035;
        velocities[i*3+1] = (Math.random() - 0.5) * 0.0035;
        velocities[i*3+2] = (Math.random() - 0.5) * 0.0035;
      }
    }
    particles.geometry.attributes.position.needsUpdate = true;
    energyLines.forEach((l) => { if (!_sphereReducedMotion) l.rotation[l.userData.axis] += l.userData.speed; });
    renderer.render(scene, camera);
    } catch (e) {
      console.warn("Q-Sphere Render-Fehler — Fallback", e);
      stopped = true; if (raf) cancelAnimationFrame(raf); showFallback();
    }
  }
  raf = requestAnimationFrame(animate);

  return function stop() {
    stopped = true;
    active = false;
    App.qSphereActive = null;
    if (raf) cancelAnimationFrame(raf);
    window.removeEventListener("resize", onResize);
    wrap.removeEventListener("mousemove", onPointer);
    wrap.removeEventListener("touchmove", onPointer);
    wrap.removeEventListener("mouseleave", onLeave);
    wrap.removeEventListener("click", onTap);
    canvas.removeEventListener("webglcontextlost", onContextLost);
    try {
      wireGeom.dispose(); sphereGeom.dispose(); particleGeom.dispose();
      energyLines.forEach((l) => l.geometry.dispose());
      renderer.dispose();
      // dispose() gibt nur GL-Ressourcen frei, NICHT den Kontext. Ohne
      // forceContextLoss sammeln sich tote Kontexte (Tab-Wechsel/Start) → ab
      // ~16 wirft Chrome den ältesten weg → Sphere wird „zerschossen".
      const gl = renderer.getContext && renderer.getContext();
      const lose = gl && gl.getExtension && gl.getExtension("WEBGL_lose_context");
      if (lose) lose.loseContext();
    } catch (e) {}
  };
}

// Hängt die Sphere an #q-sphere-wrap; Fallback auf das SVG-Ring-Muster bei
// fehlendem WebGL oder Three.js-Ladefehler.
function mountQSphere() {
  const wrap = document.getElementById("q-sphere-wrap");
  const canvas = document.getElementById("q-sphere-canvas");
  if (!wrap || !canvas) return;
  // Läuft schon eine Sphere auf genau diesem Canvas? Dann NICHT neu aufbauen
  // (jeder Neuaufbau frisst einen WebGL-Kontext).
  if (App.qSphereStop && App.qSphereCanvas === canvas && document.body.contains(canvas)) return;
  if (App.qSphereStop) { try { App.qSphereStop(); } catch (e) {} App.qSphereStop = null; App.qSphereCanvas = null; }
  const fb = wrap.querySelector(".q-sphere-fallback");
  const fallback = () => { canvas.style.display = "none"; if (fb) fb.style.display = "block"; };
  if (!sphereSupported()) { fallback(); return; }
  loadThree().then(() => {
    if (!document.body.contains(canvas)) return; // Tab inzwischen gewechselt
    try { App.qSphereStop = buildQSphere(wrap, canvas); App.qSphereCanvas = canvas; }
    catch (e) { console.warn("Q-Sphere fehlgeschlagen", e); fallback(); }
  }).catch(() => fallback());
}

// ---------- Q-Aktions-Ergebnistexte ----------
// Auf Modul-Ebene, weil Assistent-Screen UND Q-Overlay Aktionen ausführen
// und beide dieselben Bestätigungstexte zeigen sollen.

function _assistResultText(tool, r) {
  if (tool === "termin_anlegen") return `Termin für ${r.kunde} am ${r.datum} um ${r.uhrzeit} angelegt.`;
  if (tool === "termin_stornieren") return `Termin von ${r.kunde} storniert${r.mail_sent ? " (Kunde per Mail informiert)" : ""}.`;
  if (tool === "rueckruf_anlegen") return `Rückruf für ${r.kunde} angelegt.`;
  if (tool === "material_bestellen") return `${r.menge}× ${r.material} bestellt.`;
  if (tool === "abwesenheit_melden") return `${r.mitarbeiter} ist als ${r.typ} eingetragen.`;
  if (tool === "wissen_merken") return `In der Wissensdatenbank gespeichert (${r.kategorie}).`;
  if (tool === "rueckruf_erledigt") return `Rückruf von ${r.kunde} abgehakt.`;
  if (tool === "mitarbeiter_zurueck") return `${r.mitarbeiter} ist wieder verfügbar.`;
  if (tool === "auftrag_status") return `Auftrag von ${r.kunde}: ${r.status_label}.`;
  if (tool === "material_anlegen") return `Material „${r.name}" im Katalog angelegt.`;
  if (tool === "wissen_loeschen") return `Wissens-Eintrag gelöscht.`;
  if (tool === "angebot_erstellen") return `Angebot für ${r.kunde} erstellt${r.lexware_voucher_number ? " (" + r.lexware_voucher_number + ")" : ""}.${r.warning ? " " + r.warning : ""}`;
  if (tool === "angebot_senden") return `Angebot an ${r.to_email} gesendet.`;
  if (tool === "rechnung_erstellen") return `Rechnung für ${r.kunde} erstellt${r.lexware_voucher_number ? " (" + r.lexware_voucher_number + ")" : ""}.${r.warning ? " " + r.warning : ""}`;
  if (tool === "rechnung_abrechnen") return r.mail_sent ? `Rechnung an ${r.email_used} gesendet — Auftrag abgeschlossen.` : `Rechnung in Lexware angelegt${r.mail_error ? " (Mail offen: " + r.mail_error + ")" : ""}.`;
  if (tool === "anfrage_beantworten") return `Antwort an ${r.kunde} gesendet${r.closed ? " (Anfrage geschlossen)" : ""}.`;
  if (tool === "termin_verschieben") return `Termin von ${r.kunde} verschoben${r.alter_termin_entfernt === false ? " (alten Termin bitte im Kalender prüfen)" : ""}.`;
  if (tool === "drive_ordner_anlegen") return `Drive-Ordner für ${r.kunde} bereit${r.link ? ": " + r.link : ""}.`;
  if (tool === "drive_notiz_anlegen") return `Notiz für ${r.kunde} in Drive abgelegt${r.link ? " (" + r.link + ")" : ""}.`;
  if (tool === "email_schreiben") return `E-Mail an ${r.to_email} gesendet${r.anhaenge ? " (" + r.anhaenge + " Anhang" + (r.anhaenge > 1 ? "e" : "") + ")" : ""}.`;
  return "Erledigt.";
}

// Für Drive-Aktionen eine HTML-Antwort mit echtem, klickbarem Link direkt
// zum Drive-Ordner. Gibt null zurück, wenn kein (gültiger) Link da ist →
// dann greift der normale Text-Pfad (_assistResultText).
function _assistResultHtml(tool, r) {
  const raw = r && r.link;
  if (!raw || !/^https:\/\//i.test(String(raw))) return null;
  if (tool !== "drive_ordner_anlegen" && tool !== "drive_notiz_anlegen") return null;
  const href = String(raw).replace(/&/g, "&amp;").replace(/"/g, "%22");
  const kunde = esc(r.kunde || "Kunde");
  const lead = tool === "drive_ordner_anlegen"
    ? `✓ Drive-Ordner für ${kunde} bereit`
    : `✓ Notiz für ${kunde} in Drive abgelegt`;
  return `${lead} — <a href="${href}" target="_blank" rel="noopener noreferrer">in Drive öffnen ↗</a>`;
}

// ---------- Q-Overlay ----------

function toggleQOverlay(forceClose) {
  const overlay = document.getElementById("q-overlay");
  if (!overlay) return;
  // Ist der Assistent-Chat gerade wirklich zu sehen, ist Q direkt erreichbar —
  // Overlay bleibt zu. App.current reicht als Kriterium nicht: showKundenProfil()
  // rendert z.B. das Profil in die View, ohne App.current zu aendern, und dann
  // muss Q per Overlay erreichbar sein.
  // Statt stillem No-Op (fuehlt sich an wie "App kaputt") das Eingabefeld
  // fokussieren — sichtbares Feedback, dass Q genau hier schon bereit ist.
  if (!forceClose && document.getElementById("q-chat")) {
    const inp = document.getElementById("q-input");
    if (inp) { inp.focus(); inp.scrollIntoView({ block: "center", behavior: "smooth" }); }
    return;
  }
  // Archiv-Bild offen: Bild in den Assistent laden statt Overlay öffnen,
  // damit Q mit vollem Bildkontext antworten kann.
  if (!forceClose && App._archivPreviewFile) {
    const { proxyUrl, name } = App._archivPreviewFile;
    document.getElementById("archiv-preview-modal")?.remove();
    App._archivPreviewFile = null;
    fetch(proxyUrl).then((r) => r.blob()).then((blob) => {
      App.qPendingFile = new File([blob], name, { type: blob.type || "image/jpeg" });
      App.qPendingPreviewUrl = proxyUrl;
      navigate("assistent");
    }).catch(() => navigate("assistent"));
    return;
  }
  const opening = forceClose ? false : !overlay.classList.contains("open");
  // Zugeklapptes Panel darf nicht weiter mithoeren.
  if (!opening && App.qOverlayVoice) App.qOverlayVoice.stop("cancel");
  overlay.classList.toggle("open", opening);
  document.querySelectorAll(".tabbar button[data-tab='_qoverlay']").forEach((b) =>
    b.classList.toggle("active", opening));
  if (opening) {
    _qOverlayRender();
    const inp = document.getElementById("q-overlay-input");
    if (inp) setTimeout(() => inp.focus(), 280);
  }
}

// Hilfsfunktion: letzten Text-Inhalt einer Rolle aus App.qchat lesen
function _qLastMsg(role) {
  for (let i = (App.qchat || []).length - 1; i >= 0; i--) {
    if (App.qchat[i].role === role && App.qchat[i].text) return App.qchat[i].text;
  }
  return null;
}

function _qOverlayRender() {
  const msgsEl = document.getElementById("q-overlay-msgs");
  if (!msgsEl) return;
  const chat = App.qchat || [];

  let html = "";
  const isTyping = chat.some((m) => m.role === "typing");
  if (isTyping) {
    // Letzte User-Nachricht + Typing-Indikator
    const lastMe = [...chat].reverse().find((m) => m.role === "me");
    if (lastMe) html += `<div class="q-ov-bbl me">${esc(lastMe.text || "")}</div>`;
    html += `<div class="q-typing-row"><div class="q-thinking-orb" id="q-typing-orb-ov"><canvas id="q-mini-sphere-canvas-ov"></canvas></div><div class="q-typing-ring-wrap"><div class="q-ov-bbl q typing"><span></span><span></span><span></span></div></div></div>`;
  } else {
    // Letzten Q-Response (q / err / confirm) + die dazugehörige User-Frage suchen
    let lastQIdx = -1;
    let lastQMsg = null;
    for (let i = chat.length - 1; i >= 0; i--) {
      const r = chat[i].role;
      if (r === "q" || r === "err" || r === "confirm" || r === "mail") { lastQIdx = i; lastQMsg = chat[i]; break; }
    }
    if (lastQMsg) {
      const lastMe = chat.slice(0, lastQIdx).reverse().find((m) => m.role === "me");
      if (lastMe) html += `<div class="q-ov-bbl me">${esc(lastMe.text || "")}</div>`;
      if (lastQMsg.role === "q")      html += `<div class="q-ov-bbl q">${lastQMsg.html ? lastQMsg.text : esc(lastQMsg.text || "")}</div>`;
      else if (lastQMsg.role === "err")  html += `<div class="q-ov-bbl err">${esc(lastQMsg.text || "")}</div>`;
      else if (lastQMsg.role === "confirm") {
        // Aktion direkt hier bestätigen — kein Umweg über den Assistent-Tab.
        // m lebt in App.qchat: wechselt der Nutzer doch in den Assistenten,
        // sieht er dort denselben Stand (bestätigt/abgebrochen).
        if (lastQMsg.resolved) {
          html += `<div class="q-ov-bbl q"><p class="q-summary" style="margin:0 0 6px">${esc(lastQMsg.summary || "Aktion")}</p><div class="confirm-done">${lastQMsg.cancelled ? "✕ Abgebrochen" : "✓ Bestätigt"}</div></div>`;
        } else {
          html += `<div class="q-ov-bbl q" style="max-width:100%">
             ${lastQMsg.frage ? `<p style="margin:0 0 8px">${esc(lastQMsg.frage)}</p>` : ""}
             <p class="q-summary" style="margin:0 0 8px">${esc(lastQMsg.summary || "Aktion")}</p>
             <div class="confirm-actions"><button class="btn-sm" data-ov-cyes="${lastQIdx}">Ausführen</button><button class="btn-sm btn-ghost" data-ov-cno="${lastQIdx}">Abbrechen</button></div>
           </div>`;
        }
      }
      else if (lastQMsg.role === "mail") {
        const d = lastQMsg.data || {};
        if (lastQMsg.resolved) {
          html += `<div class="q-ov-bbl q"><p class="q-summary" style="margin:0 0 6px">✉️ E-Mail an ${esc(d.empfaenger || d.empfaenger_name || "")}</p><div class="confirm-done">${lastQMsg.sent ? "✓ Gesendet" : "✕ Abgebrochen"}</div></div>`;
        } else {
          // Kompakter Mail-Editor: An/Betreff/Text direkt hier redigier- und
          // sendbar. Anhänge verwalten geht weiterhin im Assistenten (Link).
          const anh = (d.anhaenge || []).length;
          html += `<div class="q-ov-bbl q" style="max-width:100%;width:100%">
             ${lastQMsg.frage ? `<p style="margin:0 0 8px">${esc(lastQMsg.frage)}</p>` : ""}
             <p class="q-summary" style="margin:0 0 6px">✉️ E-Mail-Entwurf</p>
             ${d.hinweis ? `<p class="sub" style="margin:0 0 8px">${esc(d.hinweis)}</p>` : ""}
             <label class="sub">An</label>
             <input type="email" class="rech-input" data-ov-mmail="${lastQIdx}" value="${esc(d.empfaenger || "")}" placeholder="kunde@example.de" autocomplete="off" autocapitalize="off" spellcheck="false">
             <label class="sub">Betreff</label>
             <input type="text" class="rech-input" data-ov-msubj="${lastQIdx}" value="${esc(d.betreff || "")}" placeholder="Betreff">
             <label class="sub">Text</label>
             <textarea class="rech-input" data-ov-mtext="${lastQIdx}" rows="5">${esc(d.text || "")}</textarea>
             ${anh ? `<p class="sub" style="margin:4px 0 0">📎 ${anh} Anhang${anh > 1 ? "e" : ""}</p>` : ""}
             <div class="confirm-actions"><button class="btn-sm" data-ov-msend="${lastQIdx}">Senden</button><button class="btn-sm btn-ghost" data-ov-mcancel="${lastQIdx}">Abbrechen</button></div>
             <p class="sub" style="margin:8px 0 0"><a href="#" id="q-ov-confirm-link">Im Assistenten öffnen (Anhänge) ›</a></p>
           </div>`;
        }
      }
    } else {
      html = `<p class="q-ov-hint">Stell mir eine Frage — ich bin auch hier.</p>`;
    }
  }

  msgsEl.innerHTML = html;
  const cl = msgsEl.querySelector("#q-ov-confirm-link");
  if (cl) cl.addEventListener("click", (e) => { e.preventDefault(); toggleQOverlay(true); navigate("assistent"); });
  // Aktion bestätigen/abbrechen
  msgsEl.querySelectorAll("[data-ov-cyes]").forEach((b) =>
    b.addEventListener("click", () => _qOverlayConfirm(parseInt(b.dataset.ovCyes, 10))));
  msgsEl.querySelectorAll("[data-ov-cno]").forEach((b) =>
    b.addEventListener("click", () => {
      const m = App.qchat[parseInt(b.dataset.ovCno, 10)];
      if (!m || m.resolved) return;
      m.resolved = true; m.cancelled = true;
      App.qchat.push({ role: "q", text: "Okay, lasse ich." });
      _qOverlayRender();
    }));
  // Mail-Editor: Eingaben in m.data spiegeln (überleben Re-Render + Tab-Wechsel)
  msgsEl.querySelectorAll("[data-ov-mmail]").forEach((t) =>
    t.addEventListener("input", () => { App.qchat[parseInt(t.dataset.ovMmail, 10)].data.empfaenger = t.value; }));
  msgsEl.querySelectorAll("[data-ov-msubj]").forEach((t) =>
    t.addEventListener("input", () => { App.qchat[parseInt(t.dataset.ovMsubj, 10)].data.betreff = t.value; }));
  msgsEl.querySelectorAll("[data-ov-mtext]").forEach((t) =>
    t.addEventListener("input", () => { App.qchat[parseInt(t.dataset.ovMtext, 10)].data.text = t.value; }));
  msgsEl.querySelectorAll("[data-ov-msend]").forEach((b) =>
    b.addEventListener("click", () => _qOverlayMailSenden(parseInt(b.dataset.ovMsend, 10))));
  msgsEl.querySelectorAll("[data-ov-mcancel]").forEach((b) =>
    b.addEventListener("click", () => {
      const m = App.qchat[parseInt(b.dataset.ovMcancel, 10)];
      if (!m || m.resolved) return;
      m.resolved = true; m.sent = false;
      App.qchat.push({ role: "q", text: "Okay, die Mail lasse ich." });
      _qOverlayRender();
    }));
  _mountMiniSphere("q-typing-orb-ov", "q-mini-sphere-canvas-ov", "qMiniSphereOvStop");
  msgsEl.scrollTop = msgsEl.scrollHeight;
}

function _qOvPopTyping() {
  const i = App.qchat.findIndex((x) => x.role === "typing");
  if (i >= 0) App.qchat.splice(i, 1);
}

// Bestätigte Aktion direkt aus dem Overlay ausführen — gleicher Endpunkt und
// gleiche Ergebnistexte wie doConfirm() im Assistent-Screen.
async function _qOverlayConfirm(idx) {
  const m = App.qchat[idx];
  if (!m || m.resolved) return;
  m.resolved = true;
  App.qchat.push({ role: "typing" });
  _qOverlayRender();
  let res, j = null;
  try {
    res = await fetch("/app/api/assistent/ausfuehren", { method: "POST",
      headers: { "X-CSRF-Token": App.me.csrf, "Content-Type": "application/json" },
      body: JSON.stringify({ tool: m.tool, args: m.args }) });
  } catch (e) {
    _qOvPopTyping();
    App.qchat.push({ role: "err", text: "Netzwerkfehler. Bitte erneut." });
    _qOverlayRender(); return;
  }
  if (res.status === 303 || res.status === 401 || res.redirected) { location.href = "/app/login"; return; }
  try { j = await res.json(); } catch (e) {}
  _qOvPopTyping();
  if (j && j.type === "done" && j.result && j.result.ok) {
    const html = _assistResultHtml(m.tool, j.result);
    if (html) App.qchat.push({ role: "q", text: html, html: true });
    else App.qchat.push({ role: "q", text: "✓ " + _assistResultText(m.tool, j.result) });
  } else {
    App.qchat.push({ role: "err", text: (j && (j.text || (j.result && j.result.error))) || "Aktion fehlgeschlagen." });
  }
  _qOverlayRender();
}

// Mail-Entwurf direkt aus dem Overlay senden — Spiegel von doMailSenden() im
// Assistent-Screen (gleiche Validierung, gleicher Endpunkt, Anhänge aus m.data
// reisen mit).
async function _qOverlayMailSenden(idx) {
  const m = App.qchat[idx];
  if (!m || m.resolved) return;
  const d = m.data || {};
  // Fehler als Toast statt Chat-Bubble: das Overlay zeigt nur die letzte
  // Nachricht — eine Fehler-Bubble würde den Entwurf aus dem Blick schieben.
  const fail = (t) => { toast(t, "err"); _qOverlayRender(); };
  if (!/^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$/.test((d.empfaenger || "").trim())) { fail("Bitte eine gültige Empfänger-Adresse eintragen."); return; }
  if ((d.betreff || "").trim().length < 2) { fail("Bitte einen Betreff eintragen."); return; }
  if ((d.text || "").trim().length < 2) { fail("Der Mail-Text fehlt."); return; }
  m.resolved = true; m.sent = false;
  App.qchat.push({ role: "typing" });
  _qOverlayRender();
  const args = {
    empfaenger: (d.empfaenger || "").trim(),
    empfaenger_name: d.empfaenger_name || "",
    betreff: (d.betreff || "").trim(),
    text: (d.text || "").trim(),
    kunde_name: d.kunde_name || "",
    anhaenge: d.anhaenge || [],
  };
  let res, j = null;
  try {
    res = await fetch("/app/api/assistent/ausfuehren", { method: "POST",
      headers: { "X-CSRF-Token": App.me.csrf, "Content-Type": "application/json" },
      body: JSON.stringify({ tool: "email_schreiben", args }) });
  } catch (e) { _qOvPopTyping(); m.resolved = false; fail("Netzwerkfehler beim Senden."); return; }
  if (res.status === 303 || res.status === 401 || res.redirected) { location.href = "/app/login"; return; }
  try { j = await res.json(); } catch (e) {}
  _qOvPopTyping();
  if (j && j.type === "done" && j.result && j.result.ok) {
    m.sent = true;
    App.qchat.push({ role: "q", text: "✓ " + _assistResultText("email_schreiben", j.result) });
    _qOverlayRender();
  } else {
    // Entwurf wieder aufmachen — Adresse/Text korrigieren statt Text verlieren.
    m.resolved = false;
    fail((j && (j.text || (j.result && j.result.error))) || "Mail konnte nicht gesendet werden.");
  }
}

async function _qOverlaySend(text) {
  if (!text.trim()) return;
  App.qchat = App.qchat || [];
  App.qhistory = App.qhistory || [];
  const QHIST_MAX = 8;
  const history = App.qhistory.slice(-QHIST_MAX);
  App.qhistory.push({ role: "user", text });
  App.qchat.push({ role: "me", text });
  App.qchat.push({ role: "typing" });
  _qOverlayRender();

  let res, j = null;
  try {
    res = await fetch("/app/api/assistent", { method: "POST",
      headers: { "X-CSRF-Token": App.me.csrf, "Content-Type": "application/json" },
      body: JSON.stringify({ text, history, screen_context: App.screenContext || null }) });
  } catch (_) {
    App.qchat.splice(App.qchat.findIndex((m) => m.role === "typing"), 1);
    App.qchat.push({ role: "err", text: "Netzwerkfehler." });
    _qOverlayRender(); return;
  }

  const typIdx = App.qchat.findIndex((m) => m.role === "typing");
  if (typIdx >= 0) App.qchat.splice(typIdx, 1);

  try { j = await res.json(); } catch (_) {}
  if (!j || !j.type) { App.qchat.push({ role: "err", text: "Fehler." }); _qOverlayRender(); return; }

  if (j.type === "message") {
    App.qchat.push({ role: "q", text: j.text });
    App.qhistory.push({ role: "model", text: j.text });
  } else if (j.type === "navigate") {
    if (j.text) { App.qchat.push({ role: "q", text: j.text }); App.qhistory.push({ role: "model", text: j.text }); }
    _qOverlayRender();
    setTimeout(() => { toggleQOverlay(true); handleNavigate(j.bereich, j.kunde, j.kategorie); }, 600);
    return;
  } else if (j.type === "confirm") {
    const say = j.frage || j.summary;
    if (say) App.qhistory.push({ role: "model", text: say });
    App.qchat.push({ role: "confirm", tool: j.tool, args: j.args, summary: j.summary, frage: j.frage, resolved: false });
  } else if (j.type === "email_entwurf") {
    // Der Entwurf braucht Platz zum Redigieren — im Overlay nur anteasern,
    // bearbeitet und freigegeben wird er im Assistenten.
    const say = j.frage || `Mail-Entwurf an ${j.empfaenger || j.empfaenger_name || "den Kunden"}: ${j.betreff || ""}`;
    App.qhistory.push({ role: "model", text: say });
    App.qchat.push(mailDraftMsg(j));
  } else if (j.type === "done") {
    // Automatisierung steht auf 'automatisch': die Aktion ist schon
    // gelaufen. Ohne diesen Zweig bliebe das Overlay stumm.
    const txt = (j.frage ? j.frage + " " : "") + "✓ " + (j.summary || "Erledigt.");
    App.qhistory.push({ role: "model", text: txt });
    App.qchat.push({ role: "q", text: txt });
  } else if (j.type === "error") {
    App.qchat.push({ role: "err", text: j.text });
  }
  _qOverlayRender();
}

function initQOverlay() {
  const overlay  = document.getElementById("q-overlay");
  const inp      = document.getElementById("q-overlay-input");
  const sendBtn  = document.getElementById("q-overlay-send");
  const closeBtn = document.getElementById("q-overlay-close");
  const micBtn   = document.getElementById("q-overlay-mic");
  if (!overlay || !inp || !sendBtn) return;

  // Schließen per X-Button oder Außerhalb-Klick
  if (closeBtn) closeBtn.addEventListener("click", () => toggleQOverlay(true));
  document.addEventListener("pointerdown", (e) => {
    if (overlay.classList.contains("open") &&
        !overlay.contains(e.target) &&
        !e.target.closest("[data-tab='_qoverlay']")) {
      toggleQOverlay(true);
    }
  }, { passive: true });

  // Senden
  async function send() {
    const text = inp.value.trim();
    if (!text) return;
    inp.value = "";
    await _qOverlaySend(text);
  }
  sendBtn.addEventListener("click", send);
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });

  // Sprechen im Overlay — dieselbe Steuerung wie im Assistenten: ein Tipp
  // startet, ein zweiter sendet, Halten sendet beim Loslassen. Waehrend der
  // Aufnahme weicht die Eingabezeile der Leiste mit Laufzeit und Live-Pegel.
  if (micBtn) {
    const inputRow = document.getElementById("q-ov-inputrow");
    const ovVoice = createVoiceRecorder({
      bar: document.getElementById("q-ov-recbar"),
      wave: document.getElementById("q-ov-rec-wave"),
      time: document.getElementById("q-ov-rec-time"),
      onUi(on) {
        micBtn.classList.toggle("recording", on);
        if (inputRow) inputRow.hidden = on;
      },
      onText(text) { _qOverlaySend(text); },
      onError(msg) {
        App.qchat = App.qchat || [];
        App.qchat.push({ role: "err", text: msg });
        _qOverlayRender();
      },
    });
    App.qOverlayVoice = ovVoice;
    micBtn.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      try { micBtn.setPointerCapture(e.pointerId); } catch (_) {}
      ovVoice.pointerDown();
    });
    micBtn.addEventListener("pointerup", () => ovVoice.pointerUp());
    micBtn.addEventListener("pointercancel", () => ovVoice.pointerCancel());
    const ovCancel = document.getElementById("q-ov-rec-cancel");
    const ovSend = document.getElementById("q-ov-rec-send");
    if (ovCancel) ovCancel.addEventListener("click", () => ovVoice.stop("cancel"));
    if (ovSend) ovSend.addEventListener("click", () => ovVoice.stop("send"));
  }
}

// ---------- Boot ----------
// Q-Verlauf ueber einen Reload retten (nur sessionStorage — nicht persistent,
// wahrt die Datensparsamkeit). Bild-Vorschauen (Blob-URLs) ueberleben einen
// Reload ohnehin nicht und werden beim Sichern verworfen.
function saveQState() {
  try {
    const slim = (App.qchat || []).map((m) => {
      const c = { ...m };
      delete c.previewUrl; delete c.file; delete c.fileBlob;
      return c;
    });
    sessionStorage.setItem("q_state", JSON.stringify({ chat: slim, hist: App.qhistory || [] }));
  } catch (e) { /* Storage voll/blockiert — dann halt nicht */ }
}
function restoreQState() {
  try {
    const raw = sessionStorage.getItem("q_state");
    if (!raw) return;
    const s = JSON.parse(raw);
    if (s && Array.isArray(s.chat) && s.chat.length) App.qchat = s.chat;
    if (s && Array.isArray(s.hist)) App.qhistory = s.hist;
  } catch (e) { /* defekter Eintrag — ignorieren */ }
}
// Auch bei manuellem Reload / Tab-Schliessen sichern (Best effort).
window.addEventListener("pagehide", saveQState);

async function boot() {
  if ("serviceWorker" in navigator) {
    try {
      // War schon ein SW aktiv? Dann ist ein späterer controllerchange ein echtes
      // Update (nicht die Erst-Installation) → einmal neu laden, damit frisches
      // JS/CSS greift. So bleibt niemand mehr auf einer alten Version hängen.
      const hadController = !!navigator.serviceWorker.controller;
      let reloaded = false;
      navigator.serviceWorker.addEventListener("controllerchange", () => {
        if (reloaded || !hadController) return;
        reloaded = true;
        // Vor dem selbst ausgeloesten Update-Reload den Q-Verlauf sichern,
        // sonst sind laufende Chats + unbestaetigte Entwuerfe weg (nur RAM).
        saveQState();
        location.reload();
      });
      const reg = await navigator.serviceWorker.register("/app/sw.js", { scope: "/app" });
      reg.update().catch(() => {});
    } catch (e) { console.warn("SW-Registrierung fehlgeschlagen", e); }
  }
  const res = await api("/app/api/me");
  if (!res) return;
  App.me = await res.json();
  setPermissions(App.me.permissions);
  applyBrandColor(App.me.tenant.brand_color);
  document.getElementById("hdr-title").textContent = App.me.tenant.company_name || "Gewerbeagent";
  const nb = document.getElementById("notif-btn");
  if (notifSupported() && !notifGranted()) { nb.hidden = false; nb.addEventListener("click", enablePush); }
  document.getElementById("back-btn").addEventListener("click", () => history.back());
  initEdgeSwipe();
  buildTabbar();
  initQOverlay();
  requestAnimationFrame(() => requestAnimationFrame(_mountTabSphere));
  // Neue Nutzer: Q führt durch die Ersteinrichtung (Flag wird im
  // Assistent-Screen ausgewertet, der den Onboarding-Chat startet).
  if (!App.me.onboarding_done) App.startOnboarding = true;
  // Q-Verlauf aus einem vorangegangenen Reload wiederherstellen (falls vorhanden).
  // Nur wenn kein Onboarding laeuft — das startet bewusst mit leerem Chat.
  if (!App.startOnboarding) restoreQState();
  // Kam die App aus einer Push-Benachrichtigung ("/app#anfragen"), direkt
  // dorthin. Sonst der normale Start-Screen. mode:"replace", damit der erste
  // Zurueck-Druck die App verlaesst statt auf einen leeren Eintrag zu fallen.
  const start = (App.startOnboarding ? null : screenFromHash()) || "assistent";
  navigate(start, { mode: "replace" });
}

boot();
