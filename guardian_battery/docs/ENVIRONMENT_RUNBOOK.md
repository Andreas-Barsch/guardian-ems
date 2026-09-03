# Guardian EMS – Environment Runbook

Stand: 2026-09-03

## Guardian Battery 0.7.22 – SOC UI Cleanup + Predictive Cell Risk V2

### Versions- und Datenvertrag

- Guardian/Add-on: `0.7.22`; Diagnostic Engine unverändert `0.4.12`.
- Cell Risk: Algorithmus `guardian_cell_risk_v2_1`, Formel `2.0.0`, Klassifikation `1.0.0`.
- Zellidentität ist `physical_serial + cell_number`. Positionen werden ausschließlich zeitbezogen aus Position History projiziert, nie aus ADR oder heutiger Position abgeleitet.
- Daily Derived Data liegt kompakt unter `SHARE_DIR / "diagnostics" / "aggregates" / "cell_risk" / "YYYY-MM-DD.json"`. Raw Cell History wird nicht verändert und nicht bei UI-Aufrufen gescannt.

Read-only API innerhalb des bestehenden dynamischen Guardian-Ingress:

```text
GET api/diagnostics/cell-risk/top10/YYYY-MM-DD
GET api/diagnostics/cell-risk/cell/YYYY-MM-DD/<physical_serial>/<cell_number>
```

POST, PUT, PATCH und DELETE bleiben unzulässig. Cell Risk hat keine Steuer- oder Alarmwirkung und sendet keine Befehle an Pylontech, Hycube, Wechselrichter, Wallbox oder Home-Assistant-Aktoren. Der Score ist kein SOH, keine Ausfallwahrscheinlichkeit und keine Restlebensdauer.

### Source, Installation und produktive Daten getrennt prüfen

1. Source und Add-on-Metadaten müssen `0.7.22` ausweisen.
2. Die installierte Add-on-Version ändert sich erst durch einen separaten Build beziehungsweise eine separate Installation.
3. Produktive Risk-Aggregate entstehen erst nach abgeschlossenen Daily Runs. Fehlende ältere Aggregate bedeuten „Noch nicht ausreichend Daten“ und nicht Nullrisiko.
4. Nach separater Installation SOC-Einzel/Vergleich, Tooltips, gruppierte Legende, Top-10, Zelldetail, Risk History und Rücknavigation auf Desktop und Smartphone real abnehmen.

Der synthetische Entwicklungs-Audit mit 37.061 Modulstichproben ergab etwa 11,105 s initiale Analysezeit, 212,41 MiB Peak-RAM und 149,04 KiB Aggregatgröße; Top-10- und Detail-API lagen bei etwa 0,934 ms beziehungsweise 1,914 ms. Diese Werte sind nicht produktiv gemessen und ersetzen keine kontrollierte Abnahme.

## Guardian Battery 0.7.21 – Hycube Policy Boundaries + SOC Timeline

### Read-only Policyquelle und Polling

- Guardian liest Hycube-Systemwerte ausschließlich über `GET /data_row/` und die Batteriepolicy ausschließlich über `GET /Bat/getCustomBat/`. Beide Pfade sind parameterlose, read-only Requests; Redirects, Login-Automation, Proxyfunktion sowie mutierende Control-Endpunkte sind ausgeschlossen.
- Die Policy-Felder `normalMode`, `bufferMode`, `emergency` und `batProtection` bezeichnen Normalbetrieb, Passiv, Notstrom und Batterieschutz in Prozent. Eine Beobachtung ist nur bei erfolgreichem HTTP, gültigem JSON, vier vorhandenen endlichen numerischen Werten im Bereich 0..100 und exakter Summe 100 autoritativ. Ungültige Beobachtungen ersetzen die letzte gültige Policy nicht.
- Der bestehende Hycube-Collector liest die Policy einmal beim Start und danach alle 300 Sekunden im selben Thread. Es gibt keine parallelen Policy-Requests und keine Policy-Abfrage im Fünf-Sekunden-Takt. Timeout, Verbindungsfehler, ungültiges JSON, ungültige Felder/Summe sowie HTTP 4xx/5xx bleiben vom System-Collector und allen übrigen Guardian-Pfaden isoliert.

### Historie und zeitgültige SOC-Darstellung

- Policy-Evidence wird append-only unter `SHARE_DIR / "hycube_policy_history"` gespeichert. Jede Beobachtung bleibt erhalten; `policy_changed` und `effective_at` machen tatsächliche Änderungen nachvollziehbar.
- Vor der ersten gültigen Beobachtung ist Policy `unavailable`. Danach gilt sie ab ihrem beobachteten Zeitpunkt als `observed` beziehungsweise für spätere Abfragefenster `historically_applicable`. Eine spätere Änderung lässt die alte Policy bis zum Änderungszeitpunkt gelten und wirkt nicht rückwirkend auf frühere Zeiträume.
- Die kumulativen Bereichsgrenzen sind `100-normalMode`, `100-normalMode-bufferMode` und `100-normalMode-bufferMode-emergency`. Für 82/3/10/5 ergeben sich 18 %, 15 % und 5 %. Im SOC-Zeitverlauf heißen sie „Bereichsgrenze Normalbetrieb / Passiv“, „Bereichsgrenze Passiv / Notstrom“ und „Bereichsgrenze Notstrom / Batterieschutz“; Änderungen werden ohne Interpolation stufenförmig gezeichnet.
- Modul-SOCs stammen weiterhin von Pylontech. Hycube Battery Capacity stammt aus `/data_row/`, bleibt eine separate Systemangabe und ist kein verifizierter Stack-SOC oder Modulmittelwert (`aggregation_rule=not_verified`). Policywerte stammen aus `/Bat/getCustomBat/`; die Linien markieren Bereichsübergänge und beweisen keine Abschaltursache.

### Isolation und produktive Abnahme

- Hycube `batProtection` ist nicht Pylontech Under Voltage Protect; Hycube Policy ist weder Pylontech DCL noch Enable. `causality=not_determined`. Cell-/Modul-/Stackstatus, Alarmgrenzen, Confidence, Daily Diagnostics, BMS Management sowie `0x44`-/`0x47`-Semantik bleiben unverändert; die Diagnostic Engine bleibt `0.4.12`.
- History API und UI lesen nur die für das angeforderte Zeitfenster relevanten Hycube-/Policy-Dateien; bestehendes Downsampling bleibt erhalten. Das Release fügt keine Retention, Kompression oder Evidence Story hinzu.
- Nach einer separaten Installation von 0.7.21 sind Policy Readback 82/3/10/5, die Linien bei 18/15/5, Modul-SOCs, Hycube Battery Capacity, Phasenflächen, Legende und Hilfe auf Desktop und Smartphone real abzunehmen. Dieser Source-Release führt kein Deployment aus, verändert keine Hycube Policy und greift nicht auf produktives `/share` oder `/config` zu.

## Guardian Battery 0.7.20 – D1.2a Evidence Acquisition

### Evidence-Schichten und Sicherheitsgrenze

- D1.2a erweitert ausschließlich die Datenerfassung. Die später vorgesehene Evidence Story trennt Cell Evidence, Pylontech Status (`0x44`), Pylontech Thresholds (`0x47`), User/System Policy, Pylontech Management (`0x92`), Module Response, Hycube System Response und eine mögliche spätere externe Systemebene. Es gibt in 0.7.20 keine automatische Ereigniskette, grafische Kausaldarstellung oder KI-Interpretation.
- Gültige, passiv beobachtete `0x47`-Responses können Zell-/Modulspannungsgrenzen, Lade-/Entladetemperaturgrenzen und Lade-/Entladestromgrenzen liefern. Rawwerte und `info_raw` bleiben erhalten. Eine Schwelle gilt nur zum Beobachtungszeitpunkt und wird weder rückwirkend noch ohne reale `0x47`-Evidence angenommen. Physische Identität und historische Position stammen ausschließlich aus zeitgültiger Evidence; es gibt keine ADR→Position-Formel.
- Guardian sendet niemals selbst `0x47`, fragt Thresholds nicht aktiv ab und ergänzt weder RS485- noch Console-Busaktivität. Die vollständige Historisierung greift nur für ohnehin passiv beobachtete Kommunikation.

### Optionaler Hycube-Collector

- Hycube Evidence ist global mit `hycube_evidence_enabled=false` deaktiviert. Nur ein echter Boolean `true` aktiviert den Collector; fehlendes Feld, `false`, Strings und numerische Werte tun dies nicht. Ohne explizites Enable entstehen kein Worker, DNS-/HTTP-Versuch, History, Retry oder fehlende-Konfiguration-Warnung.
- Nach Enable liest genau ein nicht überlappender Collector ausschließlich `GET /data_row/`. Die lokale Basisadresse ist konfigurierbar; Redirects, Login-Automation, Proxyfunktion und Control-Endpunkte bleiben verboten. Insbesondere werden `/Wallbox/batteryDischargingPermission/`, `/update/updateControlValue/`, `/Bat/setCustomBat/` und andere mutierende Endpunkte nicht verwendet, auch wenn ein solcher Endpoint technisch GET verwendet.
- Das konstante Defaultintervall beträgt fünf Sekunden und ist von 1 bis 60 Sekunden konfigurierbar. Es gibt keine adaptive oder ereignisabhängige Umschaltung auf 1 Hz. Jeder erfolgreiche Record enthält `received_at`, `device_timestamp`, Zeitqualität, `configured_interval_seconds`, `actual_interval_seconds` und `actual_interval_quality`.
- Gespeichert werden der empfangene Body, sein SHA-256 und, soweit vorhanden, `BatteryPower`, `BatteryCapacity`, `GridPower`, `HomePower`, `solarPower`, `ExternalPower` und `Date2`. Unbekannte Payloadfelder bleiben im Body erhalten. Der Responsebody ist auf 1 MiB begrenzt; eine Überschreitung bleibt im Collector isoliert und erzeugt keinen partiellen History-Record.

