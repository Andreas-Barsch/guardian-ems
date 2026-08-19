# Guardian Battery Maintenance Logbook

## Implementierungsstand

Implementiert sind Maintenance-Datenmodell, append-only JSONL-Persistenz,
Repository und Service, HTTP-API, Ingress-UI, Deep-Links, Guardian-Timeline
sowie read-only Maintenance-Overlays für Guardian-eigene Zeitverläufe.
Home-Assistant-MQTT-Event-Discovery und das ausschließlich für neue, aktuelle
Einträge verwendete Live-Signal sind ebenfalls implementiert. Produktives
Deployment und reale Abnahme sind weiterhin nicht Bestandteil dieses Stands.

Für den Release wird Guardian auf `0.5.0` angehoben. Die diagnostische
Bewertungslogik bleibt unverändert; ihre Diagnostic-Engine-Version bleibt
deshalb `0.4.12`.

## Persistenzgrenze

Der vorgesehene produktive Pfad ist:

`/share/guardian_battery/maintenance_events.jsonl`

Das Foundation-Modul legt diese produktive Datei nicht beim Import an. Erst
die spätere Runtime-Integration erzeugt bzw. öffnet sie kontrolliert.
`events.jsonl` bleibt das technische Alarm-/Statusprotokoll;
`config_history.jsonl` bleibt Konfigurationsprovenienz. Beide werden weder
migriert noch als Maintenance-Daten interpretiert.

## Aktivität und Rückwärtskompatibilität

Die fachliche Benutzersemantik lautet **Aktiv/Nicht aktiv**. Aktive Einträge
sind gültige Datensätze für Logbuch, Timeline, Overlays und spätere
Interpretation. Nicht aktive Einträge bleiben mit stabiler ID und allen
Revisionen auffindbar, werden aber standardmäßig nicht projiziert.

Schema 1 verwendet intern weiterhin `archived_at` als rückwärtskompatible
Persistenzrepräsentation: `null` bedeutet aktiv, ein UTC-Zeitpunkt bedeutet
nicht aktiv. Ein neues Feld würde alte und neue JSONL-Zeilen unnötig in zwei
Statuswahrheiten teilen. Deshalb liefert die API zusätzlich das abgeleitete
Feld `active`, während die bestehende JSONL niemals umgeschrieben wird.
Aktivieren und Deaktivieren hängen jeweils eine Revision mit Optimistic
Concurrency an. Die alten Archive-/Restore-Routen bleiben nur für bestehende
0.5.0-Aufrufer kompatibel; die normale UI verwendet ausschließlich
`activate`/`deactivate`. Ein Statuswechsel publiziert kein MQTT-Live-Event.

## Stackposition und physische Modulidentität

`module_number` ist ausschließlich die Stackposition zum Ereigniszeitpunkt.
`module_serial` ist die optionale Identität eines konkreten physischen Moduls.
Eine Position ist niemals eine dauerhafte Modulidentität; gleiche Positionen
zu verschiedenen Zeiten dürfen nicht automatisch korreliert werden. Fehlt die
Seriennummer, bleibt die physische Identität unbekannt.

Die Bestandsuntersuchung ergab keine belastbare historische Zuordnung mit
Gültigkeitsintervallen. `info <module>` liefert aktuell unter anderem einen
Pylontech-`Barcode`; `module_infos` wird in MQTT-State und Info-Attributen
veröffentlicht, aber nicht historisch positionsbezogen persistiert.
`cell_history`, `config_history`, technische Events und Diagnosedateien
enthalten keine solche Mapping-Historie. Eine manuell im Event gespeicherte
Seriennummer gilt nur für diesen Eintrag. Deshalb rät Guardian keine Zuordnung
und wendet die heutige Position nicht rückwirkend an. Die UI bietet ohne
belastbaren Nachweis nur „Nur Position / keine eindeutige Zuordnung“ sowie die
bereits im geöffneten Event gespeicherte Identität an.

## Zellmetriken und Modulebene

Für Zellspannung und Zelltemperatur bedeutet eine gewählte Modulposition ohne
`cell_number`: **Alle Zellen / Modulebene**. Es wird keine künstliche Zelle 0
gespeichert. Die API liefert die unveränderten Rohwerte aller Zellen mit ihrer
jeweiligen Zellnummer. Das zentrale Overlay-Matching verwendet weiterhin:

