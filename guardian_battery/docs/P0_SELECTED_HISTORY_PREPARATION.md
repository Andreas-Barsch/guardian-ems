# Ausgewählte History-Vorbereitung – lokaler P0-Nachtrag zu ad1d025

19.10.2026. Battery bleibt 0.8.7 (unveröffentlicht), MCP 0.8.4, Tunnel 0.8.1,
Engine 0.4.12 unverändert. Kein Build, keine produktive Vorbereitung/Installation.
Der frühere reine Vorprüf-/Blockerbericht wurde extern archiviert; dieser Nachtrag
schließt den dort festgestellten fehlenden lokalen Vorbereitungseinstieg.

## Einstieg und ausdrücklicher Umfang

`POST /api/history-preparation` ist ausschließlich über den bestehenden geschützten
HA-Ingress erreichbar. Er validiert höchstens 4096 Byte JSON und speichert nur den
Auftrag. Keine Quellprüfung, kein Indexaufbau und kein Rebuild im HTTP-Pfad.
`GET /api/history-preparation` liest den kompakten Auftragsstatus; kein History-GET
oder Statusabruf startet eine Vorbereitung. Ohne ausdrücklich eingereichten oder
bereits gespeicherten aktiven Auftrag bleibt der Worker untätig.

Beispiel für das schon vor der Änderung in HA angefragte Vergleichsfenster:

```json
{
  "timestamp_from": "2026-10-18T16:39:00Z",
  "timestamp_to": "2026-10-19T16:39:00Z",
  "modules": [1],
  "components": ["cell_index", "display", "canonical"],
  "shared_day_artifacts": true
}
```

Dies ist ein Beispiel für die spätere konkrete Freigabe, KEINE ausgeführte Anfrage.
Das Fenster ist einschließlich Endzeitpunkt. `modules` benennt die anschließende
Abnahmeauswahl. Indizes und Tagesprojektionen sind gemeinsame Artefakte aller
aufgezeichneten Kanäle; sie werden nicht als vollständige Projektion veröffentlicht,
wenn nur Modul 1 enthalten wäre. Diese notwendige gemeinsame Verarbeitung wird
mit `shared_day_artifacts: true` ausdrücklich bestätigt. Höchstens ein Auftrag
ist aktiv; ein zweiter wird abgewiesen. Maximal 48 Stunden je Vorbereitung begrenzen
dieses Maintenance-Werkzeug, nicht die fachliche History-Abfrage.

Weitere ausdrücklich auswählbare Komponenten: `rs485_index`, `hycube_index`.
`hycube_index` betrifft vorhandene `hycube_history_projection`-Quelldateien; er
startet keinen Hycube-Backfill und keine Batterieabfrage. Fehlende Quellen werden
als `absent` ausgewiesen. `complete` bezeichnet einen abgearbeiteten Auftrag,
nicht automatisch vollständige Daten-Coverage oder eine bestandene Betriebsabnahme.

`POST` mit ausschließlich `{"cancel": true}` fordert persistenten Abbruch an.
Der Worker beendet den laufenden Schritt kooperativ. Fertige Artefakte und
Checkpoints werden dabei nicht gelöscht. Nach einem regulären Neustart setzt ein
noch aktiver gespeicherter Auftrag fort; ein abgebrochener Auftrag bleibt beendet.

## Dateiumfang und Ablauf

| Komponente | Ausgewählte Dateien / Kontext | Verarbeitung |
|---|---|---|
| Cell-Index | UTC-Tage im Fenster plus ausdrücklich begrenzte Nachbartage +/-1 für lokale Quelldateinamen | je Schritt höchstens 256 Zeilen / 256 KiB plus eine begrenzte Randzeile; Identität vor jedem Block; am Ende atomarer Austausch ausschließlich der `.idx` |
| RS485-Index | UTC-Tage im Fenster | gleiche begrenzte Schritte, ISO-Zeitstempel und Identitätscheckpoint; keine Suche im alten Gesamtbestand |
| Hycube-Index | vorhandene Projektionstage im Fenster | gleicher Ablauf, `received_at` als Zeitachse; keine Rohdaten-Neuprojektion |
| Display | UTC-Tage im Fenster; Cell-Kontext +/-1 und entsprechender Hycube-Projektionstag | isolierter Neuaufbau mit aktuellem Segment-/Coverage-Algorithmus; geschlossene Buckets schrittweise ablegen; erst vollständige Ergebnisse veröffentlichen |
| Canonical | UTC-Tage im Fenster plus genau der unmittelbar vorherige Canonical-Tag und dessen Rohquelle | vorhandene vollständige Artefakte validieren und indizieren; fehlenden/unvollständigen Zieltag aus gültigem Vorgänger aufbauen; niemals an der Fenstergrenze neutral neu starten |