### User/System Policy und Zeitverträge

- User/System Policy ist eine eigene Evidence-Ebene und wird nicht mit Pylontech DCL/CCL, Enable, Cell Evidence oder der Hycube-Systemreaktion gleichgesetzt. Aktuell ist keine read-only Quelle für Notstromreserve, Minimum-SOC, globale Entladegrenze, globale Discharge Permission oder Battery Operating Mode verifiziert. Deshalb gilt `policy_evidence=unavailable` mit `policy_evidence_reason=no_verified_read_only_source`.
- Sobald später eine verifizierte Policy-Quelle vorliegt, muss Guardian Diagnostics Wert, Einheit, Quelle, Beobachtungszeit und zeitliche Gültigkeit anzeigen. Ein heutiger Policy-Wert darf niemals rückwirkend auf historische Ereignisse angewendet werden und beweist keine Ursache für DCL/CCL.
- Cell History unterscheidet `cell_sample_at` und `pwr_sample_at` und weist `pwr_age_seconds` mit Qualitätsstatus aus. Fehlender PWR-Zeitpunkt ist `unavailable`, ein zukünftiger PWR-Zeitpunkt `invalid_future`; ein negativer Wert wird nicht als gültiges Alter gespeichert. Alte History bleibt lesbar.

### Forensische Referenz und Diagnose-Isolation

- Für den 31.08.2026 bleiben sieben DCL→0-Ereignisse bei `Y225004C32250226`, C8 als Lowest Cell bei 7/7, Discharge Enable `TRUE`, maximal 398 mV Spread und minimal 2.884 V für C8 belegt. Gleichzeitig enthielt `0x44` bei 0/7 keinen dokumentierten Cell-/Module-Low-Voltage-/UV-Alarm oder Protect. DATAFLAG `0x11→0x00` ist kein Low-Voltage-Bit; trotz zeitlicher Folge und 7/7 Korrelation existiert mindestens ein Gegenbeispiel ohne anschließendes DCL→0. `causality=not_determined` bleibt verbindlich.
- D1.2a verändert keine Cell-, Modul- oder Stackbewertung, Alarmgrenze, Confidence, Daily-Diagnostic- oder BMS-Management-Bewertung. `0x47` und Hycube sind ausschließlich Evidence Acquisition; die Diagnostic Engine bleibt `0.4.12`.

### Speicher und kontrollierte produktive Abnahme

- Die synthetische Abschätzung eines repräsentativen Hycube-Records beträgt etwa 1.067 Byte. Bei fünf Sekunden entspricht dies ungefähr 17,58 MiB/Tag, 527,51 MiB/30 Tage und 6,27 GiB/Jahr. Diese Werte sind nicht produktiv gemessen. History wächst kontinuierlich; reale Payloadgröße, Wachstum, CPU und RAM müssen in einem kontrollierten Pilot bestimmt werden. Langfristige Retention beziehungsweise verlustfreie segmentierte Kompression bleibt offen.
- Nach Installation 0.7.20 zunächst den normalen Betrieb mit `hycube_evidence_enabled=false` verifizieren. Ein späterer separater Pilot darf erst danach Basisadresse und Collector kontrolliert aktivieren und muss Payload, Feldsemantik, Zeit, Vorzeichen, Failure Isolation und Speicherwachstum prüfen. Kein Control-Endpunkt darf verwendet werden.
- Dieses Source-Release führt kein Deployment aus, aktiviert Hycube nicht und greift nicht auf produktives `/share`, `/config` oder `/data_row/` zu.

## Guardian Battery 0.7.19 – Guardian Diagnostics Ingress Navigation Fix

### Zweck, Datenquelle und aktive Komponente

- Guardian Diagnostics macht abgeschlossene Daily Diagnostics in einer eigenständigen Gesamtübersicht, einer nach Datum sortierten Tagesliste und validierten Tagesdetails sichtbar. Benutzer müssen dafür keine Derived-JSON-Dateien oder Worker-Logs lesen.
- V1 verwendet ausschließlich die aktive deterministische Komponente **BMS Management Evidence** und bleibt architektonisch für weitere Daily Components offen. Es enthält keine KI und keine neue Diagnosebewertung.
- API und UI lesen ausschließlich bereits erzeugte Derived Data unter `/share/guardian_battery/diagnostics`. Browserzugriffe analysieren weder `cell_history` noch `rs485_history` neu und starten keinen Daily Run, Backfill oder Worker-Vorgang.

### Read-only API und Sicherheitsvertrag

Erlaubte GET-Routen:

```text
/api/diagnostics/overview
/api/diagnostics/days
/api/diagnostics/daily/YYYY-MM-DD
/api/diagnostics/bms-management/aggregate/YYYY-MM-DD
/api/diagnostics/bms-management/events/YYYY-MM-DD
```

- POST, PUT, PATCH und DELETE werden mit `405` abgelehnt. Es existieren keine Runtime- oder Anlagensteuerungsendpunkte.
- Datumswerte müssen kanonisches `YYYY-MM-DD` sein. Pfad-Traversal, freie Resultdateireferenzen und Symlink-Escapes werden verhindert. Resultrevisionen werden mit demselben vollständigen Indexvertrag wie im Daily Worker validiert.
- Browser-DTOs enthalten keine absoluten Raw-History-Pfade oder Rawframes; HTML/Text wird escaped und Eventantworten sind begrenzt. GET verändert weder Resultrevision, Event Store, Aggregate noch Worker-State.

### Datenqualität und Evidence-Semantik

- `complete`: vollständig gemäß den aktiven Components analysiert.
- `partial`: verwertbares Tagesresultat bei eingeschränkter Datenlage; null Ereignisse dürfen nicht als unauffälliger vollständiger Tag interpretiert werden.
- `failed`: kein gültiges neues Tagesresultat. Dies bezeichnet einen Analysefehler und keinen kritischen Batteriezustand. Da fehlgeschlagene Attempts derzeit nicht vollständig historisch je Tag persistiert sind, bleibt eine historische Failed-Liste offen und wird nicht erfunden.
- Fehlende Tage sind keine Nulltage. 7-/30-Tage-Fenster aggregieren nur vorhandene autoritative Derived Results; bei unzureichender Historie bleibt der Trend „noch nicht bestimmbar“.
- `physical_serial` ist die primäre Identität. Eine angezeigte historische Position stammt ausschließlich aus zeitgültiger Tages-Evidence und niemals aus der heutigen Zuordnung.
- CCL und DCL sind die vom adressierten BMS gemeldeten Management-Limitwerte, nicht automatisch lokales Zell-, Stack- oder Wechselrichterlimit. Numerisches Limit und Enable sind getrennte Evidence: CCL=0 bei Charge Enable sowie DCL=0 bei Discharge Enable können beobachtet werden.
- Cell Context, Current Context, rekonstruierter Stackstrom, Lowest Cell, Coverage und gap-qualifizierter Duty Cycle werden nur aus vorhandenen Derived Data projiziert. Rohe `0x44`-Byteübergänge bleiben uninterpretierte zeitliche Korrelation und werden nicht als Protection, MOSFET, Shutdown oder Fault bezeichnet. `causality=not_determined` bleibt verbindlich.

Als reales, nicht hardcodiertes Beispiel wurde am 02.09.2026 CCL `10 / 10 / 5 / 0 / 0 / 0 A` bei Charge Enable `ENABLED` für alle sechs Module und DCL `-25 A` beobachtet. Dies belegt ausschließlich die Trennung von Limit und Enable und keine Ursache oder Produktregel.

### Ingress-Navigation und Deployment-Separation

- Guardian Diagnostics ist ein interner Bereich des bestehenden Guardian-Ingress und wird dort relativ über `diagnostics` geöffnet.
- Der bestehende Guardian-Seitenmenüpunkt erzeugt die dynamische Supervisor-Ingress-Sitzung. Der aktuelle Prefix wird aus `X-Ingress-Path` übernommen; Add-on-Slug und Session-Token sind nicht austauschbar.
- Die frühere Dashboard-Source `homeassistant/dashboards/guardian_diagnostics.yaml` und ihre Registrierung wurden entfernt. Ein YAML-iframe ist kein direkter Ingress-Proxy und darf keinen statischen Add-on-Slug als Token persistieren.
- Ein wirklich eigener HA-Seitenmenüpunkt bleibt einer späteren ingress-aware Frontendfunktion vorbehalten. D1.1 verwendet keinen zweiten Server, Port oder Direktzugriff.
- Falls D1 bereits produktiv konfiguriert wurde, den Block `guardian-diagnostics` nach Backup kontrolliert aus `/config/configuration.yaml` entfernen. Danach `/config/dashboards/guardian_diagnostics.yaml` sichern und kontrolliert entfernen oder ungenutzt belassen. Diese Schritte erfolgen separat und nicht durch ein Add-on-Update.

  ```yaml
  guardian-diagnostics:
    mode: yaml
    title: Guardian Diagnostics
    icon: mdi:stethoscope
    show_in_sidebar: true
    filename: dashboards/guardian_diagnostics.yaml
  ```

### Produktiv verifizierte Referenz vom 31.08.2026

