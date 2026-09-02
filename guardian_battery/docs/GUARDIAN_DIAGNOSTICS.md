# Guardian Diagnostics D1

Guardian Diagnostics ist die read-only Präsentationsschicht für bereits abgeschlossene, deterministisch erzeugte Daily Diagnostics. V1 berücksichtigt ausschließlich die Komponente **BMS Management Evidence**. Sie startet weder Daily Runs noch Backfills und liest keine Raw Cell- oder RS485-History.

## Gesamtübersicht und Tagesdiagnosen

Die Gesamtübersicht zeigt den neuesten gültigen Diagnosetag, Datenqualität, wichtige deterministische Beobachtungen und Zusammenfassungen der letzten 7 beziehungsweise 30 Kalendertage. Nur vorhandene Daily Results werden berücksichtigt; fehlende Tage sind keine Nulltage und bei zu kurzer Historie bleibt der Trend nicht bestimmbar.

Die Tagesliste ist vom neuesten zum ältesten vorhandenen Result sortiert. Das Tagesdetail projiziert vorhandene Quellenqualität, Component-Status, BMS-Management-Aggregate und sichere Provenienz. `physical_serial` ist die Primäridentität; eine historische Position wird nur aus der zeitgültigen Evidence dieses Tages angezeigt und nie aus der heutigen Position abgeleitet. Die Darstellung ist nicht auf sechs Module festgelegt.

## BMS Management Evidence

Je physischer Seriennummer können CCL-/DCL-Reduktionen und Zero Events, Limit trotz Enable, Cell/Lowest-Cell-/Spread-Kontext, Stromkontext, Coverage, Duty Cycle, Peer-relative Werte und rohe `0x44`-Korrelationen dargestellt werden, soweit sie im autoritativen Daily Result beziehungsweise Aggregate vorhanden sind. Fehlender Kontext wird als nicht verfügbar gekennzeichnet und nicht als Nullwert erfunden.

## Evidence und Kausalität

Die Oberfläche beschreibt reproduzierbare Beobachtungen, zeitliche Korrelationen und Datenqualität. `causality=not_determined` bedeutet, dass aus der Korrelation keine Ursache folgt. Insbesondere werden Lowest Cell und rohe `0x44`-Byteübergänge nicht als Ursache einer CCL-/DCL-Begrenzung bezeichnet. Guardian Diagnostics D1 ist keine KI-Auswertung und verändert weder Cell Status noch Confidence, Maintenance Risk, Phasenklassifikation oder Gesamtbewertung.

CCL und DCL sind die vom adressierten BMS gemeldeten zulässigen Lade- beziehungsweise Entladestromlimits. Enable und numerisches Limit sind getrennte Protokollinformationen; CCL/DCL 0 A bedeutet nicht automatisch, dass das zugehörige Enable deaktiviert ist. Peer-Vergleiche definieren keinen festen Normalwert.

## Qualität, Coverage und Dauer

- `complete`: Die aktive Komponente wurde nach ihrem bestehenden Vertrag vollständig ausgewertet.
- `partial`: Die Datenlage ist teilweise; ein Ereigniswert von null darf nicht als unauffälliger vollständiger Tag interpretiert werden.
- `failed`: Es existiert kein gültiger Latest-Result-Index. Der aktuelle Worker-State hält fehlgeschlagene Versuche nicht vollständig historisch pro Tag vor; eine historische Failed-Liste bleibt deshalb in D1 **OFFEN** und wird nicht erfunden.
- Management Coverage ist die gap-qualifizierte Beobachtungszeit.
- Duty Cycle ist gap-qualifizierte Restriktionsdauer geteilt durch gap-qualifizierte Management Coverage.
- Endpoint-Dauer ist der Abstand zwischen beobachtetem Restriktionsbeginn und beobachteter Recovery. Sie ist nicht der Zähler des Duty Cycle.

## Read-only API

Alle Routen akzeptieren ausschließlich `GET`:

- `/api/diagnostics/overview`
- `/api/diagnostics/days`
- `/api/diagnostics/daily/YYYY-MM-DD`
- `/api/diagnostics/bms-management/aggregate/YYYY-MM-DD`
- `/api/diagnostics/bms-management/events/YYYY-MM-DD`

Datumswerte werden strikt als kanonisches `YYYY-MM-DD` validiert. Resultrevisionen werden ausschließlich über denselben vollständigen Indexvertrag wie im Daily Worker aufgelöst. Overview liest höchstens die 30 neuesten Derived Results; Aggregate dienen der Fensterzusammenfassung, Event Stores nur dem expliziten Tages-/Eventzugriff. Rawframes werden nicht ausgegeben.

## Home-Assistant-Ingress und Deploymentgrenze

Guardian Diagnostics ist ein interner Bereich der bestehenden Guardian-Ingress-Anwendung. Der normale Guardian-Seitenmenüpunkt lässt Home Assistant die dynamische Ingress-Sitzung erzeugen; innerhalb dieser Sitzung führt der relative Navigationspfad `diagnostics` zur read-only Ansicht. API-Aufrufe verwenden den aktuellen, aus `X-Ingress-Path` abgeleiteten Prefix. Es gibt keinen gespeicherten Session-Token, keinen zweiten Ingress-Eintrag, keinen zweiten Webserver und keinen neuen Port.

Ein Lovelace-iframe der Form `/api/hassio_ingress/3195b09a_guardian_battery/diagnostics` ist kein gültiger Einstieg: `3195b09a_guardian_battery` ist der statische Add-on-Slug, während dieses URL-Segment einen dynamischen Supervisor-Session-Token erwartet. Deshalb wurden die separate Dashboard-Source und ihre Registrierung in D1.1 entfernt. Ein wirklich eigenständiger HA-Seitenmenüpunkt erfordert später eine separate ingress-aware Frontendlösung.

Falls die fehlerhafte D1-Source bereits produktiv installiert wurde, muss der Block `guardian-diagnostics` kontrolliert aus `/config/configuration.yaml` entfernt werden. `/config/dashboards/guardian_diagnostics.yaml` kann nach vorheriger Sicherung entfernt oder ungenutzt belassen werden. Diese produktive Bereinigung ist kein Bestandteil der Source-Änderung.
