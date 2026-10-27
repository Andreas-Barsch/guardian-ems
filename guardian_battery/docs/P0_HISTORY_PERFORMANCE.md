# P0-Zeitverlauf – lokaler Kandidat Battery 0.8.7

Stand 19.10.2026. Basis d1d6d3609080d613c6c42822bea865b1358843a0
(Battery 0.8.6). Kein Deployment und keine vollständige Betriebsabnahme.
MCP 0.8.4, Tunnel 0.8.1 und Engine 0.4.12 bleiben unverändert. Der ältere
0.8.2-Checkout und vorhandene Nutzerarbeit wurden nicht verändert.

## Manuelles Laden und Darstellung

Nur das Submit lädt Historie. Initialisierung, URL-Wiederherstellung,
Auswahländerungen, Seitenwechsel und Resize laden nichts. Eine Generation plus
AbortController trennt laufenden Request, aktuelle Auswahl und geladene Daten.
Eine verspätete Antwort wird verworfen, auch beim erneuten Zeichnen. Bei Fehlern
bleibt das bestehende Diagramm erhalten. Die HTTP-Verbindung wird ohne Verbrauch
von Nutzdaten auf Abbruch geprüft; die Weitergabe eines Browserabbruchs durch
HA-Ingress muss noch real abgenommen werden.

`ordered_identity_coverage_v1` sortiert innerhalb des Fensters, erhält abweichende
Messungen mit gleichem Zeitstempel, kennzeichnet Konflikte und entfernt nur exakte
Dubletten. SVG-Segmente trennen physische Identität/Epoche, Quelle, Messgröße und
Zellkanal. Lücken beruhen auf Quell-Coverage und beobachteter Kadenz, nicht auf dem
Abstand bereits ausgedünnter Displaypunkte. Die bestehende Coverage-Konvention
(dreifache beobachtete Kadenz) bleibt die Grundlage. Ein begrenzter Kontext der
letzten 256 positiven Quellintervalle überbrückt Bucketgrenzen bei Einzelmessungen;
er ist keine Diagnose- oder SOC-Erkennungsregel. Unbekannte Kadenz behauptet keine
lückenlose Verbindung. SOC-Änderungen, Extremwerte, Konflikte und Segmentgrenzen
werden nicht zugunsten eines starren Punktlimits verworfen. Der tatsächliche
Antwortumfang kann deshalb über dem nominellen Display-Punktziel liegen.

## Physisch begrenzte Lesewege

- Dateinamen werden aus dem Fenster bestimmt; lokale Cell-Dateinamen werden an
  der UTC-Tagesgrenze berücksichtigt. RS485 liest alle ausgewählten Module und
  Kanäle in einem gemeinsamen Scan. Blockindizes begrenzen Bytebereiche; angeschnittene
  Indexblöcke (standardmäßig 256 Datensätze) sind begrenzter Randkontext. Identitäts-Checkpoints gelten
  am Blockanfang und dürfen keine zukünftige Identität vorwegnehmen.
- Ein fehlender/defekter/veralteter Index wird im HTTP-Pfad nie aufgebaut.
  Maximal 256 unindexierte Zeilen, höchstens 65536 Byte je Zeile, werden geprüft.
  Größere Tails liefern `history_index_required`, keine still gekürzte Antwort.
  Indexprüfung liest zusätzlich maximal zwei 4096-Byte-Signaturbereiche.
- Unindexierte Positions-/Wartungsmetadaten sind im historischen Kontext auf
  65536 Byte begrenzt. Die alte Policy-Vorgängersuche prüft höchstens 256
  Verzeichniseinträge und 65536 Byte Gesamtkontext. Darüber folgt
  `history_context_index_required`. Diese strukturellen Sicherheitsgrenzen sind
  KEINE aus HA abgeleiteten Latenz- oder Speicherfreigaben. Große Altbestände können
  bis zur gezielten Index-/Kontextvorbereitung nicht vollständig angezeigt werden.
- `*.window.sqlite` ist ein abgeleiteter, ersetzbarer Nebenindex für einzelne
  Display-Buckets bzw. kanonische Intervalle/Samples. Leser öffnen ihn read-only,
  prüfen die Publikationsrevision und lesen nur das Fenster und ausgewählte Kanäle.
  Fehlende/beschädigte Nebenindizes führen nur zu begrenztem Raw-Tail oder einem
  ausdrücklichen Nichtverfügbarkeitsstatus. Bestehende gzip-Projektionen bleiben
  erhalten; deren alte Version genügt nicht als neuer Fensterindex.
