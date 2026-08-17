# Guardian EMS – Environment Runbook

Stand: 2026-08-17

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
- Aktueller Release-/Entwicklungsstand in dieser Arbeitsphase: `0.4.9`
- 0.4.9-Foundation-Commit: `e0377a6`
- 0.4.8-Rollback-/Ausgangspunkt: `86adccb`
- GitHub-Repository: `Andreas-Barsch/guardian-ems`
- Entwicklungsbranch für 0.4.9: `guardian-0.4.9`
- `main` wurde per Fast-Forward von `86adccb` auf `e0377a6` gebracht.

## 3. Relevante Pfade und ihre Rollen

### Git-Arbeitsbaum
`/homeassistant/guardian_ems_048_git`

Guardian-Quellbaum darin:
`/homeassistant/guardian_ems_048_git/guardian_battery`

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

## 10. Aktuelle 0.4.9-Defaults

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

## 15. Nachtrag 2026-08-17 – Keine Annahmen zu Testwerkzeugen und Pfaden

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