- Systemchart: systemweite Events,
- Modulchart: systemweite und Events derselben Position,
- Zellchart: zusätzlich modulweite und exakt passende Zell-Events;
  fremde Positionen und explizit fremde Zellen werden ausgeschlossen.

## Gemeinsame History-Chart-Semantik

Alle vier Messgrößen verwenden dieselbe skalierbare SVG-Komponente. Sie besitzt
lokale Zeit-Ticks, mehrere gerundete Y-Ticks mit Einheit, dezentes Grid,
exakten lokalen Tooltip und responsives Resize-Verhalten. SVG bleibt auch auf
HiDPI-Displays scharf. Die Messpunkte werden weder geglättet noch verändert.
Die Layerreihenfolge ist für ein späteres Arbeitspaket vorbereitet:
Phasenhintergrund, Messkurve, Maintenance-Marker, Interaktion/Tooltip. Es ist
keine Phase Engine und kein Phase Overlay enthalten.

## Schema 1

Jede JSONL-Zeile ist eine unveränderliche Revision eines Maintenance Events:

- `schema_version`: derzeit `1`
- `maintenance_event_id`: dauerhafte Identität, Präfix `MEV-` plus UUID
- `revision`: positive, monoton zu prüfende Revisionsnummer
- `occurred_at`: tatsächlicher fachlicher Ereigniszeitpunkt
- `created_at`: Erfassungszeitpunkt im Guardian-Logbuch
- `updated_at`: Zeitpunkt einer späteren Bearbeitung, sonst `null`
- `category`: stabiler, erweiterbarer technischer Kategorie-Schlüssel
- `title`, `description`
- `affected_system`
- optional `module_number`, `module_serial`, `cell_number`
- `action_taken`, `previous_state`, `result`, `reason`
- `source`: JSON-Objekt mit mindestens `kind`
- `archived_at`: Zeitpunkt einer Archivierung, sonst `null`
- `ended_at`: optionales Ende eines zeitlich ausgedehnten Ereignisses

Alle Zeitpunkte werden intern als ISO-8601 in UTC mit expliziter
Zeitzoneninformation gespeichert. `occurred_at`, `created_at` und
`updated_at` sind verschiedene Semantiken und dürfen nicht gegenseitig
ersetzt werden.

## Identität und Revisionen

Neue Guardian-Einträge erhalten genau einmal eine UUIDv4. Bearbeitungen und
Archivierung behalten diese ID und werden später durch den Repository-Layer
als neue Revision angehängt. Das Modell akzeptiert kanonische UUIDs allgemein,
damit ein zukünftiger, erst nach Sichtung realer Legacy-Daten implementierter
UUIDv5-Import möglich bleibt.

Der Low-Level-Store schreibt ausschließlich ans Dateiende. Er verwendet eine
exklusive Dateisperre, `flush` und `fsync`. Unlesbare JSON-Zeilen, unbekannte
Schema-Versionen und semantisch ungültige Datensätze werden mit ihrer
Zeilennummer gemeldet; sie werden nicht stillschweigend verworfen oder
umgedeutet.

Für atomare Repository-Operationen verwendet der Store zusätzlich
`maintenance_events.jsonl.lock`. Diese separate Sperrdatei schützt die gesamte
Read-Check-Append-Sequenz über Prozesse und Threads hinweg, ohne die
append-only Datendatei zu ersetzen oder umzuschreiben.

## Repository- und Service-API

`MaintenanceRepository` übernimmt die technische Projektion und die
Konsistenzregeln der Historie:

- `get(event_id)` liefert die aktuelle Revision, auch wenn sie archiviert ist.
- `history(event_id)` liefert alle Revisionen aufsteigend und unverändert.
- `list(include_archived=False, newest_first=True)` liefert die aktuelle Sicht.
- `append_revision(event, expected_revision=...)` führt den atomaren
  Revisionsvergleich und Append aus.
- `import_revision(...)` ist ein getrennter Einstiegspunkt für einen späteren,
  ausdrücklich freigegebenen Legacy-Adapter. Er definiert selbst kein
  Legacy-Format und umgeht keine Konsistenzprüfung.

`MaintenanceService` stellt die fachlichen Operationen bereit:

