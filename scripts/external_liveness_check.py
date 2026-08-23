#!/usr/bin/env python3
"""External Liveness-Check fuer das Framework + Postgres.

Laeuft auf dem HOST per Cron (nicht im Framework-Container — sonst
Henne-Ei wenn Framework crasht). Prueft alle 5 min:

  1. HTTP /health gegen den Framework-Container
  2. DB-SELECT 1 gegen Postgres
  3. Bei 3 aufeinanderfolgenden Fehlern UND > 1h seit letztem Alert:
     Alarm (siehe _alarm() — aktuell nur Log, siehe unten).
  4. Wenn vorher 'down' war und jetzt wieder 'ok': Recovery-Meldung.

Der Alarm geht per Stdlib-smtplib ueber ein FREMDES Postfach raus
(ALERT_SMTP_* aus der Umgebung oder der .env) — bewusst nicht ueber die
eigene Mail-Pipeline, denn die kann selbst der Ausfallgrund sein. Ohne
SMTP-Konfiguration bleibt es beim stderr-Eintrag im Log.

State-Datei: /tmp/gewerbeagent-liveness-state.json
   { "framework": {"consecutive_failures": N, "last_alert_at": "ISO",
                   "status": "ok"|"down"},
     "db":        {... idem ...} }

Cron-Setup auf dem Host:
   */5 * * * * /opt/gewerbeagent/framework/scripts/external_liveness_check.py \\
       >> /var/log/gewerbeagent-liveness.log 2>&1

Exit-Codes:
   0  alle Checks ok (oder Alert sauber zugestellt)
   1  unerwarteter Skript-Fehler

Konfiguration via Env (alle optional):
   FRAMEWORK_URL          default https://gewerbeagent.de/health
   POSTGRES_CONTAINER     default gewerbeagent_postgres
   POSTGRES_USER          default gewerbeagent
   POSTGRES_DB            default gewerbeagent
   STATE_FILE             default /tmp/gewerbeagent-liveness-state.json
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Absichtlich die OEFFENTLICHE Adresse: der framework-Container hat gar
# keinen Host-Port (er haengt nur im Docker-Netz hinter Caddy), ein
# localhost:8001 waere also ein Dauer-Fehlalarm. Ueber die echte URL
# wird ausserdem die ganze Kette geprueft, die auch ein Kunde benutzt —
# DNS, Caddy, TLS, App.
FRAMEWORK_URL = os.environ.get(
    "FRAMEWORK_URL", "https://gewerbeagent.de/health",
)
POSTGRES_CONTAINER = os.environ.get("POSTGRES_CONTAINER", "gewerbeagent_postgres")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "gewerbeagent")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "gewerbeagent")
STATE_FILE = Path(os.environ.get(
    "STATE_FILE", "/tmp/gewerbeagent-liveness-state.json",
))
HTTP_TIMEOUT_SECONDS = 10

# Alert-Schwellen
FAILURES_BEFORE_ALERT = 3       # 3 * 5min = 15 min downtime bis Alert
ALERT_COOLDOWN_HOURS = 1


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception as e:
        print(f"WARN: state-file unleserlich, starte frisch: {e}",
              file=sys.stderr)
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, default=str, indent=2))
    except Exception as e:
        print(f"WARN: state-file nicht schreibbar: {e}", file=sys.stderr)


def check_framework() -> tuple[bool, str]:
    """True wenn HTTP 200, sonst False + Grund."""
    try:
        with urllib.request.urlopen(
            FRAMEWORK_URL, timeout=HTTP_TIMEOUT_SECONDS,
        ) as resp:
            if resp.status == 200:
                return True, "200 OK"
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"URLError: {e.reason}"
    except Exception as e:
        return False, f"Exception: {type(e).__name__}: {e}"


def check_db() -> tuple[bool, str]:
    """True wenn `SELECT 1` durchgeht, sonst False + Grund."""
    try:
        result = subprocess.run(
            [
                "docker", "exec", POSTGRES_CONTAINER,
                "psql", "-U", POSTGRES_USER, "-d", POSTGRES_DB,
                "-tAc", "SELECT 1",
            ],
            capture_output=True, text=True, timeout=HTTP_TIMEOUT_SECONDS,
        )
        if result.returncode == 0 and result.stdout.strip() == "1":
            return True, "1"
        return False, (
            f"rc={result.returncode} "
            f"stderr={result.stderr.strip()[:200]}"
        )
    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except Exception as e:
        return False, f"Exception: {type(e).__name__}: {e}"


def _alarm(message: str) -> bool:
    """Alarm ausgeben — moeglichst ueber ein fremdes Postfach.

    Dieses Skript laeuft auf dem HOST, damit es auch dann noch meldet,
    wenn der Container tot ist. Genau deshalb importiert es NICHTS aus
    `core/` und verschickt per Stdlib-smtplib ueber ein fremdes Postfach
    (dieselben ALERT_SMTP_*-Werte wie die App, hier aus der Umgebung
    bzw. der .env gelesen).

    Ist kein SMTP hinterlegt, bleibt es beim stderr-Eintrag — der geht
    ueber Cron nur dann an jemanden, wenn MAILTO auf dem Host steht.
    """
    print(f"ALARM: {message}", file=sys.stderr)

    host = _env("ALERT_SMTP_HOST")
    empfaenger = _env("ALERT_SMTP_TO") or _env("HEALTH_ALERT_EMAIL")
    if not host or not empfaenger:
        return False

    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = "[Gewerbeagent] Liveness-Alarm"
    msg["From"] = _env("ALERT_SMTP_FROM") or _env("ALERT_SMTP_USER") or empfaenger
    msg["To"] = empfaenger
    msg.set_content(message)
    port = int(_env("ALERT_SMTP_PORT") or "587")
    benutzer = _env("ALERT_SMTP_USER")
    passwort = _env("ALERT_SMTP_PASSWORD")
    try:
        if (_env("ALERT_SMTP_STARTTLS") or "true").lower() != "false":
            with smtplib.SMTP(host, port, timeout=20) as srv:
                srv.starttls()
                if benutzer:
                    srv.login(benutzer, passwort)
                srv.send_message(msg)
        else:
            with smtplib.SMTP_SSL(host, port, timeout=20) as srv:
                if benutzer:
                    srv.login(benutzer, passwort)
                srv.send_message(msg)
        print("ALARM per Mail zugestellt.", file=sys.stderr)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"ALARM-Mail fehlgeschlagen: {exc}", file=sys.stderr)
        return False


def _env(name: str) -> str:
    """Wert aus der Umgebung, sonst aus der .env des Projekts.

    Der Host-Cron hat die .env nicht geladen, deshalb wird sie hier
    einmal selbst gelesen — ohne Fremdbibliothek.
    """
    import os
    wert = os.environ.get(name)
    if wert:
        return wert.strip()
    pfad = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), ".env")
    try:
        with open(pfad, encoding="utf-8") as f:
            for zeile in f:
                zeile = zeile.strip()
                if not zeile or zeile.startswith("#") or "=" not in zeile:
                    continue
                schluessel, _, w = zeile.partition("=")
                if schluessel.strip() == name:
                    return w.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _evaluate_component(
    *, name: str, label: str, ok: bool, reason: str, state: dict,
) -> None:
    """Update state + ggf. Alert/Recovery senden.

    State pro Komponente:
        consecutive_failures: int
        last_alert_at:        ISO str | None
        status:               "ok" | "down"
    """
    now = _now()
    comp = state.setdefault(name, {
        "consecutive_failures": 0,
        "last_alert_at": None,
        "status": "ok",
    })
    prev_status = comp.get("status", "ok")

    if ok:
        # Recovery-Branch: war vorher 'down' → einmal "wieder ok" pushen.
        if prev_status == "down":
            _alarm(
                f"{label} wieder online "
                f"({_now().isoformat(timespec='seconds')})"
            )
            comp["status"] = "ok"
        comp["consecutive_failures"] = 0
        print(f"OK   {label}: {reason}")
        return

    # Failure-Branch
    comp["consecutive_failures"] = comp.get("consecutive_failures", 0) + 1
    n = comp["consecutive_failures"]
    print(f"FAIL {label}: {reason} (consecutive={n})", file=sys.stderr)

    if n < FAILURES_BEFORE_ALERT:
        return

    # Schwelle erreicht — Cooldown pruefen
    last_alert_str = comp.get("last_alert_at")
    if last_alert_str:
        try:
            last_alert = dt.datetime.fromisoformat(last_alert_str)
            age_hours = (now - last_alert).total_seconds() / 3600
            if age_hours < ALERT_COOLDOWN_HOURS:
                print(
                    f"  → Alert unterdrueckt (cooldown {age_hours:.2f}h)",
                    file=sys.stderr,
                )
                return
        except Exception:
            pass

    sent = _alarm(
        f"{label} antwortet nicht — seit {n} Checks (= ~{n * 5} min). "
        f"Letzter Grund: {reason} "
        f"({now.isoformat(timespec='seconds')})"
    )
    if sent:
        comp["last_alert_at"] = now.isoformat()
        comp["status"] = "down"


def main() -> int:
    state = _load_state()

    fw_ok, fw_reason = check_framework()
    db_ok, db_reason = check_db()

    _evaluate_component(
        name="framework", label="Framework",
        ok=fw_ok, reason=fw_reason, state=state,
    )
    _evaluate_component(
        name="db", label="Postgres",
        ok=db_ok, reason=db_reason, state=state,
    )

    _save_state(state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"FATAL: liveness-check crashed: {e}", file=sys.stderr)
        sys.exit(1)
