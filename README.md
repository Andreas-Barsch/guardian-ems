# Guardian Battery 0.4.0

Phase 3A ergänzt eine nachvollziehbare Trend- und Incident-Engine.

## Neue Sensoren

Je Modul:

- Zellspreizung Trend: `rising`, `stable`, `falling`
- Änderung der Zellspreizung im Trendfenster
- SOC-Änderung im Trendfenster

Gesamtsystem:

- Incident aktiv
- Incident-Zusammenfassung

## Konfiguration

- `trend_window_minutes`: Standard 60 Minuten
- `trend_min_change_mv`: Standard 10 mV
- `incident_hold_minutes`: Standard 30 Minuten

## Push-Automationen

Die Datei `automations_guardian.yaml` enthält Vorlagen. Darin muss
`notify.mobile_app_DEIN_IPHONE` durch den tatsächlichen Benachrichtigungsdienst
der Home-Assistant-Companion-App ersetzt werden.
