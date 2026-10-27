# Lokale Diagnose-Komposition – Kontrollpunkt 3a1

Unveröffentlichter lokaler Baustein. Keine Auswahl in `main.py`, keine neue
Runtime-Factory, kein automatischer Import alter Cachedateien, keine HA-Abnahme.

## Tatsächliche Stores und Semantik

`DiagnosticRepository` benutzt `CellDiagnosticStore.add`, die vorhandene
CurrentCondition-Statistik, `DiagnosticAggregateStore.add/project_records` und
`CellDiagnosticStore.analyse` einschließlich der bestehenden EvidenceDiagnostics.
Die Statistikschleife wurde als unveränderte Berechnung in wiederverwendbare
Methoden herausgezogen. Keine Formel, Schwelle, SOC-Regel oder Version wurde
geändert. Optionale Budgetprüfungen sind außerhalb des expliziten lokalen
Arbeitskontexts inaktiv.

Die beiden existierenden Eingangssemantiken sind ausdrücklich benannt:

* `live`: bestehende append-/maxlen-Semantik, auch für unbekannte Identitäten
  und nicht als wissenschaftliche Evidenz geeignete Zellslots.
* `history`: tatsächliche `CurrentConditionBackfill._sample`-Validierung,
  autoritative Modul/Zeit-Slots und chronologisch begrenzte Retention. Optional
  erfolgt die Identitätsauflösung aus dem tatsächlichen EpochStore mit
  registrierter, vor Veröffentlichung geprüfter Abhängigkeit. Unbekannte oder
  ungültige historische Samples werden nicht erfunden; Qualitätszähler und der
  exakt bestätigte Quellcursor bleiben erhalten. Ein kleiner autoritativer
  Slotindex verweist auf bereits gespeicherte Sample-Versionen. Verdrängt eine
  spätere Slotkorrektur eine andere Identität, wird deren Retention aus dem
  nächstälteren kanonischen Slot ergänzt, ohne alten Raw-Replay. Auch ein sofort
  außerhalb des Ringendes liegender historischer Slot bleibt dadurch korrekt
  referenzierbar; diese abgeleiteten Referenzen müssen beim späteren GC erhalten
  bzw. ausdrücklich berücksichtigt werden.

Aktuell dokumentierte Positionsbelegungen werden durch eine explizite Übergabe
mit der vorhandenen `set_current_identities`-Methode übernommen. Historische
Konfigurationseinträge ersetzen nicht die ausdrücklich gesetzten aktuellen
Optionen. Aggregat-Records behalten ihre ursprünglichen Config-IDs.

## Veröffentlichung und Wiederanlauf

Private MVCC-Zeilen enthalten nur geänderte Samples, Ringzeiger, betroffene
Aggregat-Records oder Metadaten. Alte bestätigte Ansichten bleiben lesbar.
Der vorhandene ProjectionRepository veröffentlicht Cell- und Aggregat-Metadaten
sowie den bestätigten Quellcursor als ein Bundle. Erst dieselbe Transaktion
übernimmt dieses Bundle in den Kompositionszustand. Unfertige Retention- oder
Schreibarbeit ist davor keine gültige neue Veröffentlichung.

`consume` bearbeitet höchstens einen katalogisierten Quellslot. `finish` setzt
private Retentionsarbeit fort. Nach Prozessabbruch wird deren persistierter
Zustand übernommen; ein bereits angewendeter privater Slot wird nicht erneut
aus Raw gelesen. Nach erfolgreichem Commit beginnt der nächste Raw-Zugriff am
bestätigten Cursor. Quellrevisionen und Append-Belege werden geprüft; eine
veränderte oder unvorbereitete Quelle führt zu einem ausdrücklichen Fehler,
nicht zu einer versteckten Gesamtreanalyse.

Dies ist die Wiederaufnahme des neuen, gemeinsam bestätigten Checkpointformats.
Zwei beliebige bisherige JSON-Cachedateien werden nicht nachträglich als atomarer
Checkpoint ausgegeben. Ihre produktive Übernahme/Migration bleibt separat.

## Inkrementelle Analyse und aktuelle Optionen

CurrentCondition-Beiträge werden je Sample-Prüfsumme und Options-Hash dauerhaft
wiederverwendet. Ein neuer Append benötigt nur seinen neuen Beitrag. Eine
geänderte Optionskonfiguration berechnet die dafür benötigten Beiträge in
Seiten mit höchstens 32 ausgewählten Retentionssamples. Fortschritt und Beiträge
werden zusammen bestätigt; ein Neustart übernimmt die fertigen Seiten.

