# Guardian EMS – Environment Runbook

Stand: 2026-08-19

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
- Verifizierter aktueller Source-/Git-Stand: `0.4.11`
- Verifizierter Git-Branch: `main`
- Verifizierter Git-HEAD: `8db6339` (`Guardian Battery 0.4.11 enable ingress panel`)
- Verifiziert: lokales `main` und `origin/main` zeigen auf `8db6339`.
- Verifiziert in `guardian_battery/app/version.py`: `GUARDIAN_VERSION = "0.4.11"` und `DIAGNOSTIC_ENGINE_VERSION = "0.4.11"`.
- Verifiziert in `guardian_battery/config.yaml`: Version `0.4.11`.
- GitHub-Repository: `Andreas-Barsch/guardian-ems`
- Historische Bezugspunkte: 0.4.10 `fbf9dac`, 0.4.9 finaler Config-UI-Stand `55c21f7`, 0.4.9 Foundation `e0377a6`, 0.4.8 `86adccb`.
- Frühere Runbook-Angaben, die 0.4.9 als aktuellen Stand bezeichneten, sind durch diese verifizierte 0.4.11-Baseline ersetzt.

## 3. Relevante Pfade und ihre Rollen

### Git-Arbeitsbaum
`/homeassistant/guardian_ems_git`

Der frühere, versionsgebundene Verzeichnisname `/homeassistant/guardian_ems_048_git` wurde am 2026-08-19 nach einer Referenzprüfung in den versionsneutralen Namen `/homeassistant/guardian_ems_git` umbenannt.

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

Historisch bestätigter Ablauf beim 0.4.9-Release:

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

## 10. Historisch bestätigte 0.4.9-Defaults

Diese Werte wurden für 0.4.9 bestätigt. Sie sind historische Referenzwerte und dürfen nicht ungeprüft als aktuelle 0.4.11-Defaults bezeichnet werden:

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

## 15. Verifizierte Arbeitsweise ab 2026-08-19

### 15.1 Keine technischen Zustände aus Erinnerung oder Vermutung

- Versionsnummern, Branches, Commits, Release-Stände, Dateistände, Pfade und vorhandene Funktionen werden nur als aktuell bezeichnet, wenn sie unmittelbar verifiziert wurden.
- Frühere Chat-Aussagen und ältere Dokumentationsstände sind keine ausreichende Quelle für den aktuellen technischen Zustand.
- Ist ein Zustand nicht verifiziert, wird er ausdrücklich als `nicht verifiziert` bezeichnet.
- Bei Widersprüchen zwischen Runbook, Source, Git, Home-Assistant-Runtime und GitHub wird zuerst der Widerspruch geklärt; es wird nicht geraten.

### 15.2 Verifikation eines Entwicklungsstands

Soweit für die jeweilige Aussage relevant, werden gegeneinander geprüft:

1. Git-Arbeitsbaum und Branch,
2. Git-HEAD,
3. `guardian_battery/app/version.py`,
4. `guardian_battery/config.yaml`,
5. bei Aussagen über die installierte Version zusätzlich die Home-Assistant-/Supervisor-Runtime,
6. bei Aussagen über einen veröffentlichten Release zusätzlich der GitHub-Stand.

Versionsnummern werden erst nach dieser Prüfung als Ausgangsbasis für eine neue Version verwendet.

### 15.3 Terminal und Zusammenarbeit

- Terminalausgaben kurz und screenshot-tauglich halten.
- Keine unnötig langen `git show`, `diff`, `cat`, rekursiven `grep`- oder vergleichbaren Ausgaben.
- Wenn eine Datei direkt bereitgestellt werden kann, ist sie einer langen Terminalausgabe vorzuziehen.
- Pro Schritt nur die kleinste notwendige Prüfung verlangen.
- Keine wiederholte Git-Forensik durch den Nutzer, wenn die benötigten Informationen bereits verifiziert vorliegen.
- Keine manuellen Code-Schnipsel in bestehende Guardian-Dateien einfügen lassen.

### 15.4 Versions- und Release-Arbeitsweise

- Eine freigegebene/verifizierte Basis bleibt unverändert.
- Neue Funktionen werden als neuer versionierter Entwicklungsstand umgesetzt.
- Vor Beginn werden Ausgangsversion, Branch und Commit verifiziert.
- Änderung -> Test -> Regressionstest -> dokumentierter Commit/Release-Kandidat -> GitHub-Veröffentlichung -> abschließende Verifikation.
- Changelog und Runbook werden im selben kontrollierten Versionsprozess mitgeführt und dürfen nicht hinter dem Source-Stand zurückbleiben.

### 15.5 Home-Assistant-Verzeichnisstruktur

- Aktiver Git-Arbeitsbaum ab 2026-08-19: `/homeassistant/guardian_ems_git`.
- Versionsnummern gehören nicht in den Namen des aktiven Git-Arbeitsverzeichnisses.
- Historische Archive/Backups können Versionsnummern tragen, müssen aber als historische Artefakte behandelt werden.
- Alte Archive werden nicht allein aufgrund ihres Namens gelöscht; Löschung erfolgt nur nach geklärtem Zweck und bestätigter Entbehrlichkeit.
- Produktive Runtime-Daten unter `/share/guardian_battery` bleiben von Source-/Verzeichnisbereinigungen unberührt.

### 15.6 Aktuelle fachliche Arbeitsgrenze