Für das Beispiel sind das Display/Canonical 18./19.10., Cell-Index-Kandidaten
17.–20.10. und als erster Canonical-Vorgänger der 17.10. Es gibt keine rekursive
Suche nach beliebig alter Vorgeschichte. Fehlt dieser unmittelbar erforderliche
Vorgänger oder ist er ungültig, wird der betreffende Schritt ausdrücklich gesperrt.
Ein späterer Schritt kann den zuvor erfolgreich vorbereiteten Tag als Vorgänger
nutzen. Es wird keine neue Phasenregel eingeführt.

Offene Rohdateien bleiben beim Live-Writer. Als offen gilt konservativ ein Datum,
das noch nicht sowohl im UTC- als auch im lokalen Kalender abgeschlossen ist.
Auch ein benötigter offener Nachbartag sperrt die betreffende historische
Vorbereitung (`open_source_owned_by_live_writer`). Keine parallele Übernahme der
aktuellen Datei, kein Stopp als versteckte Voraussetzung, kein automatischer
Gesamt-Rebuild. Nach Abschluss solcher Tage ist ein neuer expliziter Auftrag möglich.

## Wiederaufnahme, Live-Vorrang und Fehler

`SelectedHistoryPreparation` wird im Battery-Prozess gestartet. Er teilt dessen
`historical_slot` und sieht dessen Collectorzustand, Raw-Persistenz und RS485-Queue.
Ohne aktivierte Prozesskoordination pausiert er. Ein separates Python-Programm
im HA-Terminal wäre kein Ersatz; dort ist zudem keine Python-Laufzeit vorhanden.

- Jeder Schritt gibt den Slot wieder ab. Live-Zyklus, Raw-Rückstau, nächste reale
  Poll-Frist und Abbruch werden an kooperativen Prüfpunkten beachtet. Keine
  Warteschlange konkurrierender HTTP-History-Anfragen und keine Busy-Retry-Schleife.
- Auftrag und Arbeitszustände liegen ausschließlich unter
  `/share/guardian_battery/.history-preparation/<Auftrags-ID>/`;
  `request.json` liegt im Elternverzeichnis. Nach einem Neustart werden nur die
  bereits ausdrücklich ausgewählten Schritte fortgesetzt.
- Quellrevisionen werden gespeichert und vor Fortsetzung/Publikation geprüft.
  Rotation, Truncation oder Änderung sperren den Schritt. Autoritative Rohdaten,
  SOC-Ereignisse und Herkunftsnachweise werden nicht umgeschrieben.
- Display-Arbeit erfolgt in einem isolierten Arbeitsverzeichnis. Ein Journal
  sichert dessen JSON-Zustände und gzip-Längen vor jedem Schritt. Nach Abbruch
  werden nur diese abgeleiteten Arbeitsdateien zurückgesetzt und der Schritt
  wiederholt. Bucket-IDs machen SQLite-Wiederholung idempotent. Veröffentlichte
  Altdateien bleiben bis zur erfolgreichen neuen Publikation erhalten.
- Für fehlende Canonical-Tage sortiert eine temporäre SQLite-Datei ausschließlich
  die ausgewählte Rohdatei nach Zeitstempel und ursprünglichem Byteoffset. Einlese-
  und Verarbeitungsfortschritt sind getrennt gespeichert. Die bestehende
  `CanonicalPhaseMachine`, Konfigurationsauflösung und Tagesabschlusslogik werden
  unverändert verwendet; keine vollständige Tagesliste von Rohdatensätzen im RAM.
