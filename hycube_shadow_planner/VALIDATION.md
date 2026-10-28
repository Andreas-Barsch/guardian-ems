# Veröffentlichungsvorbereitung 0.5.1 — 2026-10-28

Geprüft in einer separaten guardian-ems-Kopie auf Basis von main
`eede2dd88fb21e454b02b220f60573405e14891e`, ausschließlich im neuen
Ordner `hycube_shadow_planner`.

- Python 3.13.0: **103 Tests bestanden in 0,617 s**. Enthalten sind die
  97 übergebenen Tests und sechs neue Distributions-/Rollenprüfungen.
- Für diesen Lauf wurden Token-/Passwort-/Secret-Umgebungsvariablen entfernt
  und Socket-Verbindungsaufbau gesperrt. Netzwerkaufrufe in Rollentests sind
  Fakes. Es wurden keine HA-/Guardian-/Hardwareabfragen ausgeführt.
- Die neuen Tests prüfen Versionsgleichheit, unbestätigte Vorlagen,
  bytegleiche Zipapp-Erzeugung, ausschließlich Quellmodule im Paket,
  getrennte Leserrollen und die zwei festen Anzeigeschreibziele.
- Das erzeugte Zipapp wurde zusätzlich mit synthetischer Demo und gesperrtem
  Netzwerk ausgeführt: erfolgreich. Paket und Ergebnisse liegen außerhalb
  des Repositorys und werden nicht veröffentlicht.
- Python-Syntax (AST), JSON und TOML erfolgreich geprüft.
- Planermodule und Add-on-Einstieg sind bytegleich zur übergebenen Quelle.
  Version, Beschreibung, Veröffentlichungshinweise, Vorlagen und zusätzliche
  Paketierung sind getrennte Veröffentlichungsanpassungen.
- Keine Fundstellen beim Mustercheck auf private IPs, persönliche Pfade,
  reale Modulseriennummern und gängige Geheimnisformate. Zusätzlich keine
  Übereinstimmung mit den 30 aus der privaten Live-Konfiguration abgeleiteten
  Quell-/Site-/Modulkennungen in den zur Übernahme vorgesehenen Dateien.

Reproduzierbarer Testaufruf im Komponentenordner:

```sh
python3 -B - <<'PY'
import os, socket, unittest
for key in list(os.environ):
    if any(term in key.upper() for term in ('TOKEN', 'PASSWORD', 'SECRET', 'API_KEY')):
        os.environ.pop(key, None)
def no_network(*args, **kwargs):
    raise AssertionError('Network access prohibited during release tests')
socket.socket.connect = no_network
socket.create_connection = no_network
result = unittest.TextTestRunner().run(unittest.defaultTestLoader.discover('tests'))
raise SystemExit(not result.wasSuccessful())
PY
```

Grenzen: keine neue produktive Abnahme, kein Containerbau, keine Installation,
kein Neustart und kein Test der automatischen Wiederaufnahme. Die vorhandene
HA-Hintergrundinstallation wurde nicht verändert. Python 3.11/3.12 sind als
Ziel angegeben, dieser Testlauf verwendete 3.13.0. Die lokale Laufzeitprüfung
ersetzt keine spätere Add-on-/Ingressabnahme.