Der vollständigere Daily Run für `physical_serial=Y225004C32250226` verwendete 4.337 RS485- und 6.830 Cell-Records. Er reproduzierte sieben DCL-Zero-Ereignisse, sieben Recoveries, siebenmal DCL Zero trotz Enable, C8 als Lowest Cell bei 7/7 Ereignissen, das dominante rohe Muster `offset:0:11->00` mit Ratio `1,0`, maximal 398 mV Spread, minimal 2.884 mV Cell Voltage und etwa 52,313 s gap-qualifizierte DCL-Zero-Zeit. Der produktive DCL-Zero-Duty-Cycle betrug etwa `0,564 %`.

Die früheren `1,896 %` waren das Ergebnis einer kleineren manuellen Teil-Evidence mit kleinerem Coverage-Nenner, kein Fehler der Daily Engine. Die sieben Ereignisse wurden mit vollständigerer Tages-Evidence unverändert reproduziert. Endpoint-Dauer und gap-qualifizierte Dauer/Coverage bleiben unterschiedliche Größen; aus diesen Beobachtungen folgt keine Ursache.

### Offene reale Abnahme nach D1.1

- Produktive API gegen echte Derived Data sowie Gesamtübersicht, Tagesauswahl und Tagesdetails für 31.08./30.08. prüfen.
- Produktive D1-Dashboardregistrierung kontrolliert bereinigen; danach interne Diagnostics-Navigation, API-Aufrufe und Rücknavigation innerhalb derselben Ingress-Sitzung auf Desktop und Smartphone prüfen.
- Reale 7-/30-Tage-Fenster, `partial`-Darstellung und unveränderte Live-Acquisition/Worker-Ausführung verifizieren. Dieses Source-Release führt kein Deployment aus.

## Guardian Battery 0.7.17 – Deterministic Daily Diagnostics

### Zweck und Sicherheitsgrenze

- 0.7.17 erzeugt ausschließlich deterministische Derived Data aus bereits gespeicherter Evidence. Es enthält keine KI-Interpretation und verändert keine Raw History.
- Daily Diagnostics sendet keine RS485 Writes, Console Commands, MQTT Commands oder Hycube Actions. Der RS485-Pfad bleibt passiv.
- Cell Status, Diagnostic Confidence, Maintenance Risk, Phasenklassifikation, Gesamtbewertung, bestehende Diagnosegrenzen und relative Endpoints bleiben unverändert; die Diagnostic Engine bleibt `0.4.12`.
- Es gibt in 0.7.17 noch keine Daily-Diagnostics-UI, Daily-Result-MQTT-Projektion oder HTTP/API für Daily Results. First-Deployment-Observability erfolgt über strukturierte Logs und read-only Inspection der Derived Files.

### BMS Management Evidence

- Primäre Identität ist `physical_serial`; Positionen werden ausschließlich anhand der zum Evidence-Zeitpunkt gültigen Position History aufgelöst. Es gibt keine ADR→Position-Formel.
- Deterministisch erfasst werden CCL-/DCL-Reduktionen, Zero Events und Recoveries sowie `Limit=0` trotz Charge-/Discharge-Enable. Absolute Werte und peer-relative CCL-/DCL-Abweichungen bleiben getrennt; `25 A` oder ein anderer Wert wird nicht als feste Normalgrenze hardcodiert.
- Eventkontext umfasst Cell History, Lowest Cell, Cell Median Deviations, Spread, Module Current, rekonstruierten Stackstrom mit Provenienz und rohe `0x44`-Korrelation ohne angenommene Bitsemantik. Daily Aggregates fassen die beobachtete Evidence zusammen. `causality=not_determined` ist verbindlich; daraus folgt keine Kausaldiagnose.

### Daily Core und Guardian-Tag

- Der fachliche Tag gilt in `Europe/Berlin` als halb offenes Intervall `[day_start, day_end)`. Timestamp-basiertes Slicing behandelt Sommer-/Winterzeit korrekt und erlaubt je nach DST 23-, 24- oder 25-Stunden-Tage.
- Semantische Input-Fingerprints und physische Source-Provenienz identifizieren den gelesenen Evidence-Stand. Ergebnisse besitzen deterministische IDs und werden als immutable Revisionen atomar persistiert; der BMS-Event-Store ist idempotent.
- Komponenten bleiben gegeneinander isoliert. Der Gesamtstatus unterscheidet `complete`, `partial` und `failed`. Neue oder geänderte Late Data kann bei verändertem Fingerprint eine neue Resultrevision auslösen.

### Output Root und Dateistruktur

Produktiver Derived-Data-Pfad:

```text
/share/guardian_battery/diagnostics/
├── daily/
├── events/
│   └── bms_management/
├── aggregates/
│   └── bms_management/
├── state/
└── locks/
```

Dieser Baum enthält Derived Data, nicht Raw History. 0.7.17 führt noch keine automatische Retention für Daily Results, BMS Event Store oder BMS Aggregates ein. Das ist ein bekannter Non-Blocker; eine Regel wird erst nach realer Größenbeobachtung festgelegt.

### Worker Lifecycle und Scheduling

- `DailyDiagnosticWorker` läuft in einem eigenen isolierten Background Thread und startet erst nach Initialisierung/Start der wesentlichen Live-Acquisition. Sein Fehlerpfad darf Main Poll, Console, RS485, MQTT oder Hycube nicht steuern oder blockieren.
- Checkintervall: 5 Minuten. Ein abgeschlossener Tag wird frühestens nach 15 Minuten Grace Period und nach zwei stabilen Fingerprint-Beobachtungen verarbeitet.
- Initial Catch-up: maximal drei priorisierte vergangene Tage. Danach wird pro Zyklus maximal ein weiterer Backlog-Tag verarbeitet. Die automatische Historie ist auf die letzten sieben vergangenen Tage begrenzt; aktueller und zukünftiger Tag sind ausgeschlossen.
- Das Late-Data-Fenster umfasst drei Tage. Ein gegen den vollständig validierten Result Index veränderter Fingerprint markiert einen Tag als stale und erlaubt eine neue immutable Revision.
- Persistenter Worker-State und Crash Recovery erkennen einen unterbrochenen Versuch. Der vollständig gegen seine Resultrevision validierte Result Index ist die fachliche Wahrheit für bereits abgeschlossene Ergebnisse.

### First-Deployment-Logs

Die implementierten Suchbegriffe sind:

```text
Daily diagnostics worker starting
Daily diagnostics worker started
Daily diagnostics catch-up
Daily diagnostics candidate
waiting_for_grace
stability reset
Daily diagnostics unchanged
Daily diagnostics started
Daily diagnostics completed
Daily diagnostics stale
Daily diagnostics source changed
Daily diagnostics probe failed
Daily diagnostics index invalid
Daily diagnostics run failed
Daily diagnostics worker state write failed
Daily diagnostics interrupted attempt recovered
```

Die Logs enthalten kompakte Konfiguration, Kandidaten-/Stability-/Catch-up-Angaben, `diagnostic_date`, Attempt-/Result-ID, Status, Dauer, Fingerprint-Präfix, Component Summary, Event Count, Quality und Coverage. Sie enthalten keine Rawframes, vollständigen Cell Arrays oder vollständigen Eventpayloads.

### Read-only First-Deployment-Inspection

Die folgenden Befehle lesen ausschließlich; sie legen nichts an und bereinigen nichts automatisch:

```sh
ls -la /share/guardian_battery/diagnostics
find /share/guardian_battery/diagnostics -type f
sed -n '1,160p' /share/guardian_battery/diagnostics/state/daily_job_state.json
grep -R '"overall_status"' /share/guardian_battery/diagnostics/daily
sha256sum /share/guardian_battery/diagnostics/daily/*/*.json
```

Bei der ersten realen Abnahme getrennt prüfen: Workerstart nach Live-Acquisition, begrenzten Catch-up, niemals den aktuellen Tag, Output- und Indexstruktur, `complete`/`partial`/`failed`, Late-Data-/Stale-Verhalten, unveränderte Raw History sowie CPU-, RAM- und I/O-Auswirkung auf die Live-Acquisition.

### Reale Referenzevidence für den 31.08.2026

Für `physical_serial=Y225004C32250226` wurde manuell verifiziert:

- sieben DCL-Zero-Ereignisse und sieben Recoveries;
- DCL=0 trotz Discharge Enable bei 7/7 Ereignissen;
- C8 als Lowest Cell bei 7/7 Ereignissen;
- rohe `0x44`-Korrelation Offset 0 `11→00` bei 7/7 Ereignissen;
- endpoint-basierte Nullphasen von ungefähr `2.018,857 s`;
- gap-qualifizierte DCL-Zero-Zeit von ungefähr `52,313 s`;
- Management Coverage von ungefähr `2.759,740 s`;
- gap-qualifizierter Duty Cycle des vollständigen produktiven Daily Runs von ungefähr `0,564 %` (`dcl_zero_duty_cycle≈0,00564049`). Dieser Lauf verwendete 4.337 RS485- und 6.830 Cell-Records. Die früher genannten `1,896 %` stammten aus einer kleineren manuell ausgewerteten RS485-Teilmenge und sind nicht der vollständige Tageswert. Die sieben Ereignisse wurden in beiden Auswertungen reproduziert.

Endpoint-Dauer und gap-qualifizierte Duty-Cycle-Evidence sind unterschiedliche Messgrößen und dürfen nicht gleichgesetzt werden. Die Korrelation belegt keine Ursache.

### Abort Criteria und Rollback

Rollback erwägen, wenn Main Poll oder Console stoppt, der RS485 Reader unerwartet endet, der Worker in eine Exception Loop gerät, mehr als drei Initial-Catch-up-Tage verarbeitet werden, der aktuelle Tag analysiert wird, automatische Verarbeitung außerhalb des Sieben-Tage-Horizonts erfolgt, ein ungültiger Result Index wirksam wird, Raw History verändert wird, CPU/RAM/I/O die Live-Acquisition beeinträchtigt oder RS485-Passivität verletzt wird.

