# Guardian Battery 0.4.5 – Historical module median

Guardian reconstructs the module median retrospectively from its own persisted raw cell samples.

For each raw sample:
`Vmedian(t) = median(V1(t), …, V15(t))`

For the UI, samples are grouped into 5-minute buckets. The bucket value is the median of all
module medians in that bucket. The resulting 24-hour series is published in the `history_24h`
attribute of the module Zellmedian sensor.

This deliberately does **not** modify Home Assistant Recorder history. The Guardian custom
Lovelace card reads normal Home Assistant history for the selected cell and combines it with
Guardian's reconstructed module-median series.
