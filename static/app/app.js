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
    App.view.innerHTML =
      `<div style="display:flex;align-items:center;justify-content:space-between;margin:4px 4px 14px">
        <h1 style="font-size:22px;margin:0">Termine</h1>
        <button class="btn-sm" id="termin-new-btn" style="padding:8px 14px">+ Neu</button>
      </div>` +
      `<div class="card"><h2>Anstehende Termine</h2>${
        list.length ? list.map((t) => rowAction(t.zeit, t.kunde, t.ort, t.id, "storno", "Stornieren")).join("") : emptyRow("Keine anstehenden Termine")
      }</div>`;
    bindStorno();
    document.getElementById("termin-new-btn").addEventListener("click", showNewTerminForm);
  },

  async anrufe() {
    const [a, r] = await Promise.all([api("/app/api/aufnahmen"), api("/app/api/rueckrufe")]);
    const ad = a && a.ok ? await a.json() : { aufnahmen: [] };
    const rd = r && r.ok ? await r.json() : { rueckrufe: [] };
    App.view.innerHTML =
      `<div style="display:flex;align-items:center;justify-content:space-between;margin:4px 4px 14px">
        <h1 style="font-size:22px;margin:0">Anrufe</h1>
        <div style="display:flex;gap:6px">
          <button class="btn-sm" id="diktat-btn" style="padding:8px 12px">🎤 Diktat</button>
          <button class="btn-sm btn-ghost" id="rueckruf-new-btn" style="padding:8px 12px">+ Rückruf</button>
        </div>
      </div>` +
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
    document.getElementById("rueckruf-new-btn").addEventListener("click", showNewRueckrufForm);
    document.getElementById("diktat-btn").addEventListener("click", showDiktatForm);
  },

  async buchhaltung() {
    const isInhaber = App.me.employee.is_inhaber;
    const [a, r, b] = await Promise.all([
      api("/app/api/angebote"), api("/app/api/rechnungen"), api("/app/api/belege"),
    ]);
    const ad = a && a.ok ? await a.json() : { angebote: [] };
    const rd = r && r.ok ? await r.json() : { rechnungen: [] };
    const bd = b && b.ok ? await b.json() : { belege: [] };
    App.view.innerHTML =
      `<div style="display:flex;align-items:center;justify-content:space-between;margin:4px 4px 14px">
         <h1 style="font-size:22px;margin:0">Büro</h1>
         <div style="display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end">
           <button class="btn-sm btn-ghost" id="auftraege-btn" style="padding:8px 12px">🛠 Aufträge</button>
           <button class="btn-sm" id="beleg-new-btn" style="padding:8px 12px">📄 Beleg</button>
           ${isInhaber ? `
           <button class="btn-sm btn-ghost" id="rechnung-new-btn" style="padding:8px 12px">+ Rechnung</button>
           <button class="btn-sm btn-ghost" id="angebot-new-btn" style="padding:8px 12px">+ Angebot</button>` : ""}
         </div>
       </div>` +
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
      `<div class="card"><h2>Belege</h2>${
        (bd.belege || []).length ? bd.belege.map(belegRow).join("")
        : emptyRow("Noch keine Belege")
      }</div>`;
    const aBtn = document.getElementById("angebot-new-btn");
    const rBtn = document.getElementById("rechnung-new-btn");
    document.getElementById("beleg-new-btn").addEventListener("click", showBelegUpload);
    document.getElementById("auftraege-btn").addEventListener("click", showAuftraege);
    if (aBtn) aBtn.addEventListener("click", () => showAngebotForm());
    if (rBtn) rBtn.addEventListener("click", () => showRechnungForm());
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
        <div class="row"><div><div><b>${esc(e.name)}</b>${e.is_inhaber ? " · Inhaber" : (e.job_title ? " · " + esc(e.job_title) : "")}</div>${skills}${up}</div><div>${actions}</div></div>
        <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">${tags.join("")}</div>
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

  async material() {
    const res = await api("/app/api/material");
    const d = res && res.ok ? await res.json() : { items: [] };
    const isInhaber = App.me.employee.is_inhaber;
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
         <input type="file" id="viz-file" accept="image/jpeg,image/png" style="${inputStyle}" />
         <label class="sub">Was soll verändert werden?</label>
         <textarea id="viz-prompt" rows="3" placeholder="z.B. Wände in warmem Grau streichen, Eichenparkett verlegen" style="${inputStyle};font-family:inherit"></textarea>
         <button class="btn-sm" id="viz-go" style="width:100%;margin-top:4px" disabled>Visualisierung erstellen</button>
         <p class="muted" id="viz-status" style="margin-top:12px;min-height:20px"></p>
       </div>
       <div id="viz-result"></div>` +
      (recent ? `<div class="card"><h2>Bisherige</h2>${recent}</div>` : "");

    document.getElementById("back-mehr").addEventListener("click", () => navigate("mehr"));
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

  async einstellungen() {
    const res = await api("/app/api/einstellungen");
    const d = res && res.ok ? await res.json() : { stammdaten: {}, features: [] };
    const st = d.stammdaten || {};
    const isInhaber = !!d.is_inhaber;

    // Read-Only-View fuer Mitarbeiter, Editierbar fuer Inhaber. Felder die
    // OAuth/Voice betreffen sind hier nicht editierbar — der Setup-Wizard
    // bzw. Admin-UI bleibt zustaendig (Microsoft-Login, Drive-Anbindung,
    // Sipgate-Nummer-Routing).
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

    const readOnlyBlock = `<div class="card"><h2>Verbundene Dienste</h2>
      <div class="row"><span>Funktionen aktiv</span><span class="sub">${(d.features || []).length}</span></div>
      ${(d.features || []).length ? `<div class="sub" style="margin-top:6px">${(d.features || []).map(esc).join(", ")}</div>` : ""}
      <div class="row"><span>Paket</span><span class="sub">${esc(d.package_tier || "—")}</span></div>
      <div class="row"><span>Daten-Retention</span><span class="sub">${esc(String(d.data_retention_days || ""))} Tage</span></div>
      <p class="muted" style="margin-top:8px">Microsoft, Google, Lexware und die Telefonnummer (Sipgate) verwalte über den Setup-Bereich auf gewerbeagent.de.</p>
    </div>`;

    App.view.innerHTML =
      `<button class="btn-sm btn-ghost" id="back-mehr" style="margin-bottom:10px">← Zurück</button>` +
      `<h1 style="font-size:22px;margin:4px 4px 14px">Einstellungen</h1>` +
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
      readOnlyBlock +
      (isInhaber ? `<button class="btn-sm" id="set-save" style="width:100%;margin-top:8px">Speichern</button>` : "");

    document.getElementById("back-mehr").addEventListener("click", () => navigate("mehr"));
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
        if (r && r.ok) {
          const j = await r.json();
          if (j.ok) {
            saveBtn.textContent = "✓ Gespeichert";
            setTimeout(() => { saveBtn.textContent = "Speichern"; saveBtn.disabled = false; }, 1500);
            return;
          }
          alert("Konnte nicht speichern: " + (j.error || "unbekannt"));
        } else {
          alert("Konnte nicht speichern.");
        }
        saveBtn.disabled = false; saveBtn.textContent = "Speichern";
      });
    }
  },

  async mehr() {
    const m = App.me;
    const feats = new Set(m.features || []);
    const menu = [];
    menu.push(`<button class="row menu-item" data-go="kunden"><span>🔍 Kunden suchen</span><span class="sub">›</span></button>`);
    menu.push(`<button class="row menu-item" data-go="wissen"><span>📚 Wissensdatenbank</span><span class="sub">›</span></button>`);
    menu.push(`<button class="row menu-item" data-go="material"><span>🧰 Material</span><span class="sub">›</span></button>`);
    if (feats.has("visualisierung")) menu.push(`<button class="row menu-item" data-go="visualisierung"><span>🎨 Visualisierung</span><span class="sub">›</span></button>`);
    if (feats.has("mitarbeiter")) menu.push(`<button class="row menu-item" data-go="team"><span>👥 Team</span><span class="sub">›</span></button>`);
    menu.push(`<button class="row menu-item" data-go="einstellungen"><span>⚙️ Einstellungen</span><span class="sub">›</span></button>`);
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

// =================== Angebot / Rechnung Composer ===================
//
// Beide Composer teilen die gleiche Positionen-UI + KI-Extract-Optik.
// Rechnung hat zusaetzlich einen Pauschal-Modus (1 Titel + Brutto-Betrag),
// weil das im Handwerker-Alltag dominiert.

let _composerPositionen = [];
let _composerMode = "angebot"; // "angebot" | "rechnung"
let _rechnungInputMode = "pauschal"; // pauschal | positionen

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
            style="flex:1;padding:8px;border:1px solid var(--line);border-radius:8px;font-size:14px" placeholder="Menge" />
          <input type="text" data-fld="einheit" value="${esc(p.einheit || 'Stueck')}"
            style="flex:1;padding:8px;border:1px solid var(--line);border-radius:8px;font-size:14px" placeholder="Einheit" />
          <input type="number" data-fld="preis_brutto_eur" value="${esc(p.preis_brutto_eur || '')}" step="0.01" min="0"
            style="flex:1.2;padding:8px;border:1px solid var(--line);border-radius:8px;font-size:14px" placeholder="EUR brutto" />
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
      <input type="text" id="c-kunde-plz" placeholder="PLZ" style="flex:0 0 30%;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
      <input type="text" id="c-kunde-ort" placeholder="Ort" style="flex:1;padding:12px;border:1px solid var(--line);border-radius:10px;margin:4px 0 10px;font-size:16px" />
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

  document.getElementById("back-buero").addEventListener("click", () => navigate("buchhaltung"));
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

  document.getElementById("back-buero").addEventListener("click", () => navigate("buchhaltung"));

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
     <button class="btn-sm btn-ghost" id="back-buero" style="width:100%;margin-top:8px">Zurück zum Büro</button>`;
  document.getElementById("back-buero").addEventListener("click", () => navigate("buchhaltung"));
  const sendBtn = document.getElementById("send-pdf");
  if (sendBtn) sendBtn.addEventListener("click", async () => {
    sendBtn.disabled = true; sendBtn.textContent = "Sende …";
    const r = await api(`/app/api/${sendPath}/${encodeURIComponent(j.id)}/senden`,
      { method: "POST", body: "{}" });
    if (r && r.ok) {
      const k = await r.json();
      if (k.ok) {
        alert("Mail erfolgreich gesendet.");
        navigate("buchhaltung"); return;
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
       <button class="btn-sm" id="emp-save" style="margin-top:12px;width:100%">Mitarbeiter anlegen</button>
       <p class="muted" style="margin-top:8px;font-size:12px">Nach dem Anlegen bekommst du einen einmaligen Aktivierungs-Link zum Weitergeben.</p>
    </div>`;
  document.getElementById("back-team").addEventListener("click", () => navigate("team"));
  document.getElementById("emp-save").addEventListener("click", async () => {
    const name = document.getElementById("emp-name").value.trim();
    if (!name) { alert("Name ist Pflicht."); return; }
    const body = {
      name,
      job_title: document.getElementById("emp-job").value.trim() || null,
      contact_email: document.getElementById("emp-mail").value.trim() || null,
      skills: document.getElementById("emp-skills").value.trim() || null,
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

const Diktat = {
  ctx: null, source: null, node: null, zero: null, stream: null,
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
  Diktat.source.connect(Diktat.node);
  Diktat.node.connect(Diktat.zero);
  Diktat.zero.connect(Diktat.ctx.destination);
  Diktat.recording = true;
  Diktat.startTs = Date.now();
}

function _diktatTeardown() {
  Diktat.recording = false;
  try { if (Diktat.node) { Diktat.node.disconnect(); Diktat.node.onaudioprocess = null; } } catch (e) {}
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

async function showDiktatForm() {
  App.view.innerHTML =
    `<button class="btn-sm btn-ghost" id="back-anrufe" style="margin-bottom:10px">← Zurück</button>` +
    `<h1 style="font-size:22px;margin:4px 4px 6px">Gespräch diktieren</h1>` +
    `<p class="muted" style="margin:0 4px 14px">Sprich das Gespräch ein — Kundenname, was zu tun ist, Preise, Termin. Die KI erstellt daraus automatisch ein Briefing.</p>` +
    `<div class="card" style="text-align:center;padding:24px 16px">
       <div id="dk-timer" style="font-size:32px;font-variant-numeric:tabular-nums;margin-bottom:14px">0:00</div>
       <button class="btn-sm" id="dk-toggle" style="padding:14px 24px;font-size:17px">🎤 Aufnahme starten</button>
       <p class="muted" id="dk-status" style="margin-top:14px;min-height:20px"></p>
     </div>
     <div id="dk-result"></div>`;
  document.getElementById("back-anrufe").addEventListener("click", () => {
    if (Diktat.recording) _diktatTeardown();
    navigate("anrufe");
  });

  const toggle = document.getElementById("dk-toggle");
  const timerEl = document.getElementById("dk-timer");
  const statusEl = document.getElementById("dk-status");
  const resultEl = document.getElementById("dk-result");

  const fmtT = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

  async function stopAndUpload() {
    if (!Diktat.recording) return;
    const { blob, durationSec } = _diktatFinish();
    toggle.disabled = true;
    toggle.textContent = "🎤 Aufnahme starten";
    statusEl.textContent = "Analysiere das Gespräch … (kann 30–60 Sek dauern)";
    if (durationSec < 1 || blob.size < 2000) {
      statusEl.textContent = "Aufnahme war zu kurz. Bitte erneut versuchen.";
      toggle.disabled = false;
      return;
    }
    let res;
    try {
      res = await fetch("/app/api/aufnahmen/diktat", {
        method: "POST",
        headers: {
          "X-CSRF-Token": App.me.csrf,
          "Content-Type": "audio/wav",
          "X-Audio-Duration": String(durationSec),
        },
        body: blob,
      });
    } catch (e) {
      statusEl.textContent = "Netzwerkfehler. Bitte erneut versuchen.";
      toggle.disabled = false;
      return;
    }
    if (res.status === 303 || res.status === 401 || res.redirected) {
      location.href = "/app/login"; return;
    }
    let j = null;
    try { j = await res.json(); } catch (e) {}
    if (res.ok && j && j.ok) {
      statusEl.textContent = "";
      const todos = (j.todos || []).length
        ? `<div class="card"><h2>To-dos</h2>${j.todos.map((t) => `<div class="row"><div>☐ ${esc(t)}</div></div>`).join("")}</div>` : "";
      resultEl.innerHTML =
        `<div class="card"><h2>✓ Gespeichert: ${esc(j.kunde)}</h2>` +
        (j.briefing ? `<p style="margin:6px 0 0">${esc(j.briefing)}</p>` : `<p class="muted" style="margin:6px 0 0">Kein Briefing erkannt.</p>`) +
        `</div>` + todos +
        `<button class="btn-sm" id="dk-open" style="width:100%;margin-top:4px">Zur Aufnahme</button>` +
        `<button class="btn-sm btn-ghost" id="dk-again" style="width:100%;margin-top:8px">Weiteres Gespräch diktieren</button>`;
      const open = document.getElementById("dk-open");
      if (open) open.addEventListener("click", () => showAufnahme(j.id));
      document.getElementById("dk-again").addEventListener("click", showDiktatForm);
      toggle.style.display = "none";
      timerEl.textContent = "0:00";
    } else {
      statusEl.textContent = (j && j.error) || "Konnte nicht verarbeiten. Bitte erneut versuchen.";
      toggle.disabled = false;
    }
  }

  toggle.addEventListener("click", async () => {
    if (Diktat.recording) { await stopAndUpload(); return; }
    resultEl.innerHTML = "";
    statusEl.textContent = "";
    try {
      await _diktatStartRecording();
    } catch (e) {
      statusEl.textContent = (e && e.name === "NotAllowedError")
        ? "Mikrofon-Zugriff wurde abgelehnt. Bitte in den Browser-Einstellungen erlauben."
        : "Mikrofon nicht verfügbar.";
      _diktatTeardown();
      return;
    }
    toggle.textContent = "⏹ Stoppen & analysieren";
    timerEl.textContent = "0:00";
    Diktat.tick = setInterval(() => {
      const s = Math.round((Date.now() - Diktat.startTs) / 1000);
      timerEl.textContent = fmtT(s);
    }, 500);
    Diktat.autostop = setTimeout(() => {
      if (Diktat.recording) stopAndUpload();
    }, DIKTAT_MAX_SECONDS * 1000);
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
      btns.push(`<span class="sub">Rechnung über „Rechnungen" senden</span>`);
    }
    btns.push(`<button class="btn-sm btn-ghost" data-auftrag="${esc(a.id)}" data-status="abgebrochen" style="padding:6px 10px">Abbrechen</button>`);
    actions = `<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;align-items:center">${btns.join("")}</div>`;
  }
  return `<div class="card">
    <div class="row"><div><div><b>${esc(a.kunde)}</b></div><div class="sub">${esc(a.betrag)}${esc(progress)} · ${esc(a.zeit)}</div></div>
    <span class="pill ${pill}">${esc(a.status_label)}</span></div>
    ${actions}
  </div>`;
}

function bindAuftragActions() {
  document.querySelectorAll("[data-auftrag]").forEach((b) =>
    b.addEventListener("click", async () => {
      const status = b.dataset.status;
      if (status === "abgebrochen" && !confirm("Auftrag wirklich abbrechen?")) return;
      b.disabled = true;
      const res = await api("/app/api/auftraege/" + encodeURIComponent(b.dataset.auftrag) + "/status",
        { method: "POST", body: JSON.stringify({ status }) });
      if (res && res.ok) {
        const j = await res.json();
        if (j.ok) { showAuftraege(); return; }
        alert(j.error || "Konnte Status nicht setzen.");
      } else {
        alert("Konnte Status nicht setzen.");
      }
      b.disabled = false;
    }));
}

async function showAuftraege() {
  App.view.innerHTML = `<div class="loading">Lädt …</div>`;
  const res = await api("/app/api/auftraege");
  const d = res && res.ok ? await res.json() : { auftraege: [] };
  const isInhaber = App.me.employee.is_inhaber;
  const list = (d.auftraege || []).map((a) => auftragCard(a, isInhaber)).join("");
  App.view.innerHTML =
    `<button class="btn-sm btn-ghost" id="back-buchhaltung" style="margin-bottom:10px">← Zurück</button>` +
    `<h1 style="font-size:22px;margin:4px 4px 14px">Aufträge</h1>` +
    (list || `<div class="card">${emptyRow("Keine laufenden Aufträge")}</div>`);
  document.getElementById("back-buchhaltung").addEventListener("click", () => navigate("buchhaltung"));
  if (isInhaber) bindAuftragActions();
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
       <input type="file" id="bl-file" accept="image/jpeg,image/png,application/pdf" style="${inputStyle}" />
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
        ? `<a class="btn-sm" href="${esc(j.lexware_link)}" target="_blank" rel="noopener" style="display:block;text-align:center;width:100%;margin-top:4px;text-decoration:none">In Lexware öffnen & verbuchen</a>` : "";
      resultEl.innerHTML =
        `<div class="card"><h2>✓ Beleg übergeben</h2>${dup}</div>` + link +
        `<button class="btn-sm btn-ghost" id="bl-again" style="width:100%;margin-top:8px">Nächsten Beleg</button>`;
      document.getElementById("bl-again").addEventListener("click", showBelegUpload);
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
  document.getElementById("back-anrufe").addEventListener("click", () => navigate("anrufe"));
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
      if (j.ok) { navigate("anrufe"); return; }
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
