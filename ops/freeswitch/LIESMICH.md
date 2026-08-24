# FreeSWITCH-Konfiguration

Die Konfiguration liegt im Docker-Volume `framework_freeswitch_conf`, nicht
im Repo — ein frisches Volume bringt deshalb wieder die Werkseinstellung des
Images mit. Das ist der Grund fuer diese Vorlage.

## event_socket.conf.xml

Gehaertet am **2026-08-25** (Audit 2 vom 24.08.). Vorher stand dort die
Werkseinstellung: `listen-ip "::"` (alle Adressen, auch die oeffentliche),
Passwort `ClueCon` (der FreeSWITCH-Default) und `apply-inbound-acl`
auskommentiert. Nach aussen hat allein die ufw-Regel das abgedeckt; wer im
Host-Netz Fuss fasst, haette die volle FreeSWITCH-Konsole gehabt — und der
Container laeuft `privileged`.

Jetzt: nur `127.0.0.1`, Loopback-ACL, Zufallspasswort aus
`FREESWITCH_ESL_PASSWORD` in der `.env`, und `stop-on-bind-error`, damit ein
fehlgeschlagenes Binden auffaellt statt still zu bleiben.

**Nach einem Volume-Neubau von Hand nachziehen:**

```bash
PW="$(grep '^FREESWITCH_ESL_PASSWORD=' /opt/gewerbeagent/framework/.env | cut -d= -f2)"
sed "s/__AUS_ENV_FREESWITCH_ESL_PASSWORD__/$PW/" \
    ops/freeswitch/event_socket.conf.xml.vorlage > \
    /var/lib/docker/volumes/framework_freeswitch_conf/_data/autoload_configs/event_socket.conf.xml
docker exec gewerbeagent_freeswitch fs_cli -H 127.0.0.1 -p "$PW" -x "reload mod_event_socket"
ss -lntp | grep 8021      # muss 127.0.0.1:8021 zeigen, nicht *:8021
```

## Offen

`privileged: true` in `docker-compose.freeswitch.yml` ist bewusst noch
drin: ein Test ohne die Berechtigung braucht einen Container-Neustart und
damit ein kurzes Fenster ohne Telefonie (sipgate-Registrierung). Sobald es
ein Wartungsfenster gibt: entfernen, starten, `sofia status` pruefen —
`external::sipgate` muss wieder `REGED` sein.