Rollback-Ziel ist 0.7.16. Dabei Raw History, Position History, Config History und Maintenance History weder löschen noch verändern. `diagnostics/` als Derived Data bestehen lassen; 0.7.16 ignoriert diesen Baum. Source-Release, installiertes Add-on und produktive Daten bleiben getrennte Zustände.

### Offene reale Abnahme

- Workerstart, Grace Period, Stability, Catch-up-Grenzen, Late-Data-Revisionslauf, Crash Recovery und Shutdown im produktiven Add-on beobachten.
- Derived Files und Logs gegen die Referenzevidence vom 31.08.2026 prüfen.
- Bestätigen, dass Main Poll, Console, RS485 Reader und Live-Acquisition unter realer CPU-/RAM-/I/O-Last unverändert weiterlaufen.
- Retention nach real beobachteter Datenmenge separat definieren. Es erfolgt in diesem Source-Release kein Deployment und kein Zugriff auf produktives `/share` oder `/config`.

## Guardian Battery 0.7.16 – RS485 Management Timestamp Contract Hotfix

### Realer Fehler in 0.7.15

- Die passive RS485-Acquisition und der Evidence Writer liefen weiter; produktiv wuchs die RS485 Evidence auf rund 31 MB beziehungsweise etwa 28.990 Records.
- `project_current_management()` überschrieb jedoch den direkten numerischen 0x92-Sample-Zeitstempel beim pauschalen Dict-Merge mit dem aktuellen `datetime` des Identity Resolvers. Die MQTT-Freshness scheiterte dadurch an `float(datetime)`, und `/api/rs485/status` konnte denselben Management-Payload nicht als JSON serialisieren.
- Als Folge konnten RS485-/BMS-Managementzeilen leer bleiben und der Poll-Zyklus vor der nachgelagerten Position-History-Confirmation abbrechen, obwohl Reader und Evidence-Erfassung weiter funktionierten.

### Vertrag ab 0.7.16

- `management[ADR]["timestamp"]` ist der direkte numerische 0x92-Sample-Zeitpunkt (`int` oder `float`). Identity Resolution darf diesen Messwert-Zeitpunkt nicht überschreiben.
- Identity-Felder werden explizit projiziert. Resolver-Timestamp und Resolver-Quality werden nicht in das Management-DTO übernommen; ADR und 0x92-Frame-/Decoder-Quality bleiben direkte Management-Provenienz.
- Das MQTT-/API-Management-DTO enthält weder `datetime` noch Rawbytes und ist vollständig JSON-serialisierbar. Rawframes bleiben unverändert im Reader-/Evidence-Layer.
- MQTT-IDs, Topics, Discovery, Availability, RS485-Decodierung und Passivität bleiben unverändert. Diagnosemethodik und Diagnostic Engine bleiben `0.4.12`.

### Nach Installation von 0.7.16 noch real abzunehmen

- RS485-Status bleibt `listening` und die Managementzeilen sind wieder sichtbar.
- Modul-/Seriennummerauflösung, DCL/CCL, Enable-Zustände, CVL/DVL, Freshness und Last Update werden korrekt projiziert.
- Es treten weder `float(datetime)` noch ein datetime-bezogener JSON-Serialisierungsfehler auf.
- Nach einem vollständig erfolgreichen Poll wird die Position-History-Confirmation weiterhin erreicht; die Integritätsregeln aus 0.7.15 bleiben wirksam.

## Guardian Battery 0.7.15 – Position History Integrity + Diagnostic Topology UI

### Reale Ursache des fehlerhaften Snapshots vom 01.09.2026 um 18:29

- Partielle Polls galten fälschlich als gesund und durften dadurch History-Kandidaten bestätigen. Positionen 2–5 wurden in diesem unvollständigen Zustand als Removal-Kandidaten behandelt.
- Der bereits produktiv gespeicherte fehlerhafte Snapshot wird durch 0.7.15 weder verändert noch gelöscht. Die Korrektur verhindert künftig, dass partielle Startup-/Poll-Zustände oder ein Poll mit Exception als bestätigte vollständige Topologie in die Position History gelangen.
- Ab 0.7.15 darf erst ein vollständig erfolgreicher Poll History-Kandidaten bestätigen. Reconnect und Poll-Exception setzen Guard beziehungsweise Kandidaten zurück. Removal erfordert weiterhin drei gesunde Einzelbeobachtungen über mindestens 30 Sekunden und genau eine erwartete fehlende Position; Addition und Reintegration bleiben nach denselben konservativen Bestätigungsregeln möglich.

### Cell Diagnostics und Topologie

- Ein aktuell beobachtetes Zusatzmodul bleibt vollständig diagnostizierbar. Bei `present + not_expected` bleiben Diagnosewert und Diagnosefarbe unverändert; `NICHT ERWARTET` wird als unabhängiger Topologiehinweis dargestellt.
- `stale`, `absent`, `not_expected + absent` und `unknown` werden getrennt projiziert. Veraltete oder nicht aktuelle Zustände werden nicht als frische Diagnose ausgegeben. Für ein vorhandenes Zusatzmodul bleiben die 15-Zellen-Detailsicht, Topologiestatus und beobachtete Seriennummer verfügbar.
- Diagnosemethodik und Diagnostic Engine bleiben `0.4.12`. RS485 bleibt passiv; Missing-Alarme richten sich weiterhin ausschließlich nach der Solltopologie.

### Source-, Add-on- und Dashboard-Abnahme

- Die beiden Source-Dashboards `guardian_battery/dashboards/guardian_cell_diagnostics.yaml` und `homeassistant/dashboards/guardian_cell_diagnostics.yaml` gehören zum Release. Ein Add-on-Update aktualisiert die produktive Home-Assistant-Dashboarddatei unter `/config` nicht automatisch.
- Deshalb sind getrennt zu verifizieren: Git-/Source-Stand, installierte Add-on-Version und produktive Dashboarddatei unter `/config` einschließlich tatsächlicher Browserdarstellung. Dieses Release führt kein `/config`-Deployment aus.

### Nach Installation von 0.7.15 noch real abzunehmen

- Nach Restart entsteht kein neuer falscher partieller Position-History-Snapshot.
- Eine stabil bestätigte Reintegration von M6 erzeugt genau einen vollständigen Snapshot.
- M6 wird bei `present + not_expected` aktuell diagnostiziert, separat gekennzeichnet und seine 15-Zellen-Detailsicht bleibt erreichbar.
- Die produktive Dashboarddatei wird kontrolliert und separat aus der passenden 0.7.15-Source aktualisiert; Source, Add-on und produktives Dashboard werden anschließend unabhängig verifiziert.

## Guardian Battery 0.7.14 – Runtime Identity / Discovery Hotfix

### Reale Befunde vor dem Fix am 01.09.2026

- Die Hycube-App meldete sechs physisch eingebaute Module, während Guardian mit `module_count=5` nur die Sollpositionen verarbeitete. Der Source-Audit bestätigte, dass `parse_pwr()` alle Modulnummern oberhalb von `module_count` verwarf. Damit war M6 von Console-Folgeabfragen, Presence, Zellwerten und nachgelagerten Projektionen ausgeschlossen.
- Zusätzlich trat real `TypeError: float() argument must be a string or a real number, not 'datetime.datetime'` auf. `resolve_rs485_identity()` lieferte den Zeitpunkt der Positionsauflösung als `datetime`; beim Merge überschrieb dieser Wert den numerischen direkten 0x93-Frame-Timestamp. Der Fehler trat nach PWR-Auswertung, Stat-/Info-Verarbeitung und Console-Presence auf und brach den späteren Identity-/History-/Diagnose-/MQTT-Teil des Poll-Zyklus ab. Der Source belegt nicht, dass dauerhaft nur Modul 1 abgefragt wurde.
- Die Seite „Aktuelle Zuordnung“ zeigte für Position 1–5 passende dokumentierte und beobachtete Seriennummern, gleichzeitig aber vier Abweichungen. Die Tabelle verwendete `last_documented_serials()`, der Banner dagegen den jüngsten vollständigen Snapshot `current().positions`; dadurch konnten Position 1–4 sichtbar übereinstimmen und intern dennoch als abweichend zählen.

### Semantik und Sicherheitsgrenzen ab 0.7.14

- Discovery/Acquisition verarbeitet alle vom Console-`pwr` tatsächlich gemeldeten Module. `module_count` bleibt ausschließlich Solltopologie und Missing-Grenze. Bei Soll=5 und physisch sechs Modulen sind M1–M5 `expected=true, present`, M6 ist `expected=false, present` und wird als „vorhanden, aber nicht erwartet“ dargestellt.
- M6 bleibt für Console-, Zell-, RS485-Identitäts-, MQTT- und History-Daten verfügbar, erzeugt aber keinen Missing-Alarm. Home projiziert primär `5 / 5` und kennzeichnet ein zusätzlich erkanntes Modul separat.
- Direkte numerische 0x93-Zeitstempel bleiben beim Identity-Merge erhalten. Numerische, ISO- und `datetime`-Werte werden kontrolliert normalisiert; ein fehlerhafter Identitätseintrag wird isoliert und beendet nicht den gesamten Poll-Zyklus.
- Tabelle und Abweichungsbanner vergleichen dieselbe zuletzt dokumentierte Seriennummernprojektion. Nicht beobachtete Module werden über Presence/Availability beschrieben und nicht allein deshalb als Identitätsabweichung gezählt.
- Ein stabil bestätigter Wiedereinbau kann nach den bestehenden konservativen Regeln genau einen vollständigen Positionssnapshot erzeugen; `expected=false` bleibt davon unabhängig, solange `module_count=5` gilt. Es werden keine historischen Ereignisse manuell erzeugt.
- Diagnosemethodik und Diagnostic Engine bleiben `0.4.12`. Der RS485-Pfad bleibt ohne Schreib-, Sende- oder Pollinglogik.

