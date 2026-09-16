# External Research Evidence Contract

Guardian exposes a provider-neutral, read-only evidence contract for external research.
Capability discovery is available through `GET /api/research/status`; it performs no
history scan. Raw Cell History evidence is available through
`GET /api/research/evidence/raw`.

The raw endpoint requires `source=guardian.cell_history`, `physical_serial`, `from`,
`to`, and an explicit comma-separated `fields` list. The maximum time range is six
hours, a page contains at most 500 records, and the bounded scan may contain at most
10,000 matching records. Continuation uses a signed cursor bound to the complete
normalized query. Missing values remain `null`; they are never converted to zero.

Supported fields are `timestamp`, `soc`, `current`, `voltage`,
`temperature_channels`, and `cell_01` through `cell_15`. Every record also carries
historical physical identity and position metadata. Voltage is conservatively marked
as DERIVED because legacy records may require reconstruction from their cell values;
the other selectable fields are OBSERVED.

Physical access is fail-closed. Every selected daily Cell History file must have a
valid rebuildable block index. Guardian does not perform a full-file fallback for this
endpoint, and rejects excessive windows and page sizes before touching history data.
Raw JSONL remains authoritative and unchanged.

This implements the bounded external Cell History slice of GRA-002 and GRA-003. The
status response advertises only this proven raw source. Existing Research API routes
remain compatible; their individual access characteristics are not upgraded merely by
this contract. Research reads and live acquisition still have no shared I/O budget or
enforced priority mechanism. The capability response reports that limitation rather
than claiming GRA-001 enforcement. Extending bounded external access to further stores
and moving additional research compute off-device therefore remain follow-up work.