- `*.append.json` enthält Geräte-/Inode-/Größen-/mtime-Bezug und eine
  Append-Generation. Raw-Daten werden zuerst persistiert. Rewrite, Rotation oder
  Truncation machen den alten Prefix-Nachweis ungültig. Das ist ein Nachweis des
  normalen Writer-Lebenszyklus, kein kryptografischer Schutz gegen absichtliche
  Manipulation von Dateiinhalten und Metadaten.
- Offene kanonische Phasen werden aus dem begrenzten Checkpoint mit derselben
  bestehenden Phasenmaschine fortgesetzt. Keine Legacy-Gesamtanalyse als
  interaktiver Ersatz bei fehlender/veralteter kanonischer Projektion.

## Live-Vorrang und Messgrößen

Ein gemeinsamer historischer Slot hat keine Warteschlange. UI/Projektionsjobs
und Research-Zeitreihen/Phasen konkurrieren um diesen Slot. Statusabrufe bleiben
möglich. Ein aktiver Erfassungszyklus, Raw-Persistenz oder Rückstau im Buswriter
unterbrechen historische Arbeit an kooperativen Prüfpunkten. Die Laufzeitfrist
kommt aus dem nächsten tatsächlichen Poll-Termin; verschachtelte Budgets dürfen
sie nicht verlängern. Die Serialisierung prüft zwischen JSON-Teilen. Keine harte
Präemption eines einzelnen Betriebssystemzugriffs oder einer SQLite-Operation.
HTTP-Schreibzeit ist gesondert erfasst; Ingress-/Netzwerkverhalten ist noch offen.

Die vorhandene read-only Timingausgabe erfasst Auswahl/Index, Zell-/Hycube-/Raw-
Arbeit, RS485-Lesen/Decode/Identität, kanonische Phasen, Overlays, Serialisierung
und Antwortschreiben. RS485 zählt Quellenbytes, Decodes, ungeklärte Identität,
ungültige Datensätze und Signaturbytes getrennt. Queuezeit ist 0 (sofortige
Abweisung). Display-Bytes bedeuten ausgewählte JSON-Payloads aus SQLite, NICHT
physische Datenträger-I/O. Nicht gemessene Prozess-Peakwerte und physische
I/O bleiben ausdrücklich null/unbekannt. Browserstatus-Tooltip zeigt Abruf plus
JSON-Parse und Zeichnen getrennt; Abruf enthält Netz- und Serverzeit und ist
kein isolierter Netzwerkbenchmark. Kein zusätzlicher Historienabruf fürs Profiling.

## Reproduzierbare lokale Prüfung

`tests/benchmark_history_p0.py` erzeugt synthetische Tagesdateien in einem
Temporärverzeichnis und misst dasselbe 24-h-Busfenster mit sechs Modulen bei
1/10/30/365 Tagen Bestand. Cold bedeutet neue Readerinstanz, nicht geleerter
Betriebssystemcache. Der Vergleich erfasst Quellenbytes, Decodes, Laufzeit und
Python-tracemalloc-Peak; SQLite-Dateisystem-I/O ist damit nicht gemessen.
Tests verwenden den vorhandenen Produktionsdatei-/Netzwerkschutz und deaktivieren
Bytecode/pytest-Cache. Keine Tests gegen /share, /data, /config oder produktive APIs.
Die benannten historischen Bilddateien wurden in Attachments/Projektunterlagen
nicht gefunden. Kurvenfehler werden daher synthetisch regressionsgeprüft.

## Ausgewählte Bestandsvorbereitung

Der lokale Nachtrag ergänzt einen expliziten, im Battery-Prozess koordinierten
Vorbereitungsauftrag. Fenster/Komponenten werden vorab benannt; der HTTP-Pfad
speichert nur den kleinen Auftrag. Blockindizes, Display-Staging mit Replayjournal
und kanonische Sortier-/Verarbeitungscheckpoints setzen unter Live-Vorrang fort.
Ohne passenden Vorgänger, bei offenen Quellen oder überschrittenen strukturellen
Grenzen folgt ein gesperrter Status statt einer versteckten Gesamtanalyse.
[Konkreter Einstieg, Grenzen, Testszenarien und späterer Freigabeumfang](P0_SELECTED_HISTORY_PREPARATION.md).
Keine produktive Vorbereitung wurde ausgeführt.

## Vor einer Installation und HA-Abnahme

1. Den vollständigen sauberen Kandidaten samt Quellmarker und BUILD_VERSION
   0.8.7 prüfen. Der lokale Sicherungscommit ist noch kein gebautes oder
   veröffentlichtes Release. Keine pauschale Übernahme der anderen Branches.
2. Aktuellen gemeinsam passenden Rückkehrstand, Optionen und Datensicherungsumfang
   verifizieren. Raw-Historie, SOC-Ereignisse und Herkunftsnachweise nicht ersetzen.