### Nach Installation von 0.7.14 noch real abzunehmen

- Guardian erkennt und verarbeitet alle sechs Module; Home zeigt `5 / 5` plus ein zusätzliches Modul; M6 erscheint present/not expected, bleibt messbar und erzeugt keinen Missing-Alarm.
- RS485-Management ist nach vollständigem Poll-Zyklus verfügbar, ein stabiler Wiedereinbau erzeugt genau einen passenden Position-History-Snapshot und der Abweichungsbanner stimmt mit der sichtbaren Zuordnungstabelle überein.
- Weiter offen bleiben der physische Waveshare-Disconnect/Reconnect und ein möglicher hardware- oder treiberbedingter Open-Pegelimpuls. Source-Release, installierte Add-on-Version, produktive `/config`-Dateien und Browserzustand bleiben getrennte Zustände.

## Guardian Battery 0.7.13 – Unified Live Topology & RS485 Identity Restore

- Ausgangspunkt der realen Abnahme war 0.7.12. Die zentrale Presence-Projektion „Aktuelle Zuordnung“ stellte die erwartete Topologie bereits korrekt dar; andere Live-Projektionen verwendeten teilweise noch abweichende Nenner-, Availability- und Diagnose-Semantik.
- Ab 0.7.13 verwenden Home-Zusammenfassung, Live-Messwerte, Cell-Diagnostics und Inventarstatus dieselbe zentrale Solltopologie/Presence. `module_count` bestimmt die erwarteten und alarmwirksamen Positionen. Historisch bekannte Module oberhalb davon bleiben Inventar beziehungsweise History und sind `not_expected`.
- Retained numerische und diagnostische Zustände werden nicht gelöscht oder durch Nullwerte ersetzt. Presence steuert ihre aktuelle Availability, sodass `stale`, `absent` und `not_expected` nicht als frische Livewerte erscheinen. Stammdaten und Recorder-Historie bleiben erhalten.
- Der 0x93-Reader-State war in 0.7.12 prozesslokal und nach einem Add-on-Neustart leer. 0.7.13 liest beim aktivierten Sniffer einmalig und begrenzt vorhandene append-only `rs485_history`-Evidence und rekonstruiert daraus mit dem zentralen Decoder die jüngste gültige bekannte Identität je ADR. Die Evidence-Dateien werden dabei nicht verändert.
- Eine restaurierte Identität bedeutet `identity_known=true`, aber `identity_currently_confirmed=false`; sie erzeugt keine frische Presence. Erst ein neues gültiges direktes `0x93` bestätigt die Identität für den laufenden Prozess oder ersetzt sie. Positionen werden weiterhin ausschließlich über die dokumentierte Positionshistorie und niemals aus ADR-Arithmetik aufgelöst.
- Die Source-Dashboarddatei `homeassistant/dashboards/guardian_module_information.yaml` gehört zu 0.7.13. Ein Add-on-Update kopiert sie nicht automatisch in die produktive Home-Assistant-Konfiguration. Nach Installation sind deshalb getrennt zu prüfen: Git-/Source-Stand, Add-on-Version, produktive Dashboarddatei unter `/config` und die tatsächliche Browserdarstellung. Dieses Source-Release deployt keine Datei nach `/config`.

### Nach dem 0.7.13-Source-Release weiterhin offene reale Abnahme

- Real zu prüfen bleiben Home `4 / 5`, Availability von M5/M6, Cell-Diagnostics-Liveprojektion, Inventory-Presence, Startup-Restore der bekannten 0x93-Identität, deren anschließende Live-Bestätigung durch ein neues `0x93`, lokale Uhrzeit in der Positionshistorie und die Aktualisierung bestehender MQTT-Discovery-Entities.
- Ebenfalls offen bleiben der physische Waveshare-Disconnect/Reconnect und ein möglicher hardware- oder treiberbedingter Open-Pegelimpuls.
- Source-Stand, installierte Add-on-Version, produktive Dashboarddateien und Browserzustand sind getrennte Zustände. Eine reale Abnahme darf erst nach dem jeweiligen kontrollierten Deployment als bestanden dokumentiert werden.

## Guardian Battery 0.7.8 – Passive RS485 Runtime Foundation

### Verifizierte Hardware- und Protokollfakten

- **HA-Host verifiziert:** Waveshare USB TO RS485 als `/dev/ttyACM0`, stabiler Hostpfad `/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B97005529-if00`, VID:PID `1A86:55D3`.
- **HA-Host verifiziert:** bestehende Pylontech Console über `/dev/ttyUSB0` mit Prolific-basiertem USB-Serial-Adapter. Die genaue Prolific-VID/PID ist nicht dokumentiert.
- **Supervisor-Konfiguration verifiziert:** `uart: true`, `usb: false` und keine expliziten Gerätezuordnungen (`devices: []` in der App-Info).
- **Mac verifiziert:** passiver RS485-Mitschnitt mit 115200/8N1, gültigen Pylontech-Prüfsummen sowie realen 0x42- und 0x92-Frames. CCL, DCL und Charge-/Discharge-Enable-Bits wurden beobachtet.

### Release- und Sicherheitsgrenzen

- Console und RS485 sind getrennte Rollen. Waveshare `1A86:55D3` ist für Console-auto ausgeschlossen; Mehrdeutigkeit und identische aufgelöste Device-Nodes werden fail-closed behandelt.
- `rs485_sniffer_enabled` ist standardmäßig `false`. Direkt nach Installation wird ohne manuelle Aktivierung kein RS485-Port geöffnet.
- Der RS485-Runtimepfad liest ausschließlich, besitzt keinen Protokoll-Schreibpfad und erzeugt keine 0x94-/0x95-Kommandos.
- pyserial öffnet das POSIX-Gerät intern mit `O_RDWR`; Guardian setzt DTR und RTS vor `open()` auf `False`, aktiviert keinen RS485-Sendemodus und ruft keinen Schreibpfad auf.
- **Real auf Home Assistant verifiziert:** Prolific-Console und Waveshare-RS485 sind im Guardian-Container parallel sichtbar; beide stabilen `by-id`-Pfade sind verfügbar. Die bestehende Console-Acquisition läuft weiter, während der passive Reader im Zustand `listening` gültige 0x42- und 0x92-Frames empfängt.
- **Real auf Home Assistant verifiziert:** Eine beobachtete 0x92-Antwort für ADR `02` ergab CCL `0,0 A`, DCL `-25,0 A`, Charge Enable `false` und Discharge Enable `true`; sie stimmt mit dem unabhängigen Mac-Mitschnitt überein.
- Noch **nicht produktiv verifiziert** sind der physische Waveshare-Disconnect/Reconnect und ein möglicher hardware-/treiberbedingter Pegelimpuls beim Öffnen.
- 0.7.8 enthält keine RS485-Persistenz, MQTT-/HA-Projektion, UI-Integration oder Diagnosekopplung. `/share/guardian_battery` bleibt außerhalb dieses Release-Arbeitspakets.

## Guardian Battery 0.7.9 – RS485 Evidence Release

- Release-Basis ist der geprüfte Phase-C-Checkpoint `54eecf3108c2864c8b93fb2bbfb862ea82b6959e` auf `guardian-0.7.8-rs485`. Guardian/Add-on werden auf `0.7.9` angehoben; die Diagnostic Engine bleibt `0.4.12`.
- Der Source enthält append-only RS485-Evidenz, kompakte MQTT-/HA-Projektion, ADR-basierte History sowie Guardian-Zeitverlauf und Managementdarstellung. RS485 bleibt diagnoseisoliert und besitzt keinen aktiven Sendepfad.
- Die oben dokumentierten realen Phase-B-Fakten bleiben gültig. Phase-C-Persistenz, MQTT-, History- und UI-Verhalten wurden noch nicht real auf Home Assistant abgenommen.
- Weiterhin offen sind physischer Waveshare-Disconnect/Reconnect, ein möglicher Pegelimpuls beim Port-Open, reale Langzeitdatenmenge, ADR↔physische Identität und weitergehende eindeutige 0x44-Dekodierung.
- Git-/Source-Release, installierte Add-on-Version und persistente Daten unter `/share/guardian_battery` bleiben strikt getrennte Zustände. Ein Deployment darf bestehende Runtime-Daten weder migrieren noch überschreiben.

### Reale 0.7.9-Abweichung und Instrumentierung in 0.7.10

- **Real auf Home Assistant verifiziert:** Reader `listening`, gültige 0x42- und 0x92-Frames sowie korrekte Managementdekodierung; die bestehende Console lief parallel weiter.
- **Real auf Home Assistant beobachtet:** Das erwartete Verzeichnis `rs485_history` war nach dem 0.7.9-Start unerwartet nicht vorhanden.
- Der Source-Audit bestätigte die erwartete Verdrahtung: Writer und Pipeline werden bei aktiviertem Sniffer erzeugt, `writer.start()` läuft vor `reader.start()`, und die Verzeichniserzeugung erfolgt synchron. Die Ursache der realen Source-/Runtime-Abweichung ist nicht abschließend bestimmt.
- 0.7.10 ergänzt ausschließlich Start-, First-Record- und Fehlerlogging, `last_error` sowie einen Integrationstest des Runtime-Lifecycles. Die Instrumentierung dient der eindeutigen realen Abnahme; sie bestätigt noch keine produktive Persistenzfunktion.

