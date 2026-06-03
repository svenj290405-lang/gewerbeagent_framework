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
  current: "start",
};

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
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function el(html) { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; }

// Notification-API ist nicht überall da (z.B. iOS Safari ohne Home-Screen-
// Installation) — defensiv prüfen, sonst wirft ein blanker Zugriff und die
// App bleibt beim Laden hängen.
function notifSupported() { return typeof Notification !== "undefined"; }
function notifGranted() { return notifSupported() && Notification.permission === "granted"; }

// ---------- Tabs ----------
const TABS = [
  { key: "start",       label: "Start",   ico: "🏠" },
  { key: "termine",     label: "Termine", ico: "📅", feature: "kalender" },
  { key: "anrufe",      label: "Anrufe",  ico: "📞", feature: "voice_init" },
  { key: "buchhaltung", label: "Büro",    ico: "🧾", feature: "lexware" },
  { key: "mehr",        label: "Mehr",    ico: "⋯" },
];

function buildTabbar() {
  const bar = document.getElementById("tabbar");
  bar.innerHTML = "";
  const feats = new Set(App.me.features || []);
  TABS.filter((t) => !t.feature || feats.has(t.feature)).forEach((t) => {
    const b = el(`<button data-tab="${t.key}"><span class="ico">${t.ico}</span>${t.label}</button>`);
    b.addEventListener("click", () => navigate(t.key));
    bar.appendChild(b);
  });
}

