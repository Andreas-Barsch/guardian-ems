# Hycube Notstromplaner 0.5.1

Separater Python-Dienst für Notstromberechnungen, historische Balancingbewertung
und eine begrenzte Home-Assistant-Anzeige. Er verändert weder Hycube-Parameter
noch Geräte, Ladefreigaben, Batterieschutz, Guardian-Erfassung oder RS485.
Die Quellenleser verwenden nur freigegebene GET-Routen beziehungsweise bestehende
Guardian-Dateien. Optional schreibt der Anzeigepublisher ausschließlich zwei
fest definierte Sensorzustände über HA `POST /api/states/...`; keine HA-Services.

Dieser Quellstand wird separat zur Prüfung bereitgestellt, ohne GitHub-Release
oder neue Installation. Die Übergabe meldet einen bereits laufenden separaten
HA-Hintergrundprozess; das ist keine portable Add-on-/Containerabnahme.

## Modell und Anzeige

- Hauptziel: 24 Stunden Notstrom bei konfigurierten 300 W Hauslast, mit 72 Stunden
  Prognose. Zusätzlich gelten modellierte 40 W Standby und je 0,92 Lade-/Entlade-
  Wirkungsgrad. Diese Annahmen sind konfigurierbar, keine Messung oder Garantie.
- Im Inselbetrieb liefert nur AB1; AB2 und Netz bleiben immer null. AB2 kann
  einen normalen Leistungsüberschuss anzeigen, aber keine Notstromenergie liefern.
- Batterieenergie ist eine lineare SOC-Schätzung oberhalb der technischen
  Schutzgrenze. Nennkapazität und Zuordnung müssen bestätigt werden; es wird
  keine gemessene Kapazität oder Energie aus gemitteltem Modul-SOC erfunden.
- Volllade-/Balancingbedarf V1.1 entsteht ausschließlich aus Historie. Pack-100-%-
  Meldung, Haltefenster und Zellziel werden getrennt beurteilt. Fehlende
  Temperatur-/Schutzinformationen sind keine Schutzfreigabe.
- Die interne bedingte Energieplanung berücksichtigt Reserve, PV-Zuteilung und
  gegebenenfalls historische Voll-/Halteziele im selben Energievorrat. Sie ist
  keine Normalverbrauchssimulation und kein globales Netzverbrauchsoptimum.
- Die rollende Reserve schützt auch einen bis zur nächsten Planung steigenden
  Bedarf. Die aktuelle Reichweite verwendet den aktuellen SOC. Ein
  prognosebegrenztes Ergebnis ist keine zugesicherte reale Versorgungsdauer.

Leichte Prüfung alle 60 s; vollständige Planung beim Start, nach 120 s
anhaltendem Defizit-/Überschusswechsel mit 50 W Hysterese, neuer Prognose,
Reservegrenze, Konfigurationsänderung, Datenwiederkehr und spätestens nach 3600 s.
Die Reserve betrachtet dieses Planungsintervall plus ein Prüfintervall.
Es werden keine zusätzlichen Solcast-API-Abrufe ausgelöst.

Die zwei dedizierten Anzeigezustände sind:

- `sensor.hycube_planung_notstromreservedauer`: Reichweite in Stunden.
- `sensor.hycube_planung_notstrom_24h`: benötigter Notstromanteil an der
  Nennenergie, auf 0,1 Prozentpunkte aufgerundet; kein absoluter SOC-Grenzwert.

Die [HA-Karte](deploy/PV_PROGNOSE_KARTE.yaml) zeigt zusätzlich je AB1/AB2
**Errechnet** und **Wirksam** aus dem Attribut `solar_factors`. Errechnet ist
`actual_kwh / raw_forecast_kwh`, bis zur Freigabe vorläufig, ohne gültige Stunden
ohne Zahlenwert. Wirksam ist der tatsächlich verwendete Faktor, initial 80 %.
Die Karte unterdrückt Zahlen nach 180 s ohne Aktualisierung. Veraltete Messungen
über 90 s und nicht lesbare Prognosen ergeben keine fortgeschriebenen Livezahlen.

## Lokale Prüfung ohne HA

Python **3.11 oder neuer**, Standardbibliothek; keine Laufzeitabhängigkeiten.
Alle folgenden Befehle starten im Ordner `hycube_shadow_planner`:

```sh
python3 -m unittest discover -s tests -q
python3 -m hycube_sim demo --output runs/demo
python3 tools/build_zipapp.py --output dist/hycube-shadow-0.5.1.pyz
python3 dist/hycube-shadow-0.5.1.pyz demo --output runs/demo-zip
```

Die Demo erzeugt synthetische Daten ohne Netzwerkzugriff. Das Zipapp enthält
ausschließlich die Python-Module und einen Startpunkt. Feste Zeitstempel und
sortierte Einträge machen es bei gleichem Quellstand bytegleich reproduzierbar.
`pyproject.toml` beschreibt alternativ ein installierbares Python-Paket;
dessen Buildwerkzeug benötigt setuptools >=68, der Dienst selbst keine Pakete.

## Konfiguration und vorhandene Daten