- Vorgänger, vollständige Canonical-Artefakte, Konfigurationshash und zugehörige
  Quellhashes werden geprüft. Bestehende gültige Canonical-gzip-Bytes bleiben bei
  bloßer Neuindizierung unverändert. Bei Wiederaufbau wird nur das abgeleitete
  Zielartefakt ersetzt. Fehlende/ungültige Checkpoints führen zu `blocked`.
- SQLite arbeitet transaktional mit Budgetprüfpunkten. Ein unterbrochener
  Publikationsschritt wird wiederholt. Mehrere öffentliche Dateien werden nicht
  als eine Dateisystemtransaktion ersetzt: Leser prüfen die Revision und können
  währenddessen ausdrücklich Nichtverfügbarkeit melden, statt falsche Daten zu liefern.

Strukturelle Grenzen: 65536 Byte je Quellzeile und je Konfigurations-/Positionskontext,
4 MiB je Arbeits-JSON bzw. dekomprimiertem Canonical-Artefakt, 16 MiB je zu
validierender Canonical-Rohdatei, 64 MiB temporärer Sortierdatenbank (Kontrolle zwischen
Schritten, höchstens ein Schritt zusätzlicher Zuwachs), SQLite-Sortiercache 1 MiB.
Index-/Sortier-/Verarbeitungsschritte umfassen höchstens 256 Records und rund
256 KiB plus eine maximal 65536 Byte große Randzeile. SHA-Verifikation und finale
Publikation sind begrenzte, wiederholbare Einheiten mit Prüfpunkten; nach Abbruch
beginnt diese jeweilige Einheit erneut. Kein Versprechen harter Präemption eines
Systemaufrufs oder atomarer JSON-Verarbeitung. Diese Grenzen sind KEINE aus HA
abgeleiteten Leistungsfreigaben. Überschreitungen werden als gesperrt ausgewiesen,
nicht durch stilles Kürzen oder einen unbegrenzten Ersatzweg umgangen.

Keine pauschalen alten Rebuild-POSTs zusätzlich auslösen. Die bisherigen
`/api/display-projection/rebuild`, `/api/canonical-phase/rebuild` und Hycube-Backfill-
Einstiege sind keine Auswahl dieses Pilotfensters und bleiben für diesen Plan außen vor.

## Lesender Vorprüfer und tatsächlich beobachteter Bestand

`tools/history_preflight.py` erzeugt eine begrenzte Pfadliste und liest nur
Dateimetadaten; `--shell` druckt einen exakt adressierten BusyBox-stat-Aufruf für
Core-SSH. Keine Inhaltsöffnung, Reparatur oder Ausführung des ausgegebenen Befehls.
Vorhandensein wird niemals als Indexgültigkeit ausgegeben. Policy-Inventar und
Kontextgültigkeit sind separate Prüfungen.

Am 19.10.2026 um 19:06:13 UTC wurden über Core-SSH ausschließlich Metadaten gelesen:
Cell 18.10. 6298232 Byte; RS485 18.10. 44949800 Byte; Positionshistorie 2328 Byte,
Wartungshistorie 28574 Byte, Konfigurationshistorie 28534 Byte.
Um 19:03:28 UTC: Cell 19.10. 5504993 Byte, vorhandene `.idx` 4359 Byte;
RS485 19.10. 35737719 Byte. Display-/Canonical-gzip vorhanden; keine SQLite-
Dateien in der geprüften Verzeichnistiefe. Das beweist weder Gültigkeit noch einen
passenden Vorgänger. Die Verzeichnisgröße der Hycube-Policy sagt nichts über
Inhaltsmenge aus. Keine Rohdaten exportiert und keine Vorbereitung ausgeführt.

## Ausgeführte lokale Prüfungen