- `create(...)`
- `get(event_id)`
- `history(event_id)`
- `list(include_archived=False, newest_first=True)`
- `update(event_id, expected_revision=..., changes=...)`
- `archive(event_id, expected_revision=...)`
- `restore(event_id, expected_revision=...)`

Der Layer enthält keine HTTP-Statuscodes, Request-Objekte, UI-Daten oder
MQTT-spezifische Logik.

## Revisions- und Konfliktmodell

Ein neues Event beginnt bei Revision 1. Jede Änderung, Archivierung und
Wiederherstellung hängt genau die nächste Revision mit derselben Event-ID an.
`created_at` bleibt unverändert; eine fachliche Korrektur von `occurred_at`
ist nur als explizite Änderung und damit als neue Revision möglich.

Der Aufrufer übergibt `expected_revision`. Repository und Store sperren die
gemeinsame Read-Check-Append-Operation. Weicht die persistierte aktuelle
Revision ab, wird `MaintenanceConflictError` mit erwarteter und tatsächlicher
Revision ausgelöst. Der veraltete Datensatz wird nicht angehängt.

Die Projektion akzeptiert pro Event ausschließlich die lückenlose Folge
1, 2, 3, ... in Persistenzreihenfolge. Sie lehnt insbesondere ab:

- identische doppelte Revisionen,
- widersprüchliche doppelte Revisionen,
- Revisionssprünge,
- ein verändertes `created_at`,
- ein fehlendes `updated_at` ab Revision 2,
- ein gesetztes `updated_at` in Revision 1,
- beschädigte Datensätze und unbekannte Schema-Versionen.

Bei einer Inkonsistenz wird keine vermeintlich aktuelle Revision ausgewählt.

## Aktivität und Sortierung

Deaktivierung ist kein Löschen, sondern eine neue Revision mit internem
`archived_at`. Die Standardliste blendet nicht aktive Events aus. Mit dem
Aktivitätsfilter `all` oder `false` bleiben sie sichtbar; `get(event_id)` und
`history(event_id)` bleiben ebenfalls verfügbar. Aktivierung erzeugt eine
weitere Revision mit leerem `archived_at`.

Die Logbuch-Standardsortierung ist absteigend nach `occurred_at`, sekundär
nach `maintenance_event_id` (neueste zuerst). Für eine spätere Timeline
liefert `newest_first=False` zuverlässig die chronologische Sortierung.

## Maintenance API

Die REST-artige API ist in den bestehenden Guardian-Ingress-Webserver auf
Port 8099 integriert. Sie startet keinen weiteren Listener und besitzt keine
eigene externe Authentifizierungsgrenze. Vor jeder Maintenance-Anfrage bleibt
die vorhandene Prüfung auf den Home-Assistant-Ingress-Proxy aktiv.

`config_ui.py` erzeugt `MaintenanceEventLog`, Repository, Service und API erst
bei der ersten Maintenance-Anfrage. Ein Import oder Start der Webanwendung
öffnet deshalb nicht vorsorglich die produktive Maintenance-Datei. Die
handler-unabhängige `MaintenanceApi` erhält ihren Service per Konstruktor und
wird in Tests ausschließlich mit temporären Dateien betrieben.

### Routen

| Methode | Route | Funktion |
|---|---|---|
| `GET` | `/api/maintenance/events` | aktuelle Events auflisten |
| `POST` | `/api/maintenance/events` | neues Event anlegen |
| `GET` | `/api/maintenance/events/<event_id>` | aktuelle Revision laden |
| `PATCH` | `/api/maintenance/events/<event_id>` | Event bearbeiten |
| `POST` | `/api/maintenance/events/<event_id>/deactivate` | nicht aktiv setzen |
| `POST` | `/api/maintenance/events/<event_id>/activate` | aktiv setzen |
| `POST` | `/api/maintenance/events/<event_id>/archive` | Kompatibilität: nicht aktiv setzen |
| `POST` | `/api/maintenance/events/<event_id>/restore` | Kompatibilität: aktiv setzen |
| `GET` | `/api/maintenance/events/<event_id>/history` | vollständige Revisionsfolge |

Ein HTTP `DELETE` ist nicht vorgesehen. Ein vom HA-Ingress vorangestellter
Pfadpräfix wird toleriert; die API erzeugt selbst keine absoluten
`/hassio/ingress/...`-Annahmen.

### Kanonische JSON-Repräsentation

