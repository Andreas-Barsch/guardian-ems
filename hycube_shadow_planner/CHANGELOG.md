# Hycube Notstromplaner

## 0.5.1 — zur Veröffentlichung vorbereitet, 2026-10-28

Erste separate Quellübernahme in guardian-ems; kein GitHub-Release und kein
Deployment durch diese Änderung.

- Enthält die vorhandene 24-h-Notstromplanung mit 72-h-Prognose, rollender
  Reserve und historischer Balancingbewertung V1.1.
- Getrenntes kausales Solarlernen für AB1 und AB2 mit initial sieben Tagen
  Faktor 0,8; AB2 und Netz bleiben im Inselmodell null.
- Zwei feste HA-Anzeigesensoren; zusätzlich je Anlage errechneter und wirksamer
  Faktor. Keine Geräteaktionen und keine HA-Serviceaufrufe.
- Versionsmetadaten auf 0.5.1 abgestimmt, portable Anleitung und unbestätigte
  Konfigurationsvorlage ergänzt. Private Betriebsdaten nicht übernommen.
- Reproduzierbares Quell-Zipapp und zusätzliche Rollen-/Paketprüfungen.

Die Python-Planermodule und der Add-on-Einstieg wurden gegenüber dem übergebenen
0.5.1-Stand nicht funktional geändert. Ältere lokale Berichte und Releasepakete
bleiben außerhalb dieser Quellübernahme erhalten.
