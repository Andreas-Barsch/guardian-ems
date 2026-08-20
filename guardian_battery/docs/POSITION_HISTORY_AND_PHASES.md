# Positionshistorie und Phasenprojektion

## Physische Modulidentität

Eine Stackposition (`module_number`) ist keine physische Identität. Guardian
dokumentiert die Zuordnung von Position 1 bis 6 zu einer nachgewiesenen
Seriennummer deshalb in `/share/guardian_battery/position_history.jsonl` als
append-only Vollsnapshots. Jeder Snapshot besitzt eine stabile `PHS-…`-ID,
`effective_at`, `created_at` und die ID des auslösenden Maintenance Events.
Die Initialdokumentation darf nur zum Erfassungszeitpunkt entstehen. Spätere,
auch rückdatierte Änderungen bleiben zusätzliche Datensätze; bestehende
Historie wird nie umgeschrieben.

Aktuell vom BMS beobachtete Barcodes werden stabilisiert: Erst drei identische,
aufeinanderfolgende erfolgreiche Lesungen bestätigen eine Zuordnung. Fehlende,
unlesbare oder wechselnde Antworten verändern nichts. Eine bestätigte
Abweichung erzeugt ein systemseitiges Maintenance Event und einen neuen
append-only Vollsnapshot; vorhandene Snapshots werden nie überschrieben. Nach
einem Neustart wird die dokumentierte Historie aus JSONL rekonstruiert und eine
Beobachtung erneut stabil bestätigt. Bestehende Zellmessungen ohne quellseitige
Seriennummer werden nicht nachträglich einer Seriennummer zugeschrieben.

Die Ingress-API unter `api/position-history` bietet die vollständige Historie,
den aktuellen dokumentierten Stand, Auflösung Position→Seriennummer und
Seriennummer→Position zu einem Zeitpunkt sowie bekannte Seriennummern je
Position. Neue Snapshots verlangen die ID eines aktiven Maintenance Events und
`expected_latest_snapshot_id` als optimistische Nebenläufigkeitskontrolle.

### Persistenz, Restart und Rollback

`position_history.jsonl` enthält schema-versionierte JSON-Objekte, einen
vollständigen Zustand der sechs Positionen pro Zeile und wird ausschließlich
erweitert. `position_history.jsonl.lock` schützt die atomare
Read-Check-Append-Operation gegen konkurrierende Schreibvorgänge; nach dem
Schreiben werden Puffer und Dateisystemzustand synchronisiert. Ein Neustart
rekonstruiert die Projektion vollständig aus der Datei. Der Client muss beim
Anlegen `expected_latest_snapshot_id` mitsenden; ein veralteter Stand wird als
Konflikt abgewiesen.

Der erste Snapshot ist eine Initialdokumentation zum tatsächlichen
Erfassungszeitpunkt und darf keine rückwirkend angenommene Identität erzeugen.
Jeder weitere Snapshot verweist auf das auslösende aktive Maintenance Event.
Ein Code-Rollback darf `position_history.jsonl`, ihre Sperrdatei oder andere
persistente Daten unter `/share/guardian_battery` weder löschen noch
umschreiben. Älterer Code darf unbekannte neue Persistenzdateien lediglich
unangetastet lassen.

## Phase Engine

Die diagnostische Phase Engine verwendet exakt dieselbe deterministische Regel wie die
Live-Zelldiagnostik: Strom klassifiziert Laden, Entladen oder Ruhe; SOC und die
mittlere Zellspannung ergänzen Low- beziehungsweise High-SOC. Es gibt keine
lernende oder KI-basierte Klassifikation.

Die drei Analysemodi sind strikt getrennt:

- `historical`: je Messzeitpunkt gilt der damals bereits wirksame Datensatz aus
  `config_history.jsonl`; vor dem ersten belegten Datensatz lautet die Phase
  `unknown`.
- `current`: alle Messpunkte werden mit der derzeitigen Konfiguration nur neu
  projiziert.
- `what_if`: ein Aufrufer muss alle vier Phasenparameter ausdrücklich
  mitsenden. Diese Auswertung ist flüchtig und schreibt weder Konfiguration noch
  Messhistorie.

`api/history/series` liefert Phaseintervalle getrennt von Messreihe und
Maintenance-Markern. Die UI zeichnet in dieser Reihenfolge: Phasenhintergrund,
Grid/Achsen, Messkurven, Maintenance-Werkzeugmarker, Interaktion. Rohmessungen,
Home-Assistant Recorder und JSONL-Historien werden dabei nicht verändert.
Ohne Messpunkte bleibt die X-Zeitachse erhalten, während keine Y-Skala und kein
Messpfad erfunden werden. Maintenance-Schraubenschlüssel werden auf einer
eigenen Markerzeile weiterhin anhand von `occurred_at` positioniert.

### Visual Phase Projection

Die UI-Projektion ist eine zweite, ausschließlich visuelle Ebene. Sie verändert
weder Messwerte noch diagnostische Intervalle. Die zentralen Defaults sind:

- neue Phase nach mindestens 180 Sekunden stabiler Klassifikation;
- Stromhysterese 0,2 A um die diagnostischen Lade-/Entladegrenzen;
- Zusammenführen einer Unterbrechung von höchstens 120 Sekunden, wenn davor
  und danach dieselbe visuelle Phase liegt.

Einzelne Ausschläge und kurzes Schwellwertflattern erzeugen dadurch keinen
eigenen Farbstreifen. Echte länger anhaltende Wechsel bleiben sichtbar. Die
API liefert kompakte visuelle und getrennte diagnostische Intervalle; die
Oberfläche kann die Hintergrundebene mit „Phasen anzeigen“ ein- oder
ausschalten.

## History-Performance

Guardian speichert weiterhin sämtliche Rohsamples append-only in täglichen
JSONL-Dateien. Die History-API liest die benötigten Tagesdateien einmal,
erstellt Messreihe und Phasensamples im selben Durchlauf und cached Ergebnisse
mit Dateigröße und Änderungszeit als Invalidierungssignatur. Für die Anzeige
werden pro Zeitfenster höchstens 6.000 Punkte übertragen. Zeitbasierte Buckets
bewahren Minimum und Maximum jeder Zellreihe; es findet keine Glättung oder
Mittelwertbildung der Rohdaten statt.

Die Home-Assistant-Standardkarte `history-graph` unterstützt keine fremden
Guardian-Hintergrundebenen und bleibt daher unverändert. SOC, Strom,
Zellspannung und Zelltemperatur verwenden die zentrale Guardian-History-Ansicht
mit derselben Phasenprojektion für Stack-, Modul- und Zellkontext.