- Verifizierte Entwicklungsbasis für die nächsten Arbeiten: Guardian Battery `0.4.11`, Git-HEAD `8db6339`.
- Das BMS-/Modul-Info-Thema wird über den bestehenden seriellen Guardian-Zugriff mit `info <module>` umgesetzt.
- Ziel: übersichtliche modulbezogene Hersteller-/Identitätsinformationen im Dashboard.
- Vorgesehen ist eine kompakte Home-Assistant-Info-Entity je Modul mit Detailinformationen als Attribute.
- Cycle Count / modulbezogener SOH bleibt für diesen Arbeitsschritt offen.
- Das Maintenance-Handbuch bleibt ein eigener Funktionsbereich.

### 15.7 Korrekturen

- Eine erkannte falsche oder nicht belegte technische Aussage wird ausdrücklich verworfen und nicht als Grundlage weiterverwendet.
- Dokumentation wird nur mit belegten Informationen ergänzt.
- Unbekannte historische Details werden nicht nachträglich erfunden.

### 15.8 Home-Assistant Terminal-&-SSH-Randbedingungen

Für technische Anweisungen in der aktuellen Home-Assistant-Umgebung gelten zusätzlich:

- Terminal-&-SSH-Kommandos müssen mit den dort verfügbaren BusyBox-Werkzeugen kompatibel sein.
- GNU-spezifische Optionen werden nicht vorausgesetzt. Insbesondere ist `find -printf` in dieser Umgebung nicht verfügbar.
- Eine Python-Installation im Terminal-&-SSH-Add-on wird nicht vorausgesetzt.
- Vor der Ausgabe eines Terminalbefehls wird geprüft, ob er unter den bekannten HA-/BusyBox-Bedingungen geeignet ist.
- Befehle und Ausgaben bleiben kurz und screenshot-tauglich; rekursive oder umfangreiche Ausgaben werden vermieden.
- Benötigt eine Guardian-Funktion Python, wird die dafür vorgesehene Add-on-/Container-Runtime verwendet. Eine Python-Laufzeit des Terminal-&-SSH-Add-ons ist keine Voraussetzung.
- Änderungen an produktiven bzw. aktuell verwendeten Dateien erfolgen nicht direkt „am offenen Herzen“: Zuerst wird eine neue Datei bzw. ein separater neuer Stand erstellt und geprüft. Erst danach wird der geprüfte Stand kontrolliert an die vorgesehenen Zielorte übernommen.

## 16. Entwicklungsstand 0.4.12 – BMS-/Modul-Info

### 16.1 Verifizierter Entwicklungsstand
- Ausgangsbasis: Guardian Battery `0.4.11`, Git-HEAD `8db6339`.
- Git-Arbeitsbaum: `/homeassistant/guardian_ems_git`.
- 0.4.12-Entwicklungsstand: `app/version.py` und `config.yaml` auf `0.4.12`.
- Neue Funktion in `app/main.py`: serielles `info <module>` für erkannte Module.
- Cycle Count / modulbezogener SOH bleibt offen.

### 16.2 Verifizierte Tests
- Isolierter `parse_info()`-Unit-Test: `5 passed`.
- Gesamte Regression-Suite einschließlich Info-Test: `26 passed`.
- Für die vorhandenen Tests auf dem Mac war `PYTHONPATH=app python3 -m pytest -q` erforderlich.
- Vollständiger Import von `main.py` außerhalb HA zog unnötig Runtime-Abhängigkeiten (`pyserial`, `/share/guardian_battery`) in den Unit-Test; der Parser-Test wurde deshalb isoliert.
- Softwaretests sollen künftig soweit möglich vor Übergabe in der Modell-Sandbox ausgeführt werden; reale Pylontech-, MQTT-, Supervisor- und HA-Runtime-Tests bleiben auf HA.

### 16.3 Verifizierte `ha apps`-CLI
`ha apps --help` bestätigt: `changelog`, `info`, `install`, `logs`, `rebuild`, `restart`, `start`, `stats`, `stop`, `uninstall`, `update`.

`ha apps install --help` bestätigt `ha apps install [slug] [flags]`; ein beliebiger lokaler Verzeichnispfad ist keine Installationsquelle.

### 16.4 Fehlversuch: lokaler `/addons`-Teststand
Am 2026-08-19 wurde ein separater Teststand unter `/homeassistant/addons/guardian_battery_0412_test` mit eigenem Slug versucht.

Verifiziertes Ergebnis:
- erschien nach App-Store-Refresh nicht als eigene App;
- keine passende Supervisor-Logmeldung;
- `ha apps info guardian_battery` meldete, dass die App nicht existiert;
- Kopieren von 0.4.12 nach `/homeassistant/addons/guardian_battery` plus `ha apps rebuild 3195b09a_guardian_battery` aktualisierte die installierte Repository-App nicht;
- `ha apps info 3195b09a_guardian_battery` zeigte weiterhin `0.4.11`.

**Regel:** Diesen lokalen `/addons`-Umweg nicht erneut als Test-/Deploymentweg verwenden.

Nach bestandenem Test und Regressionstest gilt:
`dokumentierter Commit/Release-Kandidat -> GitHub -> kontrollierte Übernahme/Veröffentlichung -> HA-Repository aktualisieren -> 0.4.12 installieren -> Hardware-/Runtime-Verifikation`.

### 16.5 Nächster Schritt
- Dokumentation mit 0.4.12 synchronisieren.
- 0.4.12-Commit/Release-Kandidaten erstellen.
- Branch zu GitHub pushen.
- `main` kontrolliert mit Rollback-Punkt übernehmen.
- App-Store über den historisch bestätigten manuellen Refresh aktualisieren.
- Erst nach tatsächlich bestätigter Installation von 0.4.12 den Pylontech-/MQTT-/HA-Runtime-Test durchführen.
