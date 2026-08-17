# Guardian Battery 0.4.9 – Dashboard-Konfigurationsmenü

Das Konfigurationsmenü ist eine per Home-Assistant-Ingress geschützte Weboberfläche des Guardian-Apps.
Es arbeitet direkt auf den Supervisor-App-Optionen und erzeugt keine zweite Konfigurationsquelle.

## Struktur
1. Anlage
2. Zelldiagnostik
3. Phasenerkennung
4. Bewertungsgrenzen – Reihenfolge High-SOC, Entladung, Low-SOC, Ladung
5. History & Datenerfassung
6. Erweitert / System

Jeder editierbare Parameter zeigt Standardwert, Einheit/Wertebereich und die Konsequenz einer Änderung.
Die Standardwerte entsprechen dem vor Einführung des Menüs produktiv verwendeten 0.4.9-Parametersatz.

## Übernahme
- Eingaben werden zunächst lokal fachlich validiert.
- Danach wird die Supervisor-Schema-Validierung ausgeführt.
- Nur eine gültige, tatsächlich geänderte Konfiguration wird persistent gespeichert.
- Guardian wird anschließend neu gestartet.
- Beim Start zeichnet `ConfigHistory` nur einen neuen Datensatz auf, wenn sich der diagnostisch relevante Parametersatz und damit die Config-ID geändert hat.
- Historische Rohdaten werden nicht rückwirkend verändert.

## Sicherheitsprinzip
Die Weboberfläche akzeptiert nur Ingress-Verbindungen des Home-Assistant-Supervisors. Für die persistente Änderung der eigenen App-Optionen verwendet Guardian den Supervisor-API-Token und die dafür erforderliche Manager-Rolle.
