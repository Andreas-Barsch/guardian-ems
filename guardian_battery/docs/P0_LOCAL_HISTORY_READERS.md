# Lokale History-Reader-Komposition (Kontrollpunkt 4a1)

Nur explizit über `compose_local_history` auswählen. `main` und produktive
Factories aktivieren diese Komposition nicht. Keine Änderung von SOC-,
Diagnose-, Phasen-, Versions- oder Hardwareverträgen. Kein HA-Nachweis.

## Lesevertrag

Die tatsächlichen HistoryApi, CellHistorySeries, Rs485HistorySeries,
HycubeBatteryCapacitySeries, DisplayHistoryReader, CanonicalPhaseReader und
GuardianResearchApi können vorbereitete Stores verwenden. GET entdeckt keine
Dateien, erzeugt keine Quellindizes und baut keine Projektionen auf.

`IndexedHistoryWindow` friert bestätigte Quellrevisionen/Receipts/Offsets ein.
Alle Module und Zellkanäle einer History-Anfrage teilen denselben Scan.
Das API-Ende bleibt einschließlich; intern wird das halboffene Ende mit
`nextafter` gebildet. Physische Quelldateinamen bestimmen keinen UTC-Tag.
RS485-Identität wird bei expliziter Indexvorbereitung anhand des vorhandenen
Decoders gespeichert. Ein fehlender Ursprung über getrennte Quelldateien wird
nicht geraten: unbekannte Identität bleibt ausdrücklich teilweise/unaufgelöst.
Hycube verwendet die vorhandenen Raw- und Projektionsformate ohne neuen Writer.

Bereits veröffentlichte Display-/Canonical-Bundles werden gemeinsam ausgewählt,
gepinnt und auf relevante Epochen geprüft. Der zusätzliche Zeitfensterindex wird
explizit in Schritten von höchstens acht Publikationszeilen vorbereitet.
Antworten nennen Bundle, Eingangs-Fingerprint, Coverage und vorhandene
Epochen-/Canonical-Abhängigkeiten. Ein späterer Raw-Append verändert eine
bestätigte Publikation nicht. Diese Antwort ist ein **bestätigter Datenstand**,
kein Nachweis der Abdeckung bis zum neuesten Raw-Wert. Detailabfragen lesen
bestätigte indizierte Raw-Fenster. Fehlende Publikationen/Indizes ergeben einen
Vorbereitungsstatus, keinen verdeckten Vollscan.

PreparedMetadata verwendet unveränderliche, explizit vorbereitete Revisionen.
Originale Identitäts-IDs und Timeline-Datensätze bleiben erhalten. Die
Vorbereitung benutzt den bestehenden Resolver; ihre initiale Materialisierung
ist **noch kein begrenzter Altbestands-Migrationsnachweis**. Abfragen verwenden
zeitindizierte Fenster und den unmittelbar vorherigen Metadatenstand.

Research-Routen für Zellserien, Topologie, Identitätsepochen, Zell-Coverage,
Display, Hycube-Kapazität und kanonische Phasen verwenden dieselben tatsächlichen
Reader. Nicht vorbereitete Zusatzrouten liefern in dieser lokalen Komposition
503. Persistierte SOC-Ereignisrouten bleiben unverändert; sie sind nicht durch
diesen History-Index als physisch begrenzt nachgewiesen. Research-Cursor binden
an das eingefrorene Eingangs-Fingerprint; geänderte Eingänge machen einen alten
Cursor ungültig, statt Ergebnisse unterschiedlicher Stände zu vermischen.

## Auftrag, Abbruch und Oberfläche

Ein expliziter Submit, kein automatischer Abruf oder Retry. Der lokale
HTTP-Pfad hält API, Serialisierung und Transfer in derselben Scheduler-Zulassung.
Socket-Abbruch erreicht den Reader und schließt dessen Fenstersnapshot.
Begrenzte FIFO-Zulassung und kooperative Zeitscheiben stammen aus Phase 3b1.
Der vorhandene Research-Request-Höchstwert von 60 Sekunden begrenzt lokal auch
Warten und History-Transfer; dies ist keine neue HA-Freigabegrenze.

Auswahlhandler werden nach Einrichtung aller Steuerelemente gebunden, auch für
Module und Zellen außerhalb des Formulars. Eine Auswahländerung bricht den
laufenden Auftrag ab und markiert den Stand als geändert. Fehlgeschlagenes
Laden erhält die vorhandenen Kurven. Die UI kennzeichnet bestätigte Datenstände
und fehlende Diagnoseüberlagerungen. Responsive Rasterregeln gelten nur für
direkte Formularfelder, nicht für verschachtelte Checkboxen.

## Strukturelle Grenzen und offene Abnahmen

- Raw: höchstens 256 Referenzen bzw. 64 KiB pro Seite, höchstens 2048 Seiten;
  Überlauf ist ein Fehler, kein abgeschnittener Erfolg. Die Grenzen sind
  lokale Schutzgrenzen, keine Zusage zu Anlagenkapazität oder HA-Latenz.
- Publikationslesung: höchstens 6000 Zeilen/8 MiB pro Auswahl und 100000
  SQL-VM-Schritte; höchstens 367 UTC-Tage pro Anfrage. Canonical-Zählung sieht
  nur Zeitstempel im angeforderten Fenster und erhält die bestehende
  Dublettenbehandlung. Metadaten haben explizite Seitenlimits.
- SQL-Messwerte bezeichnen die instrumentierten Fensterabfragen, keine gesamte
  SQLite-I/O-Bilanz. Gelesene Raw-Bytes sind logische Reads, keine gemessenen
  physischen SSD-Bytes. Python-Peak ist kein gesamter Prozess-/HA-RAM-Nachweis.
- Keine automatische Mischung eines neueren Raw-Tails in eine ältere
  Display-/Canonical-Publikation. Fortsetzung/Aufholen erfolgt weiterhin durch
  explizite vorhandene Vorbereitung; die gelesene Publikation bleibt als-of.
- Publikationsindex-Pins besitzen noch keine freigegebene GC-/Retentionpolitik.
  Daily-Diagnose, vollständige Migration, Kapazität, echte Stromausfälle,
  Gesamtsystem-/ARM64-/HA-Abnahme bleiben separate offene Gates.
- Ursprüngliche Bildfehler werden synthetisch geprüft; dieser Stand behauptet
  keine Reproduktion mit produktiven Originalantworten.