Mit dem vorhandenen Produktionsdatei-/Netzwerkschutz, ohne Bytecode- oder
pytest-Cache-Schreibzugriffe: vollständige Battery-Suite **1238 bestanden**
(58,94 s), MCP/Tunnel **63 bestanden** (11,67 s). Danach wurde ausschließlich ein
weiterer Mehrschritt-Wiederaufnahmetest ergänzt; der Anwendungscode blieb gleich.
Die abschließende gezielte Suite aus Vorprüfer und Vorbereitung: **47 bestanden**
(9,09 s), einschließlich dieses zusätzlichen Tests. Die 47 sind keine zusätzlich
zur vollständigen Suite disjunkten 47 Tests.

Abgedeckt: keine Quellarbeit bei HTTP-Annahme/Status, Ingress-Schutz, explizite
Auswahl, Ein-Auftrag-Grenze, Live-Zyklus/Queue/Deadline, kein
unkoordinierter CLI-Lauf, persistierter Abbruch, Neustart, unveränderte Pause ohne
wiederholte Schreibzugriffe, Quelländerung und beschädigte Checkpoints, begrenzte
Rohzeilen, Identität vor dem Block, keine Lektüre alter ungewählter Dateien,
Display-Replay ohne Doppelzählung, begrenzter geschlossener Display-Zustand,
kanonische Hashprüfung, fehlender Vorgänger, fehlender Sortiercheckpoint sowie
exakte semantische Gleichheit mit dem bestehenden Builder auch bei unsortierten
Quellen und Neustart mitten in der Verarbeitung. Sämtliche Daten synthetisch.
Python-Syntax, Syntax des generierten BusyBox-Stat-Aufrufs und Diff-Prüfungen
bestanden. Reale HA-Laufzeit und AArch64-Build wurden damit nicht geprüft.

## Konkrete spätere Freigabe und verbleibende Betriebsbedingungen

Zunächst finalen sauberen Folgecommit und Tests prüfen. Vor einer Betriebsfreigabe
müssen das tatsächlich installierte Battery-0.8.6-Image samt Buildherkunft/Optionen,
das passende MCP-0.8.4-Artefakt sowie eine aktuelle Sicherung mit expliziter
/share-Abdeckung und Ereignis-Herkunftsnachweisen verfügbar sein. Dies ist noch
nicht vollständig belegt; Quellcode oder alte Archive ersetzen den Nachweis nicht.

Die spätere Freigabe muss genau benennen:

1. Vollständigen Battery-0.8.7-Build aus dem dann genannten finalen Commit mit
   passendem BUILD_VERSION und Quellmarker, notwendigen Battery-Stopp,
   Installation und Wiederanlauf. MCP/Tunnel/Engine bleiben unverändert.
2. Genau den obigen Pilotauftrag (Cell-Index, Display und Canonical für das feste
   24-h-Fenster, Modul 1 als Abnahmeauswahl, gemeinsame Tagesartefakte) unter
   Live-Vorrang. Keine anderen Tage außer dokumentiertem Rand-/Vorgängerkontext.
   Gesperrte Schritte berechtigen zu keiner selbstständigen Ausweitung.
3. Dieselbe manuell ausgelöste Vorher-/Nachherabfrage und parallele Beobachtung
   von Live-Takt, Queue, Speicher und I/O. Keine zusätzlichen Hardwarepolls.

Rückkehr: das vorher gesicherte Image mit passenden Optionen installieren, aktuelle
Rohdaten/Ereignisse behalten und KEINE alten Daten über neue zurückspielen. Neue
abgeleitete Artefakte sind kein Ersatz für die Rückkehrsicherung. Keine SSD-Aktion,
keine Ereignisimporte, kein GitHub-Schreibzugriff durch diesen lokalen Nachtrag.

AArch64-Imagebuild, stdlib-SQLite im Zielimage, vollständiger Buildkontext,
Ingress-Abbruchweitergabe und reale Live-Leistungsbudgets sind noch offen. Die
alte 24-h-Anfrage besitzt weiterhin keinen belegten Abschlusszeitpunkt. Lokale
Regressionen ersetzen deshalb keine HA-Abnahme und keine Deploymentfreigabe.


## Expliziter Bootstrap ab gespeichertem Quellenanfang (lokal, nicht ausgeführt)