Detail- und Mutationsantworten verwenden:

```json
{
  "event": {
    "schema_version": 1,
    "maintenance_event_id": "MEV-<uuid>",
    "revision": 1,
    "occurred_at": "2025-03-12T13:00:00+00:00",
    "created_at": "2026-08-20T10:00:00+00:00",
    "updated_at": null,
    "category": "maintenance",
    "title": "Batterie geprüft",
    "description": null,
    "affected_system": "Pylontech Stack",
    "module_number": 1,
    "module_serial": null,
    "cell_number": null,
    "action_taken": null,
    "previous_state": null,
    "result": null,
    "reason": null,
    "source": {"kind": "manual"},
    "archived_at": null,
    "ended_at": null
  }
}
```

Zeitwerte sind kanonisches UTC-ISO-8601. `occurred_at`, `created_at` und
`updated_at` behalten ihre getrennte Semantik.

Beim Anlegen sind ausschließlich folgende Felder zulässig:
`occurred_at`, `ended_at`, `category`, `title`, `description`,
`affected_system`, `module_number`, `module_serial`, `cell_number`,
`action_taken`, `previous_state`, `result` und `reason`. Pflichtfelder sind
`occurred_at`, `category`, `title` und `affected_system`. Identität,
Revision, Erfassungs-/Änderungszeit, Archivstatus und Quelle erzeugt Guardian.

PATCH verwendet die explizite Konvention:

```json
{
  "expected_revision": 3,
  "changes": {
    "title": "Korrigierter Titel",
    "occurred_at": "2025-03-12T14:15:00+00:00"
  }
}
```

Archive und Restore akzeptieren ausschließlich:

```json
{"expected_revision": 3}
```

### Liste, Filter und Pagination

Filter sind kombinierbar:

- `include_archived`: `true` oder `false`, Standard `false`
- `newest_first`: `true` oder `false`, Standard `true`
- `occurred_from`: inklusive UTC-/zeitzonenbehaftete ISO-8601-Untergrenze
- `occurred_to`: inklusive UTC-/zeitzonenbehaftete ISO-8601-Obergrenze
- `category`
- `module_number`: 1 bis 6
- `cell_number`: 1 bis 15
- `limit`: 1 bis 100, Standard 50
- `offset`: nichtnegative Ganzzahl, Standard 0

Zeitfilter beziehen sich ausschließlich auf `occurred_at`. Zuerst wird
deterministisch sortiert, danach gefiltert und paginiert. Die Antwort enthält
`events`, `pagination` mit `limit`, `offset`, `returned` und `total` sowie die
verwendete Sortierbeschreibung.

### Fehlerformat und Statuscodes

Alle Maintenance-API-Fehler verwenden:

```json
{
  "error": {
    "code": "conflict",
    "message": "...",
    "details": {}
  }
}
```

| Code | HTTP | Bedeutung |
|---|---:|---|
| `invalid_request` | 400 | Form, JSON, Content-Type, Filter oder Pflichtfeld ungültig |
| `validation_error` | 400 | Maintenance-Domänenvalidierung fehlgeschlagen |
| `not_found` | 404 | Event oder API-Route nicht gefunden |
| `conflict` | 409 | Revisions- oder Archivzustandskonflikt |
| `method_not_allowed` | 405 | Methode für Route nicht zugelassen |
| `request_too_large` | 413 | Request-Body überschreitet das Limit |
| `internal_error` | 500 | unerwarteter interner Fehler |
| `history_error` | 503 | persistierte History ist beschädigt oder inkonsistent |

Ein Optimistic-Concurrency-Konflikt enthält in `details` zwingend
`maintenance_event_id`, `expected_revision` und `actual_revision`.
History-Fehler werden als `503` behandelt, weil die Persistenz vorhanden,
aber vorübergehend nicht zuverlässig nutzbar ist. Sie werden geloggt, jedoch
nicht als 404 getarnt. Tracebacks, interne Pfade und Exceptiondetails werden
nicht an Clients ausgegeben.

### Sicherheitsgrenzen

- maximaler Request-Body: 65.536 Bytes
- `Content-Type`: `application/json`, optional mit Charset
- Titel: maximal 200 Zeichen
- Kategorie und `source.kind`: maximal 64 Zeichen
- Systembezug und Modulseriennummer: maximal 200 Zeichen
- Beschreibung, Maßnahme, vorheriger Zustand, Ergebnis und Grund: jeweils
  maximal 10.000 Zeichen
