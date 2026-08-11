# Guardian Battery 0.4.4 – Module Median Overlay

For every detected module Guardian publishes a current **Zellmedian [mV]** calculated as:

`median(V1, …, V15)`

The module median is a reference signal, not a health score. It allows the cell-voltage
history chart to show the selected cell and the simultaneously measured module median
on the same axis.

Interpretation:
- cell ≈ median: cell follows the module collective;
- cell < median: negative relative deviation;
- cell > median: positive relative deviation;
- diagnostic relevance still depends on operating phase, magnitude, persistence and confidence.

The separate ΔV chart remains the quantitative diagnostic view.
