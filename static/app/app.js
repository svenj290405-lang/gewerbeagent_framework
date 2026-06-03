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
  { key: "start",       label: "Start",    ico: "🏠" },
  { key: "anfragen",    label: "Anfragen", ico: "✉️", feature: "mail_intake" },
  { key: "termine",     label: "Termine",  ico: "📅", feature: "kalender" },
  { key: "anrufe",      label: "Anrufe",   ico: "📞", feature: "voice_init" },
  { key: "buchhaltung", label: "Büro",     ico: "🧾", feature: "lexware" },
  { key: "mehr",        label: "Mehr",     ico: "⋯" },
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
      (d.aufnahmen || []).length ? d.aufnahmen.map((a) => rowTap(a.kunde || "Aufnahme", a.briefing || "", a.zeit, a.id)).join("") : emptyRow("Keine neuen Aufnahmen")
    }</div>`);

    App.view.innerHTML = parts.join("");
    const inline = document.getElementById("enable-notif-inline");
    if (inline) inline.addEventListener("click", enablePush);
    bindAufnahmen();
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
        (ad.aufnahmen || []).length ? ad.aufnahmen.map((x) => rowTap(x.kunde || "Aufnahme", x.briefing || "", x.zeit, x.id)).join("")
        : emptyRow("Noch keine Aufnahmen")
      }</div>`;
    bindRueckrufDone();
    bindAufnahmen();
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

  async kunden() {
    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-mehr" style="margin-bottom:10px">← Zurück</button>` +
      `<div class="card"><input id="kunde-q" type="text" placeholder="Kundenname suchen …" autocomplete="off" /></div>` +
      `<div id="kunde-res"><p class="empty">Mind. 2 Zeichen eingeben.</p></div>`;
    document.getElementById("back-mehr").addEventListener("click", () => navigate("mehr"));
    const input = document.getElementById("kunde-q");
    const res = document.getElementById("kunde-res");
    let timer = null;
    input.focus();
    input.addEventListener("input", () => {
      clearTimeout(timer);
      const q = input.value.trim();
      if (q.length < 2) { res.innerHTML = `<p class="empty">Mind. 2 Zeichen eingeben.</p>`; return; }
      timer = setTimeout(async () => {
        res.innerHTML = `<div class="loading">Suche …</div>`;
        const r = await api("/app/api/kunden?q=" + encodeURIComponent(q));
        const d = r && r.ok ? await r.json() : {};
        const blocks = [];
        if ((d.gespraeche || []).length) blocks.push(`<div class="card"><h2>Gespräche</h2>${d.gespraeche.map((x) => rowTap(x.kunde, x.briefing, x.zeit, x.id)).join("")}</div>`);
        if ((d.angebote || []).length) blocks.push(`<div class="card"><h2>Angebote</h2>${d.angebote.map((x) => row(x.kunde, x.betrag, x.zeit)).join("")}</div>`);
        if ((d.rechnungen || []).length) blocks.push(`<div class="card"><h2>Rechnungen</h2>${d.rechnungen.map((x) => row(x.kunde + (x.nummer ? " · " + esc(x.nummer) : ""), x.betrag, x.zeit)).join("")}</div>`);
        res.innerHTML = blocks.length ? blocks.join("") : `<p class="empty">Nichts gefunden für „${esc(q)}".</p>`;
        bindAufnahmen();
      }, 300);
    });
  },

  async anfragen() {
    const res = await api("/app/api/anfragen");
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
    document.querySelectorAll("[data-anfrage]").forEach((b) =>
      b.addEventListener("click", () => showAnfrage(b.dataset.anfrage)));
  },

  async wissen() {
    const res = await api("/app/api/wissen");
    const d = res && res.ok ? await res.json() : { eintraege: [], kategorien: [] };
    const isInhaber = App.me.employee.is_inhaber;
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

  async mehr() {
    const m = App.me;
    const feats = new Set(m.features || []);
    const menu = [];
    menu.push(`<button class="row menu-item" data-go="kunden"><span>🔍 Kunden suchen</span><span class="sub">›</span></button>`);
    menu.push(`<button class="row menu-item" data-go="wissen"><span>📚 Wissensdatenbank</span><span class="sub">›</span></button>`);
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
function rowTap(a, b, c, id) {
  return `<button class="row menu-item" data-aufnahme="${esc(id)}" style="align-items:flex-start">` +
    `<div style="text-align:left"><div>${esc(a)}</div>${b ? `<div class="sub">${esc(b)}</div>` : ""}</div>` +
    `<span class="sub">${esc(c)} ›</span></button>`;
}
function emptyRow(txt) { return `<div class="empty">${esc(txt)}</div>`; }

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

  App.view.innerHTML =
    `<button class="btn-sm btn-ghost" id="back-anfragen" style="margin-bottom:10px">← Zurück</button>` +
    `<div class="card">
      <div class="row"><div><b>${esc(d.kunde_name || d.kunde_email)}</b><div class="sub">${esc(d.kunde_email)}</div></div>
      <span class="pill ${d.state_style || ""}">${esc(d.state_label)}</span></div>
      <div class="sub" style="margin-top:6px">${esc(d.subject || "")} · ${esc(d.updated_at_fmt || "")}</div>
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

  document.getElementById("back-anfragen").addEventListener("click", () => navigate("anfragen"));
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
          alert(close ? "Antwort gesendet. Anfrage als erledigt markiert." : "Antwort gesendet.");
          navigate("anfragen");
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

async function showAufnahme(id) {
  App.view.innerHTML = `<div class="loading">Lädt …</div>`;
  const r = await api("/app/api/aufnahmen/" + encodeURIComponent(id));
  if (!r || !r.ok) { App.view.innerHTML = `<div class="card"><p class="empty">Konnte nicht laden.</p></div>`; return; }
  const d = await r.json();
  const todos = (d.todos || []).length
    ? `<div class="card"><h2>To-dos</h2>${d.todos.map((t) => `<div class="row"><div>☐ ${esc(t)}</div></div>`).join("")}</div>` : "";
  const termin = d.termin ? `<div class="row"><span>Termin</span><span class="sub">${esc(d.termin)}${d.termin_ort ? " · " + esc(d.termin_ort) : ""}</span></div>` : "";
  App.view.innerHTML =
    `<button class="btn-sm btn-ghost" id="back-anrufe" style="margin-bottom:10px">← Zurück</button>` +
    `<div class="card"><h2>${esc(d.kunde || "Aufnahme")}</h2>
       <div class="row"><span>Zeitpunkt</span><span class="sub">${esc(d.zeit)}${d.dauer ? " · " + esc(d.dauer) : ""}</span></div>${termin}</div>` +
    (d.briefing ? `<div class="card"><h2>Briefing</h2><div>${esc(d.briefing)}</div></div>` : "") +
    (d.notizen ? `<div class="card"><h2>Notizen</h2><div>${esc(d.notizen)}</div></div>` : "") +
    todos +
    (d.transkript ? `<div class="card"><h2>Transkript</h2><div class="sub" style="white-space:pre-wrap">${esc(d.transkript)}</div></div>` : "");
  document.getElementById("back-anrufe").addEventListener("click", () => navigate(App.current || "anrufe"));
}

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