function navigate(key) {
  App.current = key;
  document.querySelectorAll(".tabbar button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === key));
  const fn = SCREENS[key] || SCREENS.start;
  App.view.innerHTML = `<div class="loading">Lädt …</div>`;
  fn().catch((e) => {
    App.view.innerHTML = `<div class="card"><p class="empty">Konnte nicht laden.</p></div>`;
    console.error(e);
  });
}

// ---------- Screens ----------
const SCREENS = {
  async start() {
    const res = await api("/app/api/dashboard");
    const d = res && res.ok ? await res.json() : {};
    const parts = [];
    parts.push(`<h1 style="font-size:22px;margin:4px 4px 14px">Hallo${App.me.employee.name ? ", " + esc(App.me.employee.name.split(" ")[0]) : ""} 👋</h1>`);

    if (notifSupported() && !notifGranted()) {
      parts.push(`<div class="banner">Aktiviere Benachrichtigungen, damit du neue Buchungen und Rückrufe sofort siehst. <button class="btn-sm btn-ghost" id="enable-notif-inline">Aktivieren</button></div>`);
    }

    const today = (d.termine_heute || []);
    parts.push(`<div class="card"><h2>Heute (${today.length})</h2>${
      today.length ? today.map((t) => row(t.zeit, t.kunde, t.ort)).join("") : emptyRow("Keine Termine heute")
    }</div>`);

    parts.push(`<div class="card"><h2>Offene Rückrufe (${(d.rueckrufe || []).length})</h2>${
      (d.rueckrufe || []).length ? d.rueckrufe.map((r) => row(r.kunde, r.telefon, r.anliegen)).join("") : emptyRow("Keine offenen Rückrufe")
    }</div>`);

    parts.push(`<div class="card"><h2>Neue Aufnahmen (${(d.aufnahmen || []).length})</h2>${
      (d.aufnahmen || []).length ? d.aufnahmen.map((a) => row(a.kunde || "Aufnahme", a.briefing || "", a.zeit)).join("") : emptyRow("Keine neuen Aufnahmen")
    }</div>`);

    App.view.innerHTML = parts.join("");
    const inline = document.getElementById("enable-notif-inline");
    if (inline) inline.addEventListener("click", enablePush);
  },

  async termine() {
    const res = await api("/app/api/termine");
    const d = res && res.ok ? await res.json() : { termine: [] };
    const list = d.termine || [];
    App.view.innerHTML = `<div class="card"><h2>Anstehende Termine</h2>${
      list.length ? list.map((t) => rowAction(t.zeit, t.kunde, t.ort, t.id, "storno", "Stornieren")).join("") : emptyRow("Keine anstehenden Termine")
    }</div>`;
    bindStorno();
  },

  async anrufe() {
    const [a, r] = await Promise.all([api("/app/api/aufnahmen"), api("/app/api/rueckrufe")]);
    const ad = a && a.ok ? await a.json() : { aufnahmen: [] };
    const rd = r && r.ok ? await r.json() : { rueckrufe: [] };
    App.view.innerHTML =
      `<div class="card"><h2>Offene Rückrufe</h2>${
        (rd.rueckrufe || []).length ? rd.rueckrufe.map((x) =>
          rowAction(x.kunde, x.telefon + (x.anliegen ? " · " + esc(x.anliegen) : ""), "", x.id, "rueckruf-done", "Erledigt")).join("")
        : emptyRow("Keine offenen Rückrufe")
      }</div>` +
      `<div class="card"><h2>Letzte Aufnahmen</h2>${
        (ad.aufnahmen || []).length ? ad.aufnahmen.map((x) => row(x.kunde || "Aufnahme", x.briefing || "", x.zeit)).join("")
        : emptyRow("Noch keine Aufnahmen")
      }</div>`;
    bindRueckrufDone();
  },

  async buchhaltung() {
    const [a, r] = await Promise.all([api("/app/api/angebote"), api("/app/api/rechnungen")]);
    const ad = a && a.ok ? await a.json() : { angebote: [] };
    const rd = r && r.ok ? await r.json() : { rechnungen: [] };
    App.view.innerHTML =
      `<div class="card"><h2>Rechnungen</h2>${
        (rd.rechnungen || []).length ? rd.rechnungen.map((x) =>
          rowPill(x.kunde + (x.nummer ? " · " + esc(x.nummer) : ""), x.betrag + " · " + x.zeit, x.status, x.pill)).join("")
        : emptyRow("Noch keine Rechnungen")
      }</div>` +
      `<div class="card"><h2>Angebote</h2>${
        (ad.angebote || []).length ? ad.angebote.map((x) =>
          rowPill(x.kunde, x.betrag + " · " + x.zeit, x.status, x.pill)).join("")
        : emptyRow("Noch keine Angebote")
      }</div>` +
      `<p class="muted" style="text-align:center;margin-top:10px">Anlegen per Diktat/Foto folgt in einer späteren Version.</p>`;
  },

  async team() {
    const res = await api("/app/api/team");
    const d = res && res.ok ? await res.json() : { team: [] };
    const isInhaber = App.me.employee.is_inhaber;
    const cards = (d.team || []).map((e) => {
      const tags = [];
      if (e.abwesend_heute) tags.push(`<span class="pill danger">${e.abwesend_heute === "krank" ? "krank" : "abwesend"}</span>`);
      if (!e.is_active) tags.push(`<span class="pill">inaktiv</span>`);
      if (e.kalender_verbunden) tags.push(`<span class="pill ok">Kalender</span>`);
      if (e.app_verbunden) tags.push(`<span class="pill ok">App</span>`);
      const up = (e.kommende_abwesenheiten || []).map((a) =>
        `<div class="sub">${a.typ === "urlaub" ? "Urlaub" : a.typ}: ${esc(a.von)}–${esc(a.bis)}</div>`).join("");
      const skills = (e.skills || []).length ? `<div class="sub">${(e.skills || []).map(esc).join(", ")}</div>` : "";
      let actions = "";
      if (isInhaber && !e.is_inhaber) {
        actions = `<button class="btn-sm btn-ghost" data-act="toggle" data-slug="${esc(e.slug)}" data-active="${e.is_active ? "1" : "0"}">${e.is_active ? "Deaktivieren" : "Aktivieren"}</button>`;
      }
      return `<div class="card">
        <div class="row"><div><div><b>${esc(e.name)}</b>${e.is_inhaber ? " · Inhaber" : (e.job_title ? " · " + esc(e.job_title) : "")}</div>${skills}${up}</div><div>${actions}</div></div>
        <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">${tags.join("")}</div>
      </div>`;
    }).join("");
    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-mehr" style="margin-bottom:10px">← Zurück</button>` +
      `<div class="section-title">Team (${(d.team || []).length})</div>` +
      (cards || emptyRow("Keine Mitarbeiter")) +
      (isInhaber ? `<p class="muted" style="text-align:center;margin-top:6px">Mitarbeiter anlegen sowie Krank-/Urlaubsmeldung folgen in einer späteren Version.</p>` : "");
    document.getElementById("back-mehr").addEventListener("click", () => navigate("mehr"));
    document.querySelectorAll('[data-act="toggle"]').forEach((b) =>
      b.addEventListener("click", async () => {
        b.disabled = true;
        const res = await api(`/app/api/team/${encodeURIComponent(b.dataset.slug)}/aktiv`,
          { method: "POST", body: JSON.stringify({ active: b.dataset.active !== "1" }) });
        if (res && res.ok) navigate("team"); else { b.disabled = false; alert("Aktion fehlgeschlagen."); }
      }));
  },

  async mehr() {
    const m = App.me;
    const feats = new Set(m.features || []);
    const menu = [];
    if (feats.has("mitarbeiter")) menu.push(`<button class="row menu-item" data-go="team"><span>👥 Team</span><span class="sub">›</span></button>`);
    App.view.innerHTML =
      `<div class="card"><h2>${esc(m.tenant.company_name || "Mein Betrieb")}</h2>
        <div class="row"><span>Angemeldet als</span><span class="sub">${esc(m.employee.name)}${m.employee.is_inhaber ? " (Inhaber)" : ""}</span></div>
        <div class="row"><span>Freigeschaltete Funktionen</span><span class="sub">${(m.features || []).length}</span></div>
      </div>` +
      (menu.length ? `<div class="card"><h2>Verwaltung</h2>${menu.join("")}</div>` : "") +
      `<div class="card"><h2>Benachrichtigungen</h2>
        <div class="row"><span>Push auf diesem Gerät</span><span class="pill ${notifGranted() ? "ok" : "warn"}">${notifGranted() ? "aktiv" : (notifSupported() ? "aus" : "nicht unterstützt")}</span></div>
        ${notifSupported() ? `<button class="btn-sm btn-ghost" id="enable-notif-more" style="margin-top:8px">Push aktivieren</button>` : `<p class="muted" style="margin-top:6px">Auf dem iPhone: erst „Zum Home-Bildschirm" hinzufügen, dann sind Benachrichtigungen möglich.</p>`}
      </div>
      <div class="card"><form method="post" action="/app/logout"><button type="submit">Abmelden</button></form></div>
      <p class="muted" style="text-align:center;margin-top:14px">Weitere Funktionen (Material, Formulare, Wissen …) folgen.</p>`;
    const b = document.getElementById("enable-notif-more");
    if (b) b.addEventListener("click", enablePush);
    document.querySelectorAll(".menu-item").forEach((mi) =>
      mi.addEventListener("click", () => navigate(mi.dataset.go)));
  },
};

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
function emptyRow(txt) { return `<div class="empty">${esc(txt)}</div>`; }

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
  document.querySelectorAll('[data-action="rueckruf-done"]').forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      const res = await api("/app/api/rueckrufe/erledigt", { method: "POST", body: JSON.stringify({ id: b.dataset.id }) });
      if (res && res.ok) navigate("anrufe"); else { b.disabled = false; alert("Konnte nicht abhaken."); }
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

// ---------- Boot ----------
async function boot() {
  if ("serviceWorker" in navigator) {
    try { await navigator.serviceWorker.register("/app/sw.js", { scope: "/app" }); }
    catch (e) { console.warn("SW-Registrierung fehlgeschlagen", e); }
  }
  const res = await api("/app/api/me");
  if (!res) return;
  App.me = await res.json();
  document.getElementById("hdr-title").textContent = App.me.tenant.company_name || "Gewerbeagent";
  const nb = document.getElementById("notif-btn");
  if (notifSupported() && !notifGranted()) { nb.hidden = false; nb.addEventListener("click", enablePush); }
  buildTabbar();
  navigate("start");
}

boot();