[examples/planner.example.json](examples/planner.example.json) ist eine
**unbestätigte Vorlage**, keine Live-Konfiguration. Sie enthält nur Platzhalter.
Vor Verwendung privat nach `planner.json` kopieren und Zuordnungen, Kapazität,
Schutzgrenze und Leistungsgrenzen prüfen. Beispielwerte sind keine Freigabe.

- `sources.home_assistant`: bekannte HA-Basisadresse und Name einer vorhandenen
  Token-Umgebungsvariable. Keine Tokens in URLs oder Repositorydateien ablegen.
  Der Leser unterstützt auch `token_file`; der optionale Anzeigepublisher
  benötigt derzeit ausdrücklich `token_env`.
- `sources.forecasts`: je Anlage HA-Solcast-Cache, eigene Site-ID, Metadaten-
  Entität, Prognosetage und Zeitpunkt der letzten erfolgreichen Prognose.
  AB1 und AB2 nicht vertauschen; keine direkte Solcast-Verbindung.
- `sources.measurements`: tatsächliche HA-Sensoren und deklarierte Einheiten
  für die vorhandenen Schutz-/Reserveanzeigen. Modulwerte sind keine
  automatische Batterieenergiekalibrierung.
- `sources.guardian_shared`: existierender Guardian-Datenordner, bestätigte
  physische Modulzuordnung und eigener Cache **außerhalb** dieses Ordners.
  Gelesen werden `hycube_history`, `hycube_history_projection`, `cell_history`.
  Alle Guardian-Dateien bleiben `rb`; Historie wird inkrementell mit Byte-/
  Zeitbudget gelesen. Aktuelle Werte kommen aus begrenzten Dateiende-Lesezugriffen.
- `solar_learning`: eigener persistenter SQLite-Speicher, getrennte Messquellen
  und Epochen. Details: [SOLAR_LEARNING.md](SOLAR_LEARNING.md).
- `rolling.enabled`: rollende Prüfung; benötigt die vollständige SOC-,
  Guardian- und Prognosezuordnung sowie konstante Notstromlast.
- `ha_display.enabled`: ausdrücklich optional; in der Vorlage ausgeschaltet.
  Bei Aktivierung müssen die beiden festen Sensor-IDs für diesen Dienst
  reserviert sein. Der Publisher hat keine frei konfigurierbaren Schreibziele.

`battery_mapping.confirmed`, `profile_confirmed`, `power_limits_confirmed` und
die Messquellenbestätigung `verified` sind in der Vorlage bewusst false.
Erst nach tatsächlicher Prüfung setzen; fehlende Angaben nicht als bestätigt
auslegen. Relative Cache-/Ausgabepfade beziehen sich auf das Arbeitsverzeichnis.

## Start und Paketierung

Nach separater Konfiguration/Autorisierung kann der Beobachter ohne Listener laufen:

```sh
python3 -B dist/hycube-shadow-0.5.1.pyz observe --config planner.json --output runs --interval 900 --duration-hours 0
```

Bei aktivierter rollender Prüfung gelten deren 60-s-/3600-s-Werte statt einer
vollen Planung alle 900 s. `0` bedeutet unbegrenzte Laufzeit. Konfiguration wird
erneut gelesen. Der eigene Lernspeicher bleibt erhalten, der Prozess startet
nach Host-/Studio-Code-Neustart **nicht automatisch** wieder.

`serve` bietet alternativ `/`, `/index.html`, `/latest.json`; standardmäßig nur
`127.0.0.1:8091`. Kein eigener Login: nicht öffentlich freigeben. `observe`
erzeugt keinen Netzwerklistener. Ergebnisse, Eingangsmomentaufnahmen und eigene
Caches können private Daten enthalten und gehören nicht ins Repository.

`Dockerfile`, `config.json` und `addon_entry.py` bilden einen separaten,
experimentellen HA-Add-on-Kontext. Für einen späteren lokalen Build diesen
gesamten Ordner als Kontext verwenden (`docker build .`). Add-on-Konfiguration
liegt schreibgeschützt unter `/config/planner.json`, Guardian-Share wird
schreibgeschützt eingebunden. Der Einstieg legt Runtime-Konfiguration, Cache,
Lernspeicher und Berichte unter `/data` ab und startet Ingress auf Port 8091.
Der HA-Zugang kann innerhalb dieses Kontexts `http://supervisor/core` mit der
vorhandenen Variable `SUPERVISOR_TOKEN` verwenden. Containerbau, Ingress und
Installation wurden für diese Veröffentlichung nicht ausgeführt.

## Umfang dieser Veröffentlichung

Enthalten: Dienst, synthetische Tests, anonymisierte Konfigurationsvorlage,
HA-Anzeigekarte, Paketierung und Dokumentation für 0.5.1.
Ausgeschlossen: Live-Konfigurationen, Serien-/Site-/Geräte-IDs, private IPs,
Token, Historien, SQLite-Datenbanken, Screenshots, Kundendokumente, alte
Release-Manifeste sowie fertige ZIP/PYZ-Pakete. Bestehende Guardian-Komponenten
bleiben unverändert. Quellsicherung und PR sind keine Deploymentfreigabe.