Die lesende HA-Prüfung vom 19.10. fand für 10.–18.10. nur Live-Artefakte
mit `complete=false` und ohne Quellhash. `.rebuild.json` fehlt; `.current.json`
gehört zum 19.10. und ist kein rückwärts verwendbarer Zustand. Die begrenzte
Quelleninventur (Abbruch bei 97 Einträgen) fand 65 Tagesdateien vom 16.08. bis
19.10.2026. Dies beweist den Anfang des aktuell gespeicherten Bestands, nicht
den Beginn der physischen Lebensgeschichte oder das Fehlen früher gelöschter Daten.

Optional kann ein Vorbereitungsgesuch ausdrücklich
`"canonical_bootstrap_from": "2026-08-16"` enthalten. Es muss `canonical`
auswählen; der Ursprung muss 1–90 Tage vor dem ausgewählten Fenster liegen.
Ohne dieses Feld bleibt das bisherige Verhalten mit zwingendem gültigem
unmittelbarem Vorgänger unverändert. Keine automatische Umfangserweiterung.

Für das Pilotfenster 18.10.16:39Z–19.10.16:39Z bedeutet dies einen ausdrücklich
gewählten gemeinsamen Canonical-Aufbau für alle gespeicherten Identitäten vom
16.08. bis einschließlich 17.10. als Vorgänger, anschließend die ausgewählten
Tage. Ein früherer gültiger nachgewiesener Checkpoint könnte diesen Umfang
verkleinern; im geprüften Teilbestand wurde keiner belegt. Es ist ein hinreichender
konservativer Bootstrap, kein Beweis, dass jeder frühere Tag in jedem denkbaren
Datenbestand notwendig wäre. Offene Tage und benötigte offene Nachbarn bleiben
unverändert gesperrt. Am 19.10. ist der gesamte Pilot daher noch nicht vorbereitbar.

Der Worker prüft höchstens 512 Verzeichniseinträge ohne Rekursion. Die datierten
Rohdateien vor dem ausgewählten Fenster müssen exakt den ausdrücklich genannten,
lückenlosen Präfixtagen entsprechen. Frühere/fehlende/unbekannt benannte Quellen,
Symlinks und Überschreitung der Inventurgrenze sperren. Rohdateien werden hier
nur stat-geprüft; ihre Inhalte verarbeitet erst der bestehende begrenzte Sortier-
und Phasenpfad. Präfixrevisionen und Konfigurations-/Identitätskontext werden
über den ganzen Auftrag festgehalten und bei Wiederaufnahme geprüft. Pro Quelldatei
bleibt die 16-MiB-Grenze, je Verarbeitungsschritt 256 Datensätze/256 KiB plus
begrenzte Randzeile. Historischer Slot, Abbruch und Wiederaufnahme gelten auch
für den Bootstrap; ein gesperrter Vorgängerschritt sperrt folgende Canonical-Schritte.

Nur am nachgewiesenen ersten gespeicherten Quelltag wird die bestehende
`CanonicalPhaseMachine` neu initialisiert, genau wie beim bisherigen vollständigen
`CanonicalPhaseProjection.build()`. Kein neutraler Zustand an der Fenstergrenze.
Tageswechsel, Hysterese, Endpunkte, Identitätsauflösung und Tagesabschluss bleiben
unverändert. Die erste beobachtete Konfiguration beginnt erst am 17.08.2026
19:21:51Z; davor verwendet die bestehende Semantik `unknown`, keine erfundenen
Defaults. Alte unvollständige Artefakte werden nicht durch Flag-/Hash-Ergänzung
zu vollständigen Belegen umgedeutet, sondern aus den ausgewählten Rohquellen
neu abgeleitet. Diese produktive Ableitung erfordert eine separate konkrete Freigabe.

Rückkehrnachweis: Die Sicherung vom 19.10. nennt Battery 0.8.6/MCP 0.8.4 und
`share`, liefert über die geprüften Metadaten aber weder Image-Digest noch
enthaltenes Imagearchiv. Im Core-SSH waren kein Docker-Socket und kein passendes
0.8-Rückkehrarchiv direkt unter `/backup` sichtbar. Das beweist nicht deren
Fehlen auf dem Host/entfernten Sicherungsziel. Keine Restore-/Buildfreigabe daraus.
