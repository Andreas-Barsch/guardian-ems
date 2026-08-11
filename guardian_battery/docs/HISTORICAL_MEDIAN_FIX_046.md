# Guardian Battery 0.4.6 – Historical Median Fix

0.4.5 introduced retrospective module-median reconstruction but attempted to access the
diagnostic store through the MQTT publisher (`self.cell_store`). The MQTT publisher does not own
that store.

0.4.6 computes each module's historical median series in `main()`, where the real
`CellDiagnosticStore` instance exists, and passes the finished series into `Mqtt.publish()`.

The reconstruction is isolated per module with `try/except`. Failure of this optional UI history
therefore produces a warning and an empty history for that module, but does not abort normal
battery polling, alarms, live diagnostics, or MQTT state publication.