- unbekannte JSON-Felder und Query-Parameter werden abgelehnt
- HTML-/Script-Text bleibt unverändert Daten; serverseitig wird nichts davon
  ausgeführt oder als HTML gerendert

## Kategorien

Die Foundation definiert folgende Starttaxonomie:

- `maintenance`
- `inspection`
- `repair`
- `module_replacement`
- `battery_cell_test`
- `firmware_change`
- `configuration_change`
- `wiring_connection`
- `troubleshooting`
- `other_technical`

Weitere stabile Lower-Case-Slug-Schlüssel bleiben zulässig. Damit ist die
Taxonomie erweiterbar, ohne bestehende History umzuschreiben.

## Wissenschaftliche Grenze

Maintenance Events sind zeitlicher Diagnosekontext. Dieses Datenmodell
erzeugt weder Diagnosewerte noch automatische Kausalitätsaussagen und ändert
die bestehende Guardian-Diagnose-Engine nicht.

## Reproduzierbare Testbaseline

Die zuvor nur dokumentierten fünf isolierten `parse_info()`-Regressionstests
sind nun als `tests/test_parse_info.py` reproduzierbar. Sie laden gezielt nur
den Parser und seine Felddefinition aus `main.py`, weil ein vollständiger
Import des Runtime-Moduls auf `/share/guardian_battery` und HA-Abhängigkeiten
zugreift. Zusammen mit den zuvor vorhandenen 21 Tests ergibt dies wieder die
dokumentierte Baseline von 26 Tests vor der Maintenance-Erweiterung.

## Maintenance-Bedienoberfläche

Das bestehende Ingress-Frontend bietet in seiner Kopfzeile ausschließlich
die Navigation **Konfiguration** und **Maintenance-Logbuch**. Das Logbuch
verwendet denselben HTTP-Server und dieselbe Ingress-Basis wie die
Konfigurationsoberfläche; es wird kein weiterer Port oder Dienst benötigt.
Die dynamische Ingress-Basis wird aus `X-Ingress-Path` beziehungsweise dem
aktuellen Request-Pfad abgeleitet. Fest eingetragene Add-on-IDs gibt es nicht.

Die Listenansicht lädt ihre Daten ausschließlich über die Maintenance-API.
Sie bietet kombinierbare Filter für lokalen Von-/Bis-Zeitpunkt, Kategorie,
Modul, Zelle und Archivstatus, zwei Sortierrichtungen sowie eine Pagination
mit 25 Einträgen pro Seite. Die responsive Kartenansicht ersetzt bewusst eine
breite Tabelle und bleibt auf schmalen Viewports bedienbar.

Über **Neuer Eintrag** werden Ereigniszeitpunkt, Kategorie, Titel, betroffene
Komponente sowie alle optionalen Kontextfelder erfasst. Detailansicht,
Bearbeiten, Archivieren mit Bestätigungsdialog, Wiederherstellen und der
aufklappbare Revisionsverlauf arbeiten ebenfalls ausschließlich gegen die
API. Archivieren löscht keinen Datensatz.

### Stabile Deep-Links

Ein Maintenance Event ist über
`maintenance?event_id=<URL-kodierte Maintenance-Event-ID>` direkt erreichbar.
Die URL-Erzeugung ist in den UI-Helfern zentralisiert und damit unabhängig von
einer statischen Add-on-ID. Nach Erstellen oder Bearbeiten bleibt die Event-ID
in der URL erhalten. Eine unbekannte ID führt zu einer verständlichen
Nicht-gefunden-Ansicht mit Rückweg zum Logbuch.

### Zeit- und Konfliktverhalten

Gespeichert und über die API übertragen werden ausschließlich
zeitzonenbehaftete UTC-Zeitpunkte. Das Browser-Frontend zeigt und erfasst den
Ereigniszeitpunkt in der lokalen Browser-Zeitzone und zeigt den resultierenden
UTC-Speicherwert vor dem Speichern an. Nicht existierende lokale Uhrzeiten in
einer DST-Lücke werden durch einen Komponenten-Roundtrip erkannt und
abgelehnt; für existierende mehrdeutige Uhrzeiten ist der konkrete UTC-Wert
vor dem Speichern sichtbar.

