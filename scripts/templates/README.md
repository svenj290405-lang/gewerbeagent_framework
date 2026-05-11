# Branchen-Templates (Phase B10)

Jede `branche_<key>.json` enthaelt Wissensbasis-Defaults fuer eine
Branche. Beim Onboarding mit `--branche=<key>` werden die Eintraege
als `TenantKnowledge`-Zeilen in die DB geschrieben — der Tenant kann
sie spaeter via `/wissen` anpassen oder loeschen.

Format:
```json
{
  "key": "tischler",
  "label": "Tischlerei / Schreinerei",
  "knowledge": [
    {"kategorie": "leistungen", "text": "..."},
    {"kategorie": "anfahrt", "text": "..."}
  ]
}
```

Kategorien siehe `core/models/tenant_knowledge.py` (`ALLE_KATEGORIEN`).

Wenn eine Branche noch kein Template hat, faellt `--branche` auf "kein
Template" zurueck — der Tenant startet mit leerer Wissensbasis (heute-
Default).