### Reale 0x92-Pollkadenz und Freshness in 0.7.11

- **Real auf Home Assistant verifiziert:** Hycube fragt 0x92 blockweise für die erreichbaren ADRs ab. Beobachtete Abstände zwischen solchen Blöcken waren variabel und lagen unter anderem bei ungefähr 286 und 333 Sekunden; daraus wird keine allgemeingültige Pollperiode abgeleitet.
- Die frühere 120-Sekunden-Grenze war deshalb als Availability-Kriterium ungeeignet. Ab 0.7.11 ist der bestehende Optionswert `rs485_sniffer_stale_seconds` ausschließlich die Schwelle für `management_freshness=current|stale`; der konservative Default beträgt 600 Sekunden.
- Management-Entities bleiben bei einem lebenden Reader im Zustand `listening` verfügbar und behalten den letzten gültigen Wert. `sample_age_seconds` und `management_freshness` machen dessen Aktualität transparent. Erst ein nicht verfügbarer Reader-/Buszustand setzt die Entity offline; ein noch nie beobachteter 0x92-Wert wird nicht erfunden.

## Guardian Battery 0.7.12 – RS485 Identity & Topology Presence

- Source-Basis ist der gemeinsam geprüfte Identity-/Topology-Checkpoint `a98b8a6eacd8cec83c005a21534345a18922166d` auf `codex/guardian-0.7.11-freshness`. Guardian Battery und Add-on werden auf `0.7.12` angehoben; die Diagnostic Engine bleibt `0.4.12`.
- `0x93` wird passiv nach der dokumentierten Pylontech-V3.3-Struktur als Command und exakt 16 ASCII-Bytes Seriennummer dekodiert. Rawbytes bleiben erhalten. Historische gültige Raw Evidence kann deterministisch re-dekodiert werden und unterscheidet `stored_decoded` von `historical_raw_redecode`.
- Die physische Identitätskette lautet ADR → direkt beobachtete Seriennummer → zum Zeitpunkt wirksame dokumentierte Position. Es existiert keine ADR-minus-1- oder andere ADR→Position-Formel; unbekannte Identitäten bleiben unaufgelöst.
- Aktuelle Presence trennt `present`, `stale`, `absent`, `unknown` und `not_expected`. Console- und frische `0x93`-Beobachtungen dürfen Presence bestätigen; ein alter Console-Cache oder dieselbe alte `0x93`-Beobachtung zählt nicht erneut als Bestätigung.
- Nur Positionen `1..module_count` gehören zur aktuellen Solltopologie und können Missing-Alarme erzeugen. Höhere historische Positionen und deren letzte dokumentierte Seriennummer bleiben sichtbar, sind aber `not_expected`.
- Bestehende ADR-basierte MQTT-Entity-IDs und Topics bleiben kompatibel. Modul/Seriennummer verändern nur Friendly Names und Attribute unter demselben Discovery-Key. RS485 bleibt ohne Sendepfad und ohne Diagnose- oder Kausalitätswirkung.

### Reale Identitätsevidenz vor dem 0.7.12-Release

- Real beobachtet wurden: ADR `02` → `H221005E22212581`, ADR `03` → `H221005E22212536`, ADR `04` → `H221005E22212571`, ADR `05` → `H221005E22212538` und ADR `06` → `Y225004C32250226`. Dies ist Beobachtungs- und Regressionsevidenz, keine hardcodierte Produktivzuordnung.
- Für ADR `06` / `Y225004C32250226` wurde DCL wiederholt mit `-25 A` ↔ `0 A` bei weiterhin `ENABLED` meldendem Discharge Enable beobachtet. Daraus wird keine Ursache abgeleitet.
- Fehlende historische Positionsereignisse vom 31.08./01.09 wurden mangels vollständiger damaliger Evidenz bewusst nicht rückwirkend erfunden.

### Nach Source-Release weiterhin offene reale Abnahme

- Noch nicht als produktiv bestanden gelten die aktuelle Presence-Darstellung mit nur M1–M4, Position 5 als `absent`, die historische Position 6 als `not_expected`, automatische zukünftige Position-History-Snapshots sowie Removal und Reintegration im realen Betrieb.
- Ebenfalls offen bleiben der physische Waveshare-Disconnect/Reconnect und ein möglicher hardware- oder treiberbedingter Pegelimpuls beim Öffnen. Source-Release, installierte Add-on-Version und produktive Laufzeit sind getrennte Zustände.

## Aktueller verifizierter Release- und RC-Stand

- Vor dieser Release-Runde veröffentlichter Stand: Guardian Battery / Add-on
  `0.7.6`, `main` auf
  `c5c0ae63aaaf79929acd4b364a95b3708a415fc9`.
- Diagnostic Engine: unverändert `0.4.12`.
- Verifizierter 0.7.7-Release-Kandidat: Branch `guardian-0.7.7-rc1`,
  funktionaler RC `349a172bc724fb165b308ab85b09be4a29f7c2d9`.
- Git-/Source-Stand, installierte App, Home-Assistant-Deploymentdateien und
  persistente Daten unter `/share/guardian_battery` sind getrennte Zustände.

### Bestätigte Home-Assistant-Resource- und Deploymentfakten für 0.7.7

- YAML-definierte Lovelace-Ressourcen werden in dieser Installation mit
  `lovelace.resource_mode: yaml` und dem einmaligen `resources`-Eintrag
  `/local/guardian-collapsible-card.js?v=2` geladen.
- Ein `lovelace.resources`-Eintrag ohne wirksamen YAML-Resource-Modus führte
  real dazu, dass die Custom Card nicht automatisch registriert wurde.
- `/config/www/guardian-collapsible-card.js` wird über
  `/local/guardian-collapsible-card.js` ausgeliefert.
- Die reale Home-Assistant-Abnahme der korrigierten Collapsible Card ist PASS:
  automatische Registrierung, initial geschlossene unabhängige Bereiche,
  Auf-/Zuklappen und Child-Inhalt funktionieren.
- `guardian_battery/app/history_ui.py` ist Add-on-Code. Eine Änderung im
  Git-Arbeitsbaum allein verändert den laufenden Add-on-Container nicht.
- Der lokale `/addons`-Umweg bleibt ausgeschlossen. Installation und Abnahme
  erfolgen ausschließlich über den regulären Repository-/App-Workflow.
- Die reale HA-Abnahme der lokalen Mitternachtsmarker bleibt bis zur regulären
  Installation von Guardian Battery 0.7.7 offen.

## Zweck

Dieses Dokument hält bestätigte Eigenschaften, Ausnahmen, Fehlversuche und Arbeitsregeln der konkreten Guardian-EMS/Home-Assistant-Umgebung fest.

## VERBINDLICHE ARBEITSREGEL

**Vor jedem weiteren technischen Schritt in diesem Projekt ist dieses Runbook zu prüfen.**

- Ein hier als inkompatibel oder fehlgeschlagen dokumentierter Weg darf nicht erneut vorgeschlagen werden.
- Bei unbekannter CLI-Unterstützung zuerst `--help` bzw. die tatsächlich verfügbare Syntax prüfen.
- Keine GNU/Linux-Komfortoptionen voraussetzen: Die Terminal-&-SSH-Umgebung verwendet BusyBox-Werkzeuge.
- Immer nur einen kontrollierbaren Schritt ausführen; Ergebnis prüfen, erst dann weiter.
- Keine manuellen Code-Schnipsel zum Einfügen. Änderungen werden als vollständige Dateien oder vollständige Pakete geliefert.
- Produktive Laufzeitdaten unter `/share/guardian_battery` werden nicht durch Deployment- oder Quellcodearbeiten überschrieben.
- Source, Git-Arbeitsbaum, Deployment-Verzeichnis und persistente Laufzeitdaten sind strikt auseinanderzuhalten.
- Vor produktivem Deployment: separater Teststand -> Vergleich -> Tests -> Git/Commit -> kontrolliertes Deployment.

## 1. Bestätigte Umgebung

- Home Assistant mit Supervisor und Terminal & SSH.
- Home-Assistant-CLI `ha` ist verfügbar.
- Supervisor verwaltet die Apps/Add-ons.
- Shell-/Userland-Werkzeuge sind BusyBox-basiert; GNU-spezifische Optionen sind nicht automatisch verfügbar.
- `nano`/`vi` sind grundsätzlich Teil der Terminal-&-SSH-Umgebung, sollen für Guardian-Codeänderungen aber nicht als Standard-Workflow verwendet werden.

## 2. Guardian Battery – feste Identitäten

- App-Name: `Guardian Battery`
- App-Slug: `guardian_battery`
- Installierte App-ID: `3195b09a_guardian_battery`
- Historischer Feature-Release-Vorbereitungsstand dieses Abschnitts:
  Guardian/Add-on `0.6.2`, Diagnostic Engine damals unverändert `0.4.12`.
- Entwicklungscheckpoint vor der Release-Vorbereitung: `54ca42a`
  (`Add Home Assistant maintenance event integration`).
- Auf dem HA-System real verifizierte vorherige Produktions- und
  Rollback-Basis: `f37b6df` (`Document Guardian 0.4.12 Codex development
  baseline`).
- 0.4.9-Foundation-Commit: `e0377a6`
- 0.4.8-Rollback-/Ausgangspunkt: `86adccb`
- GitHub-Repository: `Andreas-Barsch/guardian-ems`
- Entwicklungsbranch für 0.4.9: `guardian-0.4.9`
- `main` wurde per Fast-Forward von `86adccb` auf `e0377a6` gebracht.