PATCH, Archivierung und Wiederherstellung senden die aktuell geladene
`expected_revision`. Bei HTTP 409 erfolgt kein automatisches Merge. Die
ungespeicherten Formularwerte bleiben stehen, und die aktuelle Serverversion
wird nur über eine ausdrücklich beschriftete Aktion neu geladen.

### Browser-Sicherheit

Vom Benutzer stammende Inhalte werden ausschließlich über `textContent`,
Formularwerte und DOM-Knoten dargestellt. Die UI verwendet kein
`innerHTML`; HTML- und Script-Fragmente bleiben sichtbarer Text. Request- und
Domänenvalidierung des Backends bleiben zusätzlich unverändert aktiv.

## Guardian Timeline / Verlauf

Die Ingress-Navigation enthält neben **Konfiguration** und
**Maintenance-Logbuch** nun den funktionsfähigen Eintrag **Verlauf**. Die
Timeline ist eine reine, read-only Projektion vorhandener Quellen:

- aktuelle Maintenance-Revisionen aus `maintenance_events.jsonl` über den
  `MaintenanceService`
- technische Alarm- und Statusereignisse aus dem bestehenden `events.jsonl`

Es wird weder eine zweite Timeline-Datei noch eine zweite Maintenance-Kopie
angelegt. Technische Events werden nicht zu Maintenance Events umklassifiziert.
Sie besitzen insbesondere keine `maintenance_event_id`; ihr interner
Projektionsschlüssel ist fachlich ausdrücklich keine persistente Event-ID.

### Projektionsmodell und Eventtypen

Das gemeinsame Timeline-Modell enthält `event_type`, `timestamp`, `title`,
`summary`, `source`, einen Projektionsschlüssel sowie optional Deep-Link,
Maintenance-ID, Modul, Zelle, Severity, Status und Metadaten. Derzeit werden
folgende typisierten Ereignisse projiziert:

- `maintenance`
- `alarm_started`
- `alarm_cleared`
- `status_changed`

Das technische Schema entspricht exakt der von `main.update_events()`
geschriebenen Struktur: Unix-Zeitstempel, `type` und je nach Typ `alarm`,
`code` oder `from`/`to`. Unbekannte oder beschädigte Datensätze werden nicht
stillschweigend übersprungen.

### Zeitsemantik, Rückdatierung und Sortierung

Für Maintenance ist `timestamp` immer `occurred_at`. Weder `created_at` noch
`updated_at` beeinflussen die Timeline-Position. Ein heute rückwirkend für
2024 erfasster Eintrag erscheint daher unmittelbar an seinem fachlichen
Zeitpunkt im Jahr 2024, ohne Migration oder zusätzlich erzeugtes Event.

`GET /api/timeline` sortiert inklusiv und chronologisch von alt nach neu.
Bei identischen Zeitstempeln lautet der deterministische Tie-Breaker
`event_type`, danach `projection_key`. Maintenance-Revisionen erscheinen
nicht mehrfach: Pro stabiler ID wird nur die aktuelle Service-Projektion
verwendet.

### API und Filter

Die Timeline-API akzeptiert:

- `from` und `to`: inklusive, zeitzonenbehaftete ISO-8601-Grenzen
- `event_type`: ein Typ oder kommaseparierte Typen
- `category`: Maintenance-Kategorie
- `module_number`: 1 bis 6
- `cell_number`: 1 bis 15
- `include_archived`: `true` oder `false`, Standard `false`

Alle Filter sind kombinierbar. Das Zeitfenster bezieht sich auf den
fachlichen `timestamp`. Die UI wandelt lokale Browserzeiten vor der Anfrage
deterministisch in UTC um und lädt nur das gewählte Fenster. Die API-Antwort
enthält `events`, `window`, `sorting` und die effektiv verwendeten `filters`.

Archivierte Maintenance Events sind standardmäßig ausgeblendet und können
explizit eingeblendet werden. Ihr Marker verwendet den bereits zentral in
Schritt 4 implementierten Deep-Link-Helper; die Timeline konstruiert keine
zweite Maintenance-URL.

### Fehler- und Aussagegrenzen

