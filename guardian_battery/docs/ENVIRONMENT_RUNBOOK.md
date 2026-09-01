# Guardian EMS – Environment Runbook

Stand: 2026-08-31

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
