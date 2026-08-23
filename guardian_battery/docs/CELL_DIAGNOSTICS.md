# Guardian Cell Diagnostics 0.4.1

## Guardian Battery 0.7.4

Die bestehende statuswirksame Methode bleibt **Phase-Resolved Cell Voltage Consistency** in der festen Reihenfolge Entladung, Tiefbereich, Ladung und Hochbereich. Die Diagnostic Engine bleibt `0.4.12`.

Ergänzende Evidenzen werden getrennt ausgewiesen: historische Ranking-Drift pro Tag und Diagnosebereich, relativer dynamischer Widerstandsindex aus qualifizierten natürlichen Stromsprüngen, relative obere/untere Capacity-Consistency und Vᵢ(Q)-Kurvenkonsistenz für Lade- und Entladesequenzen, relative Ruhe-/Relaxationsdrift, real gemeldeter BMS-Balancing-Status, zeitliche Maintenance-Korrelation und ICA/DVA-Datenbereitschaft ohne aktivierte ICA/DVA-Berechnung. Vᵢ(Q) verwendet integrierten, normierten Q, lineare Interpolation auf einem gemeinsamen Raster und die punktweise Modulmediankurve; außerhalb des belegten Bereichs wird nicht extrapoliert.

Jedes Verfahren kann mit einem konkreten Grund `NICHT BEWERTBAR` liefern. Kapazitäts- und Kurvenauswertung bilden gemeinsam genau eine Evidenzfamilie; Maintenance Risk eskaliert nur durch eine harte Current-Condition-Regel oder mehrere qualifizierte unabhängige, konvergierende Familien. Current-Condition-Confidence und Trend/Risk-Confidence sind getrennt. Letztere basiert zentral auf Datenabdeckung, Beobachtungsdauer, Ereignissen, Reproduzierbarkeit und unabhängigen Familien und ist kein Prozentwert.

Langzeit-Ranking nutzt kompakte, versionierte Tages-/Phasenaggregate mit Config-ID. Sie liegen getrennt von der unveränderten append-only Rohhistorie und sind nach dokumentierter physischer Modulseriennummer getrennt. Maintenance-Ereignisse werden zum Ereigniszeitpunkt über die Positionshistorie aufgelöst; unbekannte Identität bleibt unbekannt. Absolute Zellkapazität, absolute Zellwiderstände, Hersteller-Balancing-Schwellen, Health Score, Restlebensdauer und Ausfallwahrscheinlichkeit werden nicht aus ungeeigneten Daten abgeleitet.

Beim Start gleicht Guardian ab 0.7.1 vorhandene `cell_history/*.jsonl`-Tagesdateien mit dem Backfill-Nachweis in `diagnostic_aggregates.json` ab. Fehlende oder gewachsene Quellen werden jeweils in einem Dateipass unter der aktiven Config-ID nachaggregiert. Bereits teilweise vorhandene Tagesaggregate werden aus der unveränderten Rohdatei kanonisch hergestellt; vollständig belegte unveränderte Dateien bleiben bei folgenden Starts ungeöffnet. Explizite Sample-Seriennummern haben Vorrang, andernfalls gilt ausschließlich die Positionshistorie zum Samplezeitpunkt. Ohne sichere Identität entsteht kein geratenes Aggregat.

Ab 0.7.2 wird zusätzlich der davon getrennte, klassische Current-Condition-Arbeitscache `cell_diagnostics.json` aus geeigneten Rohsamples ergänzt oder rekonstruiert. Datei-, Offset- und Positionshistorien-Signaturen vermeiden wiederholte Vollscans; Cache- und Rohsamples werden anhand physischer Seriennummer, Modulposition und Timestamp dedupliziert und auf `cell_diag_history_max_samples` je Modul begrenzt. Eine explizite Seriennummer im Sample hat Vorrang, sonst gilt allein eine zum Samplezeitpunkt dokumentierte Positionszuordnung. Unsichere Identitäten werden ausgelassen. Current Condition bleibt `CellDiagnosticStore.analyse()` und bewertet den rekonstruierten Ringpuffer mit der aktuell aktiven Konfiguration; es gibt keine As-was-Neubewertung und `diagnostic_aggregates.json` ist keine Current-Condition-Quelle.

Ab 0.7.3 ist dieser Ringpuffer nach physischer Seriennummer statt nach historischer Position begrenzt. Die aktuelle Position dient nur zum Lookup der dort aktuell dokumentierten Seriennummer; `analyse(module_position, ...)` verwendet danach alle sicher identifizierten Samples dieses physischen Moduls über Positionswechsel hinweg. Vorgänger an derselben Position bleiben getrennt. Coverage-Schema 2 erzwingt gegenüber 0.7.2 einmalig einen vollständigen Neuaufbau und prüft neben Dateisignaturen auch Serienidentität, Samplezahl und Zeitbereich des materialisierten Caches. Die korrigierte Samplefolge wird unverändert auch an die vorhandenen Advanced-Evidence-Verfahren übergeben.

Ab 0.7.4 bleiben diese vollständigen Diagnoseobjekte intern für Berechnung, Guardian-UI und Persistenz verfügbar, werden aber nicht mehr unverändert über MQTT serialisiert. Der Gesamtstate enthält kompakte Modulresultate; Zellattribute enthalten begrenzte Status-, Phasen-, Trend-, Risk-, Qualitäts-, Begründungs- und Provenienzprojektionen. Rohsamples, vollständige Methods/Evidence Families, Sequenz- und Kurvenarrays, Maintenance-Kontextlisten und vollständige Aggregate sind keine MQTT-Transportdaten. Die Transportbegrenzung verändert keine Diagnoseformel und keine interne Explainability.

Die konfigurierbaren Quality Gates sind experimentell und benötigen weiterhin empirische Feldvalidierung. Ein nicht bestandenes Quality Gate oder eine unzureichende Datenbasis führt regulär zu `NICHT BEWERTBAR`, nicht zu einer geratenen Diagnose.

Cell Diagnostics is additive to the existing Guardian Battery Health Engine. It does not replace the 0.4.0 stack/module Health Score.

The new diagnostic engine stores `bat <module>` samples and evaluates all 15 cell channels using phase-resolved deviation from the module median and voltage ranks. Each cell exposes an evidence status and confidence. No cell SOH percentage is invented.

The Pylontech `stat` SOH and cycle count remain manufacturer/BMS values and are published separately.

Advanced evidence remains strictly quality-gated. ICA/DVA is limited to data-readiness reporting; no ICA/DVA calculation, absolute capacity, absolute resistance or lifetime prediction is produced.