## 3. Relevante Pfade und ihre Rollen

### Git-Arbeitsbaum
`/homeassistant/guardian_ems_git`

Guardian-Quellbaum darin:
`/homeassistant/guardian_ems_git/guardian_battery`

### Installierter/lokaler Add-on-Quellpfad
`/homeassistant/addons/guardian_battery`

Dieser Pfad ist **nicht** automatisch identisch mit dem Git-Arbeitsbaum.

### Persistente Runtime-Daten
`/share/guardian_battery`

Bestätigte Dateien/Verzeichnisse:
- `cell_diagnostics.json`
- `cell_history/`
- `events.jsonl`
- `guardian_state.json`
- `incident_state.json`
- `trend_history.json`
- seit 0.4.9: `config_history.jsonl`

Ab Guardian 0.5.0 zusätzlich, erstmals beim ersten Maintenance-Schreibvorgang:
- `maintenance_events.jsonl` – append-only Maintenance-Revisionen
- `maintenance_events.jsonl.lock` – separate Sperrdatei für atomare
  Read-Check-Append-Operationen

Beide Dateien dürfen vor dem Deployment nicht künstlich angelegt und bei
Deployment oder Rollback weder gelöscht noch verändert werden.

Ab Guardian 0.6.0 zusätzlich, erstmals bei dokumentierter Stackbelegung:
- `position_history.jsonl` – append-only Vollsnapshots der Positionen 1–6
- `position_history.jsonl.lock` – Sperrdatei für atomare
  Read-Check-Append-Operationen und Optimistic Concurrency

Ein Neustart rekonstruiert die dokumentierte Stackbelegung aus der JSONL-Datei.
Ein Code-Rollback darf diese Dateien nicht löschen, umschreiben oder durch eine
aktuelle BMS-Beobachtung ersetzen. Aktuelle Beobachtung und dokumentierte
historische Wahrheit bleiben getrennt.

Diese Daten dürfen bei Source-/Deployment-Arbeiten nicht überschrieben oder gelöscht werden.

### Dashboard
`/homeassistant/dashboards/guardian_cell_diagnostics.yaml`

### Separater Testbereich für das Config-Menü-Paket
`/homeassistant/guardian_049_config_test/guardian_battery`

## 4. Shell-/BusyBox-Ausnahmen

### Bestätigt fehlgeschlagen: `diff --exclude`
Der folgende GNU-artige Ansatz funktioniert in dieser Umgebung **nicht**:

`diff -qr --exclude='.pytest_cache' --exclude='__pycache__' ...`

BusyBox meldet:
`diff: unrecognized option: exclude=.pytest_cache`

**Regel:** Bei `diff` keine GNU-Option `--exclude` verwenden.

### Bereits beobachtet: `grep`-Kompatibilität
GNU-spezifische `grep`-Optionen dürfen ebenfalls nicht ungeprüft vorausgesetzt werden.

**Regel:** Vor nichttrivialen `grep`-/`diff`-/`find`-/`sed`-Konstruktionen BusyBox-Kompatibilität prüfen oder bewusst POSIX-/BusyBox-kompatible Syntax verwenden.

## 5. Home-Assistant-App-/Repository-Verhalten

Bestätigter Ablauf beim 0.4.9-Release:

1. GitHub `main` wurde erfolgreich auf 0.4.9 aktualisiert.
2. `ha apps info 3195b09a_guardian_battery` zeigte zunächst weiter:
   - `version: 0.4.8`
   - `version_latest: 0.4.8`
   - `update_available: false`
3. `ha apps reload` führte in dieser Situation **nicht** zur Erkennung von 0.4.9.
4. `ha supervisor reload` führte ebenfalls **nicht** zur Erkennung von 0.4.9.
5. Der manuelle Refresh im Home-Assistant-App-Store („Nach Updates suchen“/Neu laden) führte zur Erkennung von 0.4.9.

**Regel:** Diese beiden Reload-Befehle nicht erneut als bereits bewiesene Lösung für genau dieses Repository-Refresh-Problem verkaufen.

## 6. Git-/Release-Workflow – bestätigt funktionierend

- Feature-/Release-Branch separat erstellen.
- Änderungen committen.
- Branch zu GitHub pushen.
- `main` nur kontrolliert übernehmen.
- Beim 0.4.9-Release funktionierte:
  `git switch main`
  gefolgt von
  `git merge --ff-only guardian-0.4.9`
- Erst nach Prüfung wurde `main` gepusht.
- Kein unnötiger Merge-Commit.
- Rollback-Punkt vor Übernahme eindeutig festhalten.

## 7. 0.4.9 Config-Provenienz – bestätigtes Runtime-Verhalten

Nach Installation von 0.4.9 wurde erfolgreich protokolliert:
`Diagnostic configuration recorded: dd90ef8819da97da`

`config_history.jsonl` enthielt den ersten Datensatz mit:
- `schema_version: 1`
- `guardian_version: 0.4.9`
- `diagnostic_engine_version: 0.4.9`
- `config_id: dd90ef8819da97da`

Ein unveränderter Neustart erzeugte **keinen** zweiten Eintrag:
`wc -l /share/guardian_battery/config_history.jsonl` -> `1`

**Regel:** Config-History ist änderungsbasiert, nicht startbasiert.

## 8. Modulanzahl / Soll-Topologie

- Das betrachtete Batteriesystem kann bis zu 6 Module enthalten.
- In der aktuellen realen Konstellation werden 5 Module erkannt.
- `module_count` ist ein Konfigurationsparameter, kein fest einzubrennender Systemwert.
- Aktueller 0.4.9-Default: `module_count: 6`.
- Für das geplante Menü gilt fachlich: Wertebereich `1–6`.
- Auto-Erkennung darf die konfigurierte Soll-Modulanzahl nicht überschreiben.
- Nicht konfigurierte Module dürfen nicht als `module_missing`/`unavailable` die Diagnose verfälschen.

## 9. Konfigurationsmenü – verbindliche Anforderungen

Es wird kein Minimalmenü nur für `module_count` gebaut, sondern ein vollständiges strukturiertes Menü für alle heute bekannten sinnvoll konfigurierbaren Parameter.

Struktur:
1. Anlage
2. Zelldiagnostik
3. Phasenerkennung
4. Bewertungsgrenzen
5. History & Datenerfassung
6. Erweitert/System

Diagnosephasen immer in dieser Reihenfolge:
**High-SOC -> Entladung -> Low-SOC -> Ladung**

Für jeden konfigurierbaren Parameter:
- verständliche deutsche Bezeichnung
- aktueller Wert
- Standardwert
- Einheit
- gültiger Wertebereich
- Erklärung
- Konsequenzen einer Änderung

Weitere Regeln:
- Die aktuellen produktiven 0.4.9-Werte sind die Defaults.
- Einführung des Menüs allein darf das Diagnoseverhalten nicht ändern.
- Keine sofortige Speicherung beim Verstellen.
- Explizites Validieren/Übernehmen.
- Fachliche Cross-Validierung, z. B. Observe < Warning < Critical sowie Medium-Confidence < High-Confidence.
- Fehlerhafte Eingabe darf die letzte gültige Konfiguration nicht zerstören.
- „Auf Standard zurücksetzen“ verändert zunächst nur Formularwerte; Persistenz erst nach Übernehmen.
- Unverändertes Speichern erzeugt keinen künstlichen Config-History-Eintrag.
- Config-ID, Guardian-Version und Diagnostic-Engine-Version anzeigen, aber nicht editierbar machen.
- Historische Rohdaten werden durch Parameteränderungen nicht umgeschrieben.
- Maintenance-Logbuch bleibt ein separater Bereich.

## 10. Historische 0.4.9-Defaults

Bestätigte Defaults aus `config.yaml`:

- `serial_port: auto`
- `baudrate: 115200`
- `poll_interval_seconds: 10`
- `module_count: 6`
- `command: pwr`
- `command_timeout_seconds: 5`
- `mqtt_topic_prefix: guardian`
- `publish_discovery: true`
- `warning_cell_delta_mv: 30`
- `critical_cell_delta_mv: 80`
- `warning_soc_deviation_pct: 10`
- `critical_soc_deviation_pct: 30`
- `missing_module_is_critical: true`
- `raw_log: false`
- `detailed_log: true`
- `trend_window_minutes: 60`
- `trend_min_change_mv: 10`
- `incident_hold_minutes: 30`
- `cell_diagnostics_enabled: true`
- `cell_diagnostics_interval_seconds: 60`
- `cell_diag_low_soc_percent: 30`
- `cell_diag_high_soc_percent: 80`
- `cell_diag_charge_current_a: 0.8`
- `cell_diag_discharge_current_a: 0.8`
- `cell_diag_min_phase_samples: 30`
- `cell_diag_confidence_medium_samples: 120`
- `cell_diag_confidence_high_samples: 600`
- `cell_diag_observe_deviation_mv: 10`
- `cell_diag_warning_deviation_mv: 20`
- `cell_diag_critical_deviation_mv: 40`
- `cell_diag_history_max_samples: 8640`
- `bms_stat_interval_seconds: 3600`

## 11. Arbeitsweise mit Dateien

- Nutzer soll keine Codeblöcke manuell in bestehende Dateien einfügen müssen.
- Änderungen werden als vollständige Dateien oder vollständige Archive geliefert.
- Hochgeladene Pakete vor Verwendung per Prüfsumme kontrollieren.
- Paket zunächst separat entpacken und prüfen.
- Keine Cache-Artefakte (`.pytest_cache`, `__pycache__`, `*.pyc`) in Git übernehmen.
- Erst nach Prüfung in den Git-Arbeitsbaum übernehmen.