Eine beschädigte Maintenance-Quelle liefert einen kontrollierten 503-Fehler
`maintenance_history_error`; eine beschädigte technische Quelle entsprechend
`technical_history_error`. Eine leere Timeline wird in diesen Fällen nicht
vorgetäuscht. Ungültige Zeit- oder Filterparameter liefern HTTP 400.

Der Verlauf stellt zeitliche Korrelation dar. Er behauptet keine Kausalität
zwischen Maintenance, Alarmen oder Statusänderungen. Diagnostische
Kontextmarker und kausale Bewertungen sind nicht Bestandteil dieses Schritts;
das allgemeine Projektionsmodell kann später um weitere Eventtypen ergänzt
werden.

## Maintenance-Overlays in Guardian-Zeitverläufen

### Inventur und Integrationsgrenze

Das mitgelieferte Cell-Diagnostics-Dashboard enthält 184 unveränderte Home-
Assistant-`history-graph`-Karten. Diese Standardkarten bieten Guardian keine
saubere Schnittstelle für zusätzliche Marker. Zwei Dashboard-Einträge
referenzieren außerdem `custom:guardian-cell-history-card`; deren
Implementierung gehört nicht zu diesem Repository. Guardian verändert daher
weder diese Karten noch HA Core, Recorder-Samples oder veröffentlichte
Sensorwerte.

Als Guardian-eigene Zeitreihenquelle ist die tägliche, schema-versionierte
`cell_history/` vorhanden. Sie enthält pro Modul denselben Messzeitpunkt für
SOC, Strom, Zellspannungen und Zelltemperaturen. Darauf baut die neue
Ingress-Seite **Zeitverläufe** auf. SOC ist der verbindliche Referenzfall;
Zellspannung und Zelltemperatur verwenden dieselbe allgemeine Komponente.

### Projektionsarchitektur

Die Darstellung besteht aus drei weiterhin getrennten Schichten:

```text
cell_history/ ──> CellHistorySeries ──┐
                                      ├─> HistoryApi ─> generischer Chart
TimelineService ─> EventOverlayAdapter┘                 + Marker-Layer
```

Der `EventOverlayAdapter` erhält sichtbares Von/Bis-Fenster, optional Modul
und Zelle, Eventtypen und Archivoption. Er ruft ausschließlich die gemeinsame
Timeline-Projektion auf. Für Maintenance bleibt damit überall dieselbe
Semantik für `occurred_at`, ID, Kategorie, Archivstatus und zentral erzeugten
Deep-Link erhalten. Weder Messreihe noch Maintenance-Daten werden kopiert oder
verändert.

`GET /api/history/series` verlangt `metric`, `from`, `to` und
`module_number`. Zellmetriken verlangen zusätzlich `cell_number`;
`include_archived` ist optional und standardmäßig `false`. Unterstützte
Guardian-Metriken sind:

- `soc`
- `current`
- `cell_voltage`
- `cell_temperature`

Es werden ausschließlich die täglichen Zell-History-Dateien innerhalb des
inklusiven UTC-Zeitfensters gelesen. Bei jedem neuen Zeitfenster oder Zoom
wird dasselbe Fenster erneut für Messreihe und Timeline-Marker abgefragt; es
gibt keine Dauerabfrage der vollständigen Maintenance-History.

### Zentrale Matching-Regeln

- Systemweiter Chart ohne Modulfilter: alle Marker des Fensters.
- Modulchart: systemweite Events und Events desselben Moduls.
- Zellchart: systemweite Events, modulweite Events desselben Moduls sowie
  Events exakt derselben Modul-/Zellkombination.
- Event eines anderen expliziten Moduls: im Modul-/Zellchart ausgeblendet.
- Event einer anderen expliziten Zelle: im Zellchart ausgeblendet.

Damit bleibt übergeordneter Kontext sichtbar, während eindeutig fremde
Komponenten nicht eingeblendet werden. Diese Regeln liegen ausschließlich im
zentralen Adapter und werden nicht pro Chart dupliziert.

### Marker, Rückdatierung und Sicherheit

Marker sind fokussierbare und per Touch auswählbare vertikale Linien. Nahe
Marker verteilen ihre Werkzeugsymbole auf mehrere visuelle Ebenen. Auswahl
zeigt Maintenance, lokalen Ereigniszeitpunkt, Kategorie, Titel sowie optional
Modul und Zelle. **Maintenance-Eintrag öffnen** verwendet direkt den vom
Timeline-Service gelieferten Deep-Link.

