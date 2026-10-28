# Solarlernen in 0.5.1

AB1 (`hycube`) und AB2 (`secondary`) führen getrennte, persistente Epochen und
Faktoren. Für Notstrom wirkt nur AB1, genau einmal. Es gibt keinen zweiten
pauschalen Solarabschlag.

1. Die Anlaufphase beträgt sieben Tage ab bestätigter Messanbindung. Initial
   wird Faktor 0,8 verwendet; unzureichende Qualität verlängert diesen Zustand.
2. Rohe Prognosen werden vor Beginn der jeweiligen UTC-Stunde archiviert.
   Vorher darf eine neuere veröffentlichte Prognose sie ersetzen; nach Beginn
   wird sie eingefroren. Keine rückwirkende Bewertung mit späterem Wissen.
3. Leistung wird über reale Zeitstempel in kWh integriert. Vollständige
   Messintervalle sind erforderlich; keine Überbrückung fehlender Messung.
   Standard: maximal 60 s zwischen verwendeten Punkten.
4. Nachtstunden und Prognosen unter 0,02 kWh liefern keinen Quotienten.
   Tatsächlich null Erzeugung bei nennenswerter Prognose ist dagegen gültig.
5. Faktor = Summe tatsächlicher kWh / Summe roher prognostizierter kWh der
   gültigen Stunden im rollenden Sieben-Tage-Fenster. Kein ungewichteter
   Mittelwert einzelner Quotienten.
6. Standard-Freigabe: mindestens 24 gültige Tageslichtstunden an sieben Tagen,
   mindestens 90 % Tageslichtabdeckung, 95 % vorab archivierte Prognosestunden
   und 1 kWh Prognosesumme. Faktor 0 ist zulässig; Werte über 2 werden
   zurückgestellt, nicht abgeschnitten. Grenzen bleiben konfigurierbar.
7. Berechnung höchstens einmal pro abgeschlossener UTC-Stunde mit 60 s Nachlauf;
   Übernahme beim nächsten vollständigen Planungsdurchlauf. Bei rollendem Betrieb
   spätestens mit der stündlichen Planung. Keine doppelten Zeitumstellungsstunden.
8. Nach erfolgreichem Lernen bleibt bei späteren Lücken der letzte gültige
   Faktor erhalten. Status, gültige Stunden und Herkunft werden ausgewiesen.
9. Anlagen-, Messquellen-, Skalierungs- oder `calibration_revision`-Wechsel
   erzeugt eine neue Epoche. Bestehenden Lernspeicher nicht für Releases löschen.

Messvertrag: AB1 liest Guardian `solarPower` in W, skaliert mit `0.001` nach kW.
AB2 liest `ExternalPower` mit `-0.001`. Negative Erzeugung nach dieser Umkehr
ist ungültig: kein Betrag und keine Umdeutung von Bezug in Erzeugung.
`received_at` ist der Messzeitbezug. Tatsächliche Zuordnung und Plausibilitäts-
Leistungsgrenzen müssen vor Aktivierung der jeweiligen Quelle bestätigt werden.

Die HA-Anzeige trennt den vorläufig errechneten Quotienten vom wirksamen
Faktor; ohne gültige Stunde erscheint kein errechneter Zahlenwert. Rohprognosen,
Messungen und SQLite-Speicher bleiben private Laufzeitdaten.