## 12. Dateiübertragung

- Ein `scp`-Befehl, der innerhalb von `[core-ssh]` ausgeführt wird, interpretiert lokale Quelldateien als Dateien auf Home Assistant.
- Dateien vom Mac müssen vom Mac-Terminal aus übertragen werden.
- Nicht erneut so tun, als könne eine Mac-lokale Datei aus der HA-Shell direkt als lokale `scp`-Quelle verwendet werden.

## 13. Prüfpunkte vor jedem nächsten Schritt

Vor einem neuen Befehl beantworten:

1. Welcher Pfad wird verändert?
2. Ist es Git-Source, installierter Add-on-Source, Testkopie oder `/share`?
3. Ist der Befehl BusyBox-kompatibel?
4. Wurde diese Methode bereits als fehlgeschlagen dokumentiert?
5. Ist der Schritt reversibel?
6. Verändert er produktive Runtime-Daten?
7. Können wir das Ergebnis direkt danach eindeutig prüfen?
8. Wird nur **ein** kontrollierter Schritt verlangt?

## 14. Fortlaufende Pflege

Neue bestätigte Eigenheiten oder Fehlversuche werden mit:
- Datum
- Kontext
- ausgeführtem Befehl/Verfahren
- beobachtetem Ergebnis
- Konsequenz für zukünftige Schritte

in dieses Dokument aufgenommen.

Vermutungen werden nicht als bestätigte Umgebungsfakten dokumentiert.

## 15. Release 0.5.0 – realer Maintenance-Preflight vom 2026-08-19

### Patch-Release 0.5.1

Guardian/Add-on 0.5.1 paketiert ausschließlich das unter `6144cfa`
(`Refine maintenance identity and history UI`) funktional und visuell
abgenommene Korrekturpaket. Produktive Ausgangsbasis ist 0.5.0; der
Versionsbump stellt den regulären Home-Assistant-Updatepfad bereit, der bei
unveränderter Add-on-Version 0.5.0 nicht angeboten wird. Diagnostic Engine
bleibt `0.4.12`. Die isolierte Abnahme umfasste 149 Tests sowie Browserprüfungen
bei 1280x800, 800x900 und 390x844.

Offener, nicht blockierender UX-Punkt: Die 15 fachlich korrekten Zellkurven
der Modulebene sind ohne individuelle Hervorhebung und Legende bei eng
beieinanderliegenden Werten schwer zu unterscheiden. Dies wird nicht in 0.5.1
gelöst und rechtfertigt weder Messwertaggregation noch Glättung.

### Produktions- und Rollback-Basis

- Produktiver Git-Checkout: `/homeassistant/guardian_ems_git`
- Remote: `https://github.com/Andreas-Barsch/guardian-ems.git`
- Vorherige produktive Version: Guardian, Add-on und Diagnostic Engine
  `0.4.12`
- Verifizierter sauberer HEAD: `f37b6df`
- Installierter Add-on-Pfad: `/homeassistant/addons/guardian_battery`
- Entwicklungscheckpoint für Maintenance und HA-Events: `54ca42a`

Der rekursive Vergleich von installiertem Add-on und produktivem Git-Checkout
ergab keine Codeabweichung. In der installierten Kopie fehlte lediglich der
neuere dokumentarische 0.4.12-Abschnitt aus `CHANGELOG.md`. Diese Abweichung
ist kein Release-Blocker.

### Persistenz, Rechte und Migration

`/share/guardian_battery` wurde als `root:root`, Modus `755`, bestätigt.
Reguläre Runtime-Dateien sind überwiegend `root:root`, Modus `644`; das
Verzeichnis `cell_history` hat Modus `755`. Guardian 0.4.12 schreibt dort
bereits erfolgreich. Der Prozess war aus dem Terminal-&-SSH-Container nicht
per `ps` sichtbar; daraus wird keine Aussage über seinen Benutzer abgeleitet.
Der reale Schreib- und Rechtestest für die neuen Maintenance-Dateien erfolgt
erst in der Abnahme nach Installation von 0.5.0.

Die read-only Legacy-Inventur in Runtime-, Git-, Add-on- und separatem
Testbereich ergab für `maintenance`, `wartung`, `logbook`, `logbuch` und
`service` keine Treffer. `events.jsonl`, `cell_history/`,
`config_history.jsonl`, HA Recorder und Sensorhistorien sind keine
Maintenance-Quellen. **Keine Maintenance-Migration erforderlich.**

### Backup und Restore

Vor jedem 0.5.0-Deployment sind zwei Sicherungen verbindlich:

1. Ein HA-Backup mit der auf dem Zielsystem vorhandenen `ha backups new`-CLI.
   Die verwendeten Optionen, das fertige Archiv und die tatsächliche Aufnahme
   der erforderlichen Guardian-Daten müssen vor dem Update verifiziert werden;
   `/share` darf nicht stillschweigend als enthalten vorausgesetzt werden.
2. Ein explizites Guardian-Datenbackup außerhalb des Quellverzeichnisses:

   `tar -czf /backup/guardian_battery-pre-0.5.0-YYYYMMDD-HHMMSS.tar.gz -C /share guardian_battery`

   Anschließend sind Prüfsumme und Lesbarkeit zu prüfen:

   `sha256sum /backup/guardian_battery-pre-0.5.0-YYYYMMDD-HHMMSS.tar.gz`

   `tar -tzf /backup/guardian_battery-pre-0.5.0-YYYYMMDD-HHMMSS.tar.gz`

`/backup`, `/bin/tar`, `/usr/bin/sha256sum` und gzip-Unterstützung durch
`tar -z` wurden auf dem Zielsystem bestätigt. Beim Restore ist das gesamte
Guardian-Datenverzeichnis konsistent wiederherzustellen. Ein Code-Rollback
auf `f37b6df` darf eine inzwischen vorhandene Maintenance-JSONL oder deren
Lock-Datei nicht löschen oder verändern.

### MQTT Maintenance Event Entity

- Topic-Präfix: `guardian`
- Availability: `guardian/battery/availability` (`online`/`offline`, retained)
- Event Topic: `guardian/battery/event/maintenance`
- Discovery: Home-Assistant-MQTT-Event-Entity am Guardian-Gerät
- Live-Regel: ausschließlich neue manuelle Revision-1-Einträge mit
  `0 <= created_at - occurred_at <= 300 Sekunden`
- Maintenance Event: `retain=false`
- Kein Replay nach Guardian- oder Home-Assistant-Neustart
- Kein Publish bei Backfill älter als 300 Sekunden, Bearbeitung,
  Archivierung oder Wiederherstellung

Die Event Entity, Gerätezuordnung und das reale Live-Verhalten sind erst nach
Installation von 0.5.0 abzunehmen.

### Tatsächlicher Deploymentweg

1. Release-Branch erstellen, Änderungen prüfen und committen.
2. Release-Branch nach ausdrücklicher Freigabe zu GitHub pushen.
3. `main` kontrolliert per Fast-forward übernehmen und erst nach Prüfung
   pushen; `f37b6df` bleibt dokumentierter 0.4.12-Rollback-Punkt.
4. Im Home-Assistant-App-Store manuell **Nach Updates suchen/Neu laden**
   ausführen.
5. Prüfen, dass Store und App-Info tatsächlich `0.5.0` erkennen.
6. Update über die vorhandene App-/Add-on-Installation ausführen.
7. App neu starten und den Release-Abnahmeplan vollständig durchführen.

`ha apps reload` und `ha supervisor reload` haben die neue Repository-Version
historisch nicht zuverlässig erkannt und gelten für diese Installation nicht
als bewiesener Ersatz für den manuellen Store-Refresh.

## 16. Nachtrag 2026-08-17 – Keine Annahmen zu Testwerkzeugen und Pfaden

### Fehlversuch: Python/Pytest ungeprüft vorausgesetzt
Es wurde `python3 -m pytest -q` vorgeschlagen, ohne zuvor zu verifizieren, dass dieser Testweg in der konkreten Terminal-&-SSH-Umgebung verfügbar und der vorgesehene Projekt-Testweg ist.

**Regel:** Python-, Pytest-, Pip-, Venv- oder andere Entwicklungswerkzeuge niemals allein aufgrund einer üblichen Linux-/Python-Umgebung voraussetzen. Vor Verwendung muss ihre tatsächliche Verfügbarkeit bzw. der bereits bestätigte Projekt-Testweg geprüft werden.

### Fehlversuch: Nicht vorhandenen Dateipfad vorausgesetzt
Es wurde `guardian_battery/pyproject.toml` vorausgesetzt. Die Datei existiert im aktuellen Git-Arbeitsbaum nicht.

Tatsächlich verifiziert sind im Projekt unter anderem:
- `guardian_battery/requirements.txt`
- `guardian_battery/run.sh`
- `guardian_battery/tests/test_cell_diagnostics.py`
- `guardian_battery/tests/test_config_history.py`

**Regel:** Einen konkreten Projektpfad niemals aus einer früheren Paketstruktur, einem anderen Verzeichnis oder einer Annahme ableiten. Vor Verwendung muss seine Existenz im aktuellen Arbeitsbaum verifiziert sein.

### Meta-Regel
Wenn das Runbook gerade zur Vermeidung wiederholter Umgebungsfehler eingeführt wurde, ist es vor jedem vorgeschlagenen technischen Befehl verbindlich anzuwenden. Ein neuer Befehl darf keine dort dokumentierte Annahme erneut einführen.
