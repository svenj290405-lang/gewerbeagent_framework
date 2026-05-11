"""Oeffentliche Status-Page (Phase B6).

Drei Routes:
  GET /api/status        JSON-Aggregat ueber Framework + DB + Crons
  GET /status            statische HTML-Page die /api/status pollt
  GET /                  (nur auf status.gewerbeagent.de gemounted, siehe
                          unten) redirected zu /status

Bewusst KEIN Tenant-Detail, KEINE Failure-Reasons im Klartext —
oeffentlich erreichbar, daher minimaler Recon-Wert.

Wird in core/api/app.py via include_router eingebunden. Subdomain-
Trennung passiert auf Caddy-Ebene: status.gewerbeagent.de proxy zu
diesem selben Framework-Container, aber Caddy filtert auf die
Status-Paths.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text

from core.database.connection import get_session

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/status")
async def public_status() -> JSONResponse:
    """JSON-Snapshot des Framework-Health-Status.

    Was hier sichtbar wird:
      - status: ok | degraded
      - framework: ok (wenn dieser Endpoint antwortet, ist's per Definition ok)
      - db: ok | down
      - crons: { name: ok | stale } — KEINE Details (Toleranz-Werte, Last-Heartbeat)
    """
    body: dict = {
        "status": "ok",
        "framework": "ok",
        "db": "ok",
        "crons": {},
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    # DB-Check
    try:
        async with get_session() as s:
            await s.execute(text("SELECT 1"))
    except Exception:
        body["db"] = "down"
        body["status"] = "degraded"

    # Cron-Heartbeat-Status
    try:
        from core.integrations.cron_health import get_health_report
        report = get_health_report()
        for name, info in (report.get("crons") or {}).items():
            body["crons"][name] = "ok" if info.get("alive") else "stale"
        if report.get("status") != "ok":
            body["status"] = "degraded"
    except Exception:
        # Defensive — wenn cron_health crasht, sehen wir trotzdem db/framework
        body["crons"] = {"unknown": "stale"}

    return JSONResponse(body)


_STATUS_HTML = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <title>Gewerbeagent Status</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex, nofollow">
  <style>
    :root {
      --bg:#0a0d14; --card:#11151f; --border:#1f2533;
      --ink:#e8ebf2; --ink-muted:#7a8499;
      --green:#16c79a; --yellow:#f5a524; --red:#e5484d;
    }
    body { margin:0; font-family: 'Inter Tight', -apple-system, BlinkMacSystemFont, sans-serif;
           background:var(--bg); color:var(--ink); padding:48px 24px; }
    .wrap { max-width:680px; margin:0 auto; }
    h1 { font-size:42px; font-weight:600; margin:0 0 8px; letter-spacing:-0.02em; }
    .sub { color:var(--ink-muted); margin-bottom:32px; }
    .card { background:var(--card); border:1px solid var(--border);
            border-radius:16px; padding:24px; margin-bottom:16px; }
    .indicator { display:flex; align-items:center; gap:12px; }
    .dot { width:14px; height:14px; border-radius:50%; }
    .dot.ok { background:var(--green); box-shadow:0 0 0 4px rgba(22,199,154,0.16); }
    .dot.degraded { background:var(--yellow); box-shadow:0 0 0 4px rgba(245,165,36,0.16); }
    .dot.down { background:var(--red); box-shadow:0 0 0 4px rgba(229,72,77,0.16); }
    .pill-status { font-size:22px; font-weight:600; }
    .components { display:grid; gap:8px; margin-top:12px; }
    .row { display:flex; justify-content:space-between; padding:10px 12px;
           background:rgba(255,255,255,0.02); border-radius:8px; }
    .label { font-weight:500; }
    .state-ok { color:var(--green); }
    .state-down, .state-stale { color:var(--yellow); }
    .footer { color:var(--ink-muted); font-size:13px; margin-top:32px; text-align:center; }
    a { color:var(--ink); }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Gewerbeagent Status</h1>
    <p class="sub" id="last-checked">Wird geladen …</p>

    <div class="card">
      <div class="indicator">
        <span id="overall-dot" class="dot ok"></span>
        <span id="overall-text" class="pill-status">Alles laeuft</span>
      </div>
    </div>

    <div class="card">
      <div class="label" style="margin-bottom:12px;">Komponenten</div>
      <div class="components" id="components">
        <div class="row"><span>Lade …</span></div>
      </div>
    </div>

    <p class="footer">
      Diese Seite zeigt den aktuellen Live-Status der Gewerbeagent-Plattform.
      Bei Stoerungen: <a href="mailto:hallo@gewerbeagent.de">hallo@gewerbeagent.de</a>
    </p>
  </div>

<script>
async function refresh() {
  try {
    const r = await fetch('/api/status', { cache: 'no-store' });
    const data = await r.json();
    const dot = document.getElementById('overall-dot');
    const text = document.getElementById('overall-text');
    const lc = document.getElementById('last-checked');
    if (data.status === 'ok') {
      dot.className = 'dot ok';
      text.textContent = 'Alles laeuft';
    } else {
      dot.className = 'dot degraded';
      text.textContent = 'Teilweise eingeschraenkt';
    }
    lc.textContent = 'Letzte Pruefung: ' + new Date(data.checked_at).toLocaleString('de-DE');
    const comps = document.getElementById('components');
    comps.innerHTML = '';
    function row(name, state) {
      const cls = state === 'ok' ? 'state-ok' : (state === 'down' ? 'state-down' : 'state-stale');
      const label = state === 'ok' ? 'OK' : (state === 'down' ? 'Down' : 'Verzoegert');
      comps.innerHTML += '<div class="row"><span class="label">' + name + '</span>' +
                        '<span class="' + cls + '">' + label + '</span></div>';
    }
    row('Framework', data.framework);
    row('Datenbank', data.db);
    for (const [name, st] of Object.entries(data.crons || {})) {
      row('Cron · ' + name, st);
    }
  } catch (e) {
    document.getElementById('overall-dot').className = 'dot down';
    document.getElementById('overall-text').textContent = 'Status nicht abrufbar';
  }
}
refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>"""


@router.get("/status", response_class=HTMLResponse)
async def status_page() -> HTMLResponse:
    """Statische HTML-Status-Page. Polled /api/status alle 30s."""
    return HTMLResponse(content=_STATUS_HTML)
