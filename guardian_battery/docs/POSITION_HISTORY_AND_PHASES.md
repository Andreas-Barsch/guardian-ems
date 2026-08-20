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

Aktuell vom BMS beobachtete Barcodes sind flüchtige Laufzeitinformationen.
Sie werden nicht automatisch zur historischen Wahrheit. Abweichungen zwischen
Beobachtung und Dokumentation werden nur angezeigt. Ebenso werden bestehende
Zellmessungen ohne quellseitige Seriennummer nicht nachträglich einer
Seriennummer zugeschrieben.

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

Die Phase Engine verwendet exakt dieselbe deterministische Regel wie die
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