3. Tatsächlichen Index-/Projektionsbestand und Umfang unindexierter Kontexte
   lesend feststellen. Eventuell notwendige gezielte Vorbereitung ist separat
   auszuweisen; kein Gesamt-Rebuild beim ersten Browserrequest. Im HA-Terminal
   keine Python-Laufzeit installieren. Python existiert im Add-on, nicht im
   dokumentierten Core-SSH-Terminal. Vorbereitung und Installation benötigen die
   Freigabe für diesen konkreten neuen Stand.
4. Dieselbe 24-h-Auswahl vor/nach Installation messen, parallel Live-Verzögerung,
   verpasste/verspätete Messungen, Raw-Queue, CPU, Speicher und tatsächliche I/O.
   Schon vorher wurden 49–73 s Zyklusdauer bei 10 s Ziel beobachtet; das ist keine
   belastbare numerische Freigabegrenze. Die vollständige Vorhermessung und
   nachher abgeleitete Budgets fehlen. Lokaler Rechnerbenchmark ersetzt sie nicht.
5. Quellstand, installiertes Add-on und separate HA-Dashboarddateien getrennt
   verifizieren. Diese Änderung betrifft die Add-on-History-UI; eine getrennte
   Dashboarddatei wird durch einen Add-on-Build nicht automatisch aktualisiert.

P0 bleibt bis zum gemeinsamen Nachweis aller Anforderungen einschließlich
realem Live-Schutz und vergleichbarer HA-Abnahme OFFEN.

## Tatsächlich ausgeführte Prüfungen am 19.10.2026

- Endgültige Battery-Suite: **1192 passed in 48.35s**
  (`guardian-p0-release-candidate-tests.log`).
- Research-MCP/Tunnel: **63 passed in 12.97s**; deren Dateien unverändert.
- Python-Syntax für 39 geänderte/neue Python-Dateien und Syntax des tatsächlich
  gerenderten JavaScript geprüft; `git diff --check` ohne Befund.
- Echter lokaler Browser mit synthetischen Daten: URL stellt Modul 3 wieder her,
  Initialisierung/Reload/Auswahl erzeugen 0 Requests; Submit für Modul 4 erzeugt
  genau 1. Anschließender Resize auf 390×844 bleibt ohne Folgeabruf, Zustand
  AKTUELL, keine Browserfehler. Abgelesen: Abruf/Parse 16,1 ms, Rendern 2,6 ms.
  Das ist ausdrücklich keine HA-Laufzeitmessung. A/B-Race/Fehler/Abbruch werden
  zusätzlich ausführbar mit der vorhandenen Node-Umgebung regressionsgeprüft.
- Vergleichbarer RS485-Benchmark: gleicher semantischer SHA-256 für alle 16
  Vorher-/Nachherfälle, einschließlich unveränderter Freigabewechsel.
  Alte Darstellung 402, neue 720 Punkte wegen erhaltener redundanter Boolwerte;
  der Vergleich normalisiert ausschließlich diese Redundanz.

| Bestand | Decodes vorher → nachher | Quellenbytes vorher → nachher | Warm-Laufzeit vorher → nachher | Python-Peak warm vorher → nachher |
|---|---:|---:|---:|---:|
| 1 Tag | 2196 → 366 | 627768 → 112820 | 0,0533 → 0,0641 s | 220325 → 587942 B |
| 10 Tage | 21960 → 366 | 6277680 → 112820 | 0,2776 → 0,0627 s | 196338 → 523208 B |
| 30 Tage | 65880 → 366 | 18833040 → 112820 | 0,8050 → 0,0630 s | 218390 → 523026 B |
| 365 Tage | 801540 → 366 | 229135320 → 112820 | 10,1333 → 0,0630 s | 570563 → 522986 B |

Die erste kalte P0-Abfrage benötigt wegen neu geladener Module rund 2,03 MB
Python-Peak (vorher rund 0,22 MB), 0,0927 s statt 0,0390 s. Bei kleinen Beständen
ist der neue Pfad somit nicht pauschal schneller oder speichersparsamer.
Gleichbleibende Quellenbytes/Decodes belegen die beseitigte Altersskalierung;
sie belegen weder kalten OS-Cache noch physische HA-Datenträgerlast.

Die abschließende Prüfung ergänzte außerdem begrenzte Raw-Randbereiche bei
angeschnittenen Buckets: Ein Messwert innerhalb des Fensters darf nicht entfallen,
nur weil er im vollständigen Bucket kein Extremwert war. Rewrite verwirft alte
Sample-Zählungen im abgeleiteten kanonischen Fensterindex.