Die horizontale Position wird ausschließlich aus `occurred_at` relativ zu
`from` und `to` berechnet. `created_at` und `updated_at` beeinflussen sie
nicht. Ein später nachgetragener oder migrierter Eintrag erscheint daher nach
Neuladen automatisch am ursprünglichen Ereigniszeitpunkt; es ist keine
zusätzliche Event-Kopie erforderlich.

Freitexte werden über `textContent` dargestellt. Die Chart-UI verwendet kein
`innerHTML`. Marker liefern ausschließlich zeitlichen Kontext; aus der Lage
vor oder nach einer Messwertänderung wird keine Kausalität abgeleitet.

## MQTT-/Home-Assistant-Live-Event

Die Guardian-History, das Maintenance-Logbuch, die Timeline und die Overlays
bleiben die vollständige historische Wahrheit. MQTT ergänzt ausschließlich
eine vergängliche Live-Signalisierung für Home Assistant. Die Empfangszeit
eines MQTT-Payloads ist nicht der fachliche Ereigniszeitpunkt; `occurred_at`
und `created_at` werden deshalb getrennt als Eventattribute übertragen.

### Konservative Live-Regel

Ein Event wird nur dann live publiziert, wenn alle Bedingungen gelten:

- erste Revision (`revision == 1`)
- manuelle Quelle (`source.kind == manual`)
- weder bearbeitet noch archiviert
- `created_at` liegt zwischen 0 und einschließlich 300 Sekunden nach
  `occurred_at`

Die zentral definierte Toleranz beträgt damit fünf Minuten. Ein zukünftiger
Zeitpunkt sowie ein um mehr als fünf Minuten rückdatierter Eintrag gelten
nicht als Live-Event. Backfill, Legacy-Import, Reload vorhandener JSONL-Daten,
Guardian-/HA-Neustart, PATCH, Archivierung und Restore publizieren ebenfalls
nichts. Unabhängig davon bleiben alle diese Events historisch nach
`occurred_at` sichtbar.

### Home-Assistant MQTT Event Entity

Guardian verwendet eine echte stateless MQTT Event Entity und keinen Fake-
Sensor. Die retained Discovery-Konfiguration wird im bestehenden
`Mqtt.discovery()`-Ablauf veröffentlicht:

```text
homeassistant/event/guardian_battery/maintenance/config
```

Sie definiert den einzigen stabilen `event_type` `maintenance`. Die
Maintenance-Kategorie bleibt ein separates Payloadattribut, damit Kategorien
nicht unkontrolliert die HA-Eventtyp-Taxonomie erweitern. Discovery selbst
löst kein Event aus.

Das Live-Topic lautet:

```text
<mqtt_topic_prefix>/battery/event/maintenance
```

Der kompakte JSON-Payload enthält:

- `event_type: maintenance`
- `maintenance_event_id`
- `category`
- `title`
- `occurred_at`
- `created_at`
- `affected_system`
- `revision`
- optional `module_number` und `cell_number`
- `guardian_version`
- `deep_link`

Der Deep-Link ist der vorhandene zentrale relative Guardian-Zielpfad
`maintenance?event_id=...`. Ein absoluter HA-Ingress-Link wird nicht
transportiert, weil dessen dynamischer Session-/Prefix-Anteil dem MQTT-
Publisher nicht zuverlässig bekannt ist.

### Retain und Ausfallverhalten

Der Discovery-Payload folgt der bestehenden Guardian-Konvention und wird mit
`retain=true` publiziert. Der eigentliche Maintenance-Event-Payload verwendet
verbindlich `retain=false`; später verbundene Clients erhalten daher kein
altes Ereignis als neu.

Die Create-Reihenfolge ist strikt:

1. Event samt stabiler ID und Revision erfolgreich append-only persistieren.
2. Live-Regel prüfen.
3. Gegebenenfalls genau einen MQTT-Publish versuchen.

Eine Exception oder ein MQTT-Fehlercode wird geloggt, ändert aber die bereits
erfolgreiche HTTP-201-Antwort nicht. Der Datensatz bleibt gespeichert. Es gibt
keinen Retry und keine persistente Publish-Queue; ein späterer Neustart darf
das Ereignis deshalb nicht fälschlich erneut als live ausgeben.
