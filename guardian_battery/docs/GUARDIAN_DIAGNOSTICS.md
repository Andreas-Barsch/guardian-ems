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

## Home-Assistant-Dashboard und Deploymentgrenze

Die einzige HA-Dashboard-Source ist `homeassistant/dashboards/guardian_diagnostics.yaml`. Für ein späteres Deployment muss sie separat als `/config/dashboards/guardian_diagnostics.yaml` installiert und der Source-Eintrag `guardian-diagnostics` aus `homeassistant/configuration.yaml` kontrolliert in die produktive Lovelace-Konfiguration übernommen werden. Ein Add-on-Update erledigt beides nicht automatisch.

Das Dashboard registriert den eigenständigen Seitenmenüpunkt **Guardian Diagnostics** und bindet die servergerenderte read-only Ansicht des bestehenden Guardian-Ingress ein. Es gibt kein zweites Ingress-Panel, keinen zweiten Webserver und keinen neuen Port. Die konkrete Sidebar-/Iframe-Auflösung muss nach dem späteren Source-/Konfigurationsdeployment real im Browser verifiziert werden.