Die abschließenden vorhandenen Diagnosemethoden erhalten weiterhin die exakte
Retentionsauswahl der betroffenen Identität und deren vorhandene Aggregate.
Median-/Trend-/Sequenzmethoden werden nicht durch Näherungen ersetzt. Diese
Reduktion benötigt die ausgewählte Retention, nicht die vollständige Raw-History.
Sie ist kooperativ abbrechbar. Nach Abbruch wird die noch nicht bestätigte
Modulreduktion wiederholt; bereits bestätigte Beiträge und fertige Module bleiben
erhalten. Die kleinste dauerhafte Reduktions-Abnahme ist ein vollständiges Modul,
nicht ein Zwischenwert innerhalb einer Diagnoseformel.

Ergebnisse werden in kleinen Zell-/Metadatenstücken gespeichert. Ein identischer
Auftrag für dasselbe Bundle, dieselben Optionen, Module und Wartungsereignisse
verwendet nach Neustart das vorhandene vollständige Ergebnis ohne Neuberechnung.

Eine kleinere Ringkapazität entfernt höchstens 32 älteste Mitglieder je Schritt.
Eine größere Kapazität vergrößert den verfügbaren Platz, erfindet aber keine zuvor
aus dem Cache entfernte Evidenz. Aggregat-Retention wird weiterhin durch den Tag
eines tatsächlich verarbeiteten Samples angewendet; es gibt keine neue
Wanduhrregel. Diese Eigenschaft ist auch bei späten älteren Samples getestet.

## Worker und strukturelle Grenzen

`local_diagnostic_workers` erzeugt ausschließlich auf ausdrücklichen Aufruf die
bestehenden DerivedPersistenceWorker- und CellAnalysisWorker-Klassen mit kleinen
Quell-/Checkpoint-Referenzen. Es werden dabei keine Threads gestartet. Ein
überschriebener wartender Persistenzauftrag verliert keine Quelldeltas: der
neueste Zielcursor umfasst auch die noch nicht bestätigten Vorgänger. Ein
überschrittenes Schrittbudget meldet einen Fehler mit erhaltenen Checkpoints;
eine erneute explizite Übergabe setzt dort fort.

Strukturelle Grenzen, ausdrücklich keine abgenommenen HA-Latenzbudgets:

* ein Raw-Slot pro Delta; vorhandenes Resolver-Bytebudget 65.536 Bytes;
* maximal 128 Einträge je temporärem Mapping und 256 Zeilenmutationen je Delta;
* 32 Aggregat-Retirements bzw. Ringkürzungen pro Schritt;
* 32 neue CurrentCondition-Beiträge pro Statistikschritt;
* 65.536 Bytes je serialisiertem Einzelwert;
* 200.000 SQLite-VM-Schritte je eigener Schreibtransaktion, zusätzlich
  kooperative Budgetprüfungen bei längeren SQL-Leseoperationen;
* ein bestehendes konfiguriertes Retentionsfenster je exakter Modulreduktion;
* standardmäßig 64 Persistenzschritte pro Worker-Auftrag, explizit 1–256 wählbar.

Delta-Ergebnisse enthalten gemessene Warte-/Transaktionszeit und angenäherte
SQLite-Arbeit. Synthetische Messungen bei 10/100/300 älteren Samples stehen im
externen Prüfprotokoll. Ein separater RawRegistry-Append kann während einer
abgeleiteten Schreibtransaktion fortfahren. Das belegt die getrennten Sperren,
keine CPU-/IO-Isolation oder zulässige HA-Jittergrenze.

## Ausdrücklich offene übergreifende Gates

Keine produktive Aktivierung, kein UTC-Writerwechsel, keine automatische
Migration oder Installation. Hycube-Anbindung, interaktive Reader-Auswahl,
Gesamt-Retention/GC, Speicherreserve und F/R-Artefaktabnahme bleiben getrennt.
Analyse-Pins und MVCC-/Beitragszeilen werden nicht automatisch gesammelt; eine
produktive Kapazitätsfreigabe ist damit nicht erteilt. Nachträgliche Quellen- oder
Identitätskorrekturen erfordern weiterhin die explizite Revisions-/Reconciliation-
Arbeit und dürfen nicht als normale neue Appends ausgegeben werden.

SQLite-FULL und Prozessabbruchtests belegen nicht das Stromausfallverhalten der
produktiven Hardware. Es wurden keine produktiven Daten, Dienste oder Geräte
aufgerufen. Die lokalen Tests sind kein HA-Laufzeit- oder Gesamt-P0-Nachweis.
