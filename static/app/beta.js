/* =====================================================================
   Beta-Zusatzskript. Laeuft NUR auf /app/beta, direkt nach app.js.
   Aendert nichts am Original — app.js bleibt Zeile fuer Zeile gleich, wir
   haengen uns nur von aussen dran.

   Zustaendig fuer:
     1. theme-color der Browserleiste (app.js setzt sie auf die Markenfarbe,
        was zum hellen Beta-Header nicht passt)
     2. Hell/Dunkel/Automatisch als Nutzer-Einstellung
     3. den Umschalter dafuer im Einstellungen-Screen
   ===================================================================== */
(function () {
  "use strict";

  var STORAGE_KEY = "ga-theme";
  var MODES = ["auto", "light", "dark"];

  function currentMode() {
    var m = document.documentElement.getAttribute("data-theme");
    return MODES.indexOf(m) >= 0 ? m : "auto";
  }

  function isDark() {
    var m = currentMode();
    if (m === "dark") return true;
    if (m === "light") return false;
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  }

  // app.js setzt theme-color hart auf die Tenant-Markenfarbe (applyBrandColor).
  // Im Beta-Design ist die Kopfzeile aber hell bzw. dunkel — die Browserleiste
  // muss dazu passen, nicht zur Markenfarbe. Die Markenfarbe faerbt hier nur
  // noch Bedienelemente (--primary), und das lassen wir unangetastet.
  function syncThemeColor() {
    var meta = document.querySelector('meta[name="theme-color"]');
    if (!meta) return;
    // Wert kommt aus dem Stylesheet, damit Farbe nur an EINER Stelle steht.
    var css = getComputedStyle(document.documentElement)
      .getPropertyValue("--theme-color-meta").trim();
    var want = css || (isDark() ? "#12151a" : "#ffffff");
    // Nur schreiben wenn noetig — setAttribute loest auch bei gleichem Wert
    // einen Mutation-Record aus, und der Beobachter unten wuerde sich sonst
    // selbst im Kreis triggern.
    if (meta.getAttribute("content") !== want) meta.setAttribute("content", want);
  }

  // ---------- Lesbarkeit der Kopfzeile ----------
  // Die Markenfarbe ist ein freier Farbwaehler in den Einstellungen. Auf einem
  // dunklen Blau braucht die Leiste weisse Schrift, auf einem hellen Gelb
  // schwarze — sonst ist der Firmenname unlesbar. Wir rechnen die relative
  // Helligkeit aus (WCAG-Formel) und setzen --header-fg passend.
  function relLuminance(hex) {
    var m = /^#?([0-9a-f]{6})$/i.exec((hex || "").trim());
    if (!m) return 0;  // unbekannt -> wie dunkel behandeln (weisse Schrift)
    var n = parseInt(m[1], 16);
    var ch = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map(function (v) {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2];
  }

  function syncHeaderContrast() {
    var primary = getComputedStyle(document.documentElement)
      .getPropertyValue("--primary").trim();
    // Schwelle 0.45: darueber ist die Farbe hell genug, dass schwarze Schrift
    // besser liest als weisse.
    var fg = relLuminance(primary) > 0.45 ? "#10151f" : "#ffffff";
    // Nur schreiben wenn noetig: wir schreiben ins style-Attribut, und der
    // Beobachter unten lauscht auf genau dieses Attribut — sonst dreht er sich
    // im Kreis.
    if (document.documentElement.style.getPropertyValue("--header-fg") !== fg) {
      document.documentElement.style.setProperty("--header-fg", fg);
    }
  }

  function applyTheme(mode) {
    if (MODES.indexOf(mode) < 0) mode = "auto";
    document.documentElement.setAttribute("data-theme", mode);
    try { localStorage.setItem(STORAGE_KEY, mode); } catch (e) { /* Privatmodus */ }
    syncThemeColor();
    renderSegState();
  }

  // app.js schreibt in applyBrandColor() die Tenant-Markenfarbe in das
  // theme-color-Meta. Statt die Funktion zu ersetzen (das haengt davon ab, wie
  // sie im globalen Scope landet, und war in der Testumgebung schon nicht
  // greifbar) bewachen wir das Meta-Tag selbst: wer immer es aendert, wir
  // ziehen es auf den Wert zurueck, der zur Kopfzeile passt. Robust gegen
  // jeden Umbau in app.js.
  function guardThemeColor() {
    var meta = document.querySelector('meta[name="theme-color"]');
    if (!meta || !window.MutationObserver) return;
    new MutationObserver(syncThemeColor).observe(meta, {
      attributes: true, attributeFilter: ["content"],
    });
  }

  // Steht die Wahl auf "automatisch", muss ein Systemwechsel sofort durchschlagen.
  var mq = window.matchMedia("(prefers-color-scheme: dark)");
  var onSystemChange = function () { if (currentMode() === "auto") syncThemeColor(); };
  if (mq.addEventListener) mq.addEventListener("change", onSystemChange);
  else if (mq.addListener) mq.addListener(onSystemChange);

  // ---------- Umschalter im Einstellungen-Screen ----------
  var SEG = [
    { mode: "auto", label: "Automatisch" },
    { mode: "light", label: "Hell" },
    { mode: "dark", label: "Dunkel" },
  ];

  function renderSegState() {
    var seg = document.getElementById("beta-theme-seg");
    if (!seg) return;
    var m = currentMode();
    seg.querySelectorAll("button").forEach(function (b) {
      b.classList.toggle("active", b.dataset.mode === m);
    });
  }

  function buildThemeCard() {
    var card = document.createElement("div");
    card.className = "card";
    card.id = "beta-theme-card";
    card.innerHTML =
      '<h2>Darstellung</h2>' +
      '<div id="beta-theme-seg" class="theme-seg">' +
      SEG.map(function (s) {
        return '<button type="button" data-mode="' + s.mode + '">' + s.label + "</button>";
      }).join("") +
      "</div>" +
      '<p class="muted" style="font-size:13px;margin:10px 0 0">' +
      '„Automatisch" folgt der Einstellung deines Handys.</p>';
    card.querySelectorAll("button").forEach(function (b) {
      b.addEventListener("click", function () { applyTheme(b.dataset.mode); });
    });
    return card;
  }

  // ---------- Branding: Logo + Website in der Kopfzeile ----------
  var LOGO_URL = "/app/api/branding/logo";
  var LOGO_MAX = 512 * 1024;
  var branding = { website_url: null, has_logo: false, csrf: "" };

  // Zeigt nur die Domain statt der vollen URL — in einer Kopfzeile ist
  // "jantos.de" lesbar, "https://www.jantos.de/leistungen" nicht.
  function prettyHost(url) {
    try {
      return new URL(url).hostname.replace(/^www\./, "");
    } catch (e) { return url; }
  }

  function renderHeaderBrand() {
    var old = document.getElementById("beta-brand");
    if (old) old.remove();
    if (!branding.has_logo && !branding.website_url) return;

    var title = document.getElementById("hdr-title");
    if (!title) return;

    var wrap = document.createElement(branding.website_url ? "a" : "span");
    wrap.id = "beta-brand";
    wrap.className = "hdr-brand";
    if (branding.website_url) {
      wrap.href = branding.website_url;
      wrap.target = "_blank";
      wrap.rel = "noopener noreferrer";
      wrap.setAttribute("aria-label", "Website öffnen");
    }
    if (branding.has_logo) {
      var img = document.createElement("img");
      // Cache-Buster: nach einem Upload muss das neue Logo sofort erscheinen.
      img.src = LOGO_URL + "?v=" + Date.now();
      img.alt = "Logo";
      img.className = "hdr-logo";
      // Logo kaputt/geloescht? Dann lieber nichts zeigen als ein Bruchbild.
      img.addEventListener("error", function () { img.remove(); });
      wrap.appendChild(img);
    }
    // Die URL selbst bleibt unsichtbar: mit Logo ist das Logo der Link.
    // Nur ohne Logo zeigen wir die Domain als Text, sonst waere der Link
    // gar nicht erreichbar.
    if (branding.website_url && !branding.has_logo) {
      var span = document.createElement("span");
      span.className = "hdr-site";
      span.textContent = prettyHost(branding.website_url);
      wrap.appendChild(span);
    }
    // Direkt hinter den Firmennamen.
    title.insertAdjacentElement("afterend", wrap);
  }

  function loadBranding() {
    return fetch("/app/api/me", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (me) {
        if (!me || !me.tenant) return;
        branding.website_url = me.tenant.website_url || null;
        branding.has_logo = !!me.tenant.has_logo;
        branding.csrf = me.csrf || "";
        // Merken, damit maybeInject() weiss, ob es die Branding-Karte zeigen darf.
        document.body.dataset.inhaber =
          (me.employee && me.employee.is_inhaber) ? "1" : "0";
        renderHeaderBrand();
      })
      .catch(function () { /* Kopfzeile bleibt eben ohne Branding */ });
  }

  // ---------- Branding-Karte in den Einstellungen ----------
  function buildBrandingCard() {
    var card = document.createElement("div");
    card.className = "card";
    card.id = "beta-brand-card";
    card.innerHTML =
      '<h2>Logo &amp; Website</h2>' +
      '<div class="brand-row">' +
      '  <div id="brand-logo-prev" class="brand-logo-prev"></div>' +
      '  <div style="flex:1;min-width:0">' +
      '    <input type="file" id="brand-logo-file" accept="image/png,image/jpeg,image/webp" hidden>' +
      '    <button type="button" class="btn-sm btn-ghost" id="brand-logo-btn">Logo wählen</button>' +
      '    <button type="button" class="btn-sm btn-ghost" id="brand-logo-del" hidden>Entfernen</button>' +
      '    <p class="muted" style="font-size:12px;margin:8px 0 0">PNG, JPEG oder WEBP, max 512 KB.</p>' +
      "  </div>" +
      "</div>" +
      '<label class="muted" style="font-size:13px;display:block;margin:14px 0 4px">Website</label>' +
      '<input type="text" id="brand-site" placeholder="jantos.de" maxlength="300" style="width:100%">' +
      '<button type="button" class="btn-sm" id="brand-save" style="margin-top:10px">Website speichern</button>' +
      '<p class="msg" id="brand-msg" style="font-size:13px"></p>';

    var msg = card.querySelector("#brand-msg");
    var prev = card.querySelector("#brand-logo-prev");
    var delBtn = card.querySelector("#brand-logo-del");
    var file = card.querySelector("#brand-logo-file");
    var site = card.querySelector("#brand-site");
    site.value = branding.website_url || "";

    function paintPreview() {
      prev.innerHTML = branding.has_logo
        ? '<img src="' + LOGO_URL + "?v=" + Date.now() + '" alt="Logo">'
        : '<span class="muted" style="font-size:11px">kein Logo</span>';
      delBtn.hidden = !branding.has_logo;
    }
    paintPreview();

    card.querySelector("#brand-logo-btn").addEventListener("click", function () {
      file.click();
    });

    file.addEventListener("change", function () {
      var f = file.files && file.files[0];
      if (!f) return;
      if (f.size > LOGO_MAX) {
        msg.textContent = "Logo zu groß (" + Math.round(f.size / 1024) + " KB, max 512 KB).";
        file.value = "";
        return;
      }
      msg.textContent = "Lädt hoch …";
      fetch(LOGO_URL, {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": branding.csrf, "Content-Type": f.type || "image/png" },
        body: f,
      }).then(function (r) {
        return r.json().catch(function () { return { ok: r.ok }; });
      }).then(function (j) {
        file.value = "";
        if (!j || !j.ok) {
          msg.textContent = (j && j.error) || "Upload fehlgeschlagen.";
          return;
        }
        branding.has_logo = true;
        msg.textContent = "Logo gespeichert.";
        paintPreview();
        renderHeaderBrand();
      }).catch(function () { msg.textContent = "Upload fehlgeschlagen."; });
    });

    delBtn.addEventListener("click", function () {
      fetch(LOGO_URL, {
        method: "DELETE",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": branding.csrf },
      }).then(function () {
        branding.has_logo = false;
        msg.textContent = "Logo entfernt.";
        paintPreview();
        renderHeaderBrand();
      }).catch(function () { msg.textContent = "Konnte nicht entfernen."; });
    });

    card.querySelector("#brand-save").addEventListener("click", function () {
      var val = site.value.trim();
      msg.textContent = "Speichert …";
      fetch("/app/api/einstellungen", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": branding.csrf, "Content-Type": "application/json" },
        body: JSON.stringify({ website_url: val }),
      }).then(function (r) {
        return r.json().catch(function () { return { ok: r.ok }; });
      }).then(function (j) {
        if (!j || !j.ok) {
          msg.textContent = (j && j.error) || "Speichern fehlgeschlagen.";
          return;
        }
        // Der Server normalisiert ("jantos.de" -> "https://jantos.de") —
        // wir holen den echten Wert zurueck statt zu raten.
        msg.textContent = "Website gespeichert.";
        loadBranding().then(function () { site.value = branding.website_url || ""; });
      }).catch(function () { msg.textContent = "Speichern fehlgeschlagen."; });
    });

    return card;
  }

  // app.js rendert die Screens per innerHTML in #view; wir haengen die
  // Karten nach jedem Rendern an die passende Einstellungs-Unterseite an
  // (seit der Aufteilung in Unterseiten: Darstellung -> App-Einstellungen,
  // Logo/Website -> Betrieb). Welcher Screen offen ist, verraet der Hash —
  // an App.current kommen wir von aussen nicht ran, das ist eine
  // lexikalische const in app.js.
  function maybeInject() {
    var view = document.getElementById("view");
    if (!view || view.querySelector(".loading")) return;  // Screen laedt noch
    if (location.hash === "#einstellungen_app") {
      if (document.getElementById("beta-theme-card")) return;
      view.appendChild(buildThemeCard());
      renderSegState();
    } else if (location.hash === "#einstellungen_betrieb") {
      // Logo/Website nur fuer den Inhaber — die Endpunkte sind ohnehin
      // inhaber-only, ein Monteur bekaeme nur eine 403-Meldung zu sehen.
      if (document.body.dataset.inhaber !== "1") return;
      if (document.getElementById("beta-brand-card")) return;
      view.appendChild(buildBrandingCard());
    }
  }

  var view = document.getElementById("view");
  if (view) new MutationObserver(maybeInject).observe(view, { childList: true });
  window.addEventListener("hashchange", maybeInject);

  // app.js setzt --primary per style-Attribut auf <html> (applyBrandColor) —
  // beim Start und erneut, wenn der Nutzer die Farbe in den Einstellungen
  // speichert. Wir hoeren auf genau diese Aenderung und rechnen die Schrift-
  // farbe der Kopfzeile neu aus.
  if (window.MutationObserver) {
    new MutationObserver(syncHeaderContrast).observe(document.documentElement, {
      attributes: true, attributeFilter: ["style"],
    });
  }

  // Beim Start: das Theme hat das Inline-Skript im <head> schon gesetzt (kein
  // Aufblitzen), hier ziehen wir die theme-color nach und stellen die Wache auf.
  syncThemeColor();
  guardThemeColor();
  syncHeaderContrast();
  loadBranding().then(maybeInject);  // Branding holen, dann ggf. Karte nachziehen
})();
