# Guardian Battery 0.3.0

Version 0.3.0 ergänzt eine regelbasierte Health Engine. Sie ist bewusst
nachvollziehbar und verwendet noch keine externe KI.

## Neue Sensoren

Gesamtsystem:

- Guardian Health Score
- Guardian Empfehlung
- Guardian auffälligstes Modul

Je Modul:

- Health Score
- Gesundheit
- Bewertung
- Empfehlung
- SOC-Abweichung zum Median des Stacks

## Ereignisspeicher

Neue und beendete Alarmzustände werden unter

`/share/guardian_battery/events.jsonl`

gespeichert. Alarmzähler und letzter Status liegen in

`/share/guardian_battery/guardian_state.json`.

## Aktualisierung über GitHub

Im GitHub-Repository den Ordner `guardian_battery` vollständig ersetzen,
committen und anschließend im Home-Assistant-App-Store nach Updates suchen.
