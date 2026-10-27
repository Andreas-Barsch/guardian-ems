# Phase 2b4: unactivated cell and effective-epoch adapters

Local preparation only. No existing reader/writer/factory imports these modules;
no UTC writer activation, installed-data migration, HA or version change. This
is not an F/R-compatible release or a claim that the existing product's timezone
and global-signature paths have already been replaced.

## Existing formats and executable connection contract

`cell_history.py:CellHistoryWriter.append` writes schema1, numeric absolute
`timestamp`, module, voltages_mv, current_a, soc_percent, temperatures_c,
balancing, module_serial and position_history_id. Its current filename is based
on `datetime.fromtimestamp`, i.e. process local time. `history_append_receipt.py`
is a disposable schema1 `.append.json` generation/stat sidecar, without the new
registry's durability protocol. Its presence **does not register/confirm** a
source in the new catalog. No sidecar is upgraded, overwritten or inferred as a
new journal receipt.

Prepared call sequence:

1. Explicit source inventory/registration and confirmed RawRegistry receipts
   supply SourceCatalog. Existing log/raw bytes remain unchanged. Initial legacy
   discovery and confirmation are explicit jobs, not work on opening a window.
2. `RegistryCatalogConsumer` ingests proofs; bounded `SourceCatalog.index_step`
   derives entry time/module/identity from record contents, never filename day.
3. `CellWindowAdapter(catalog).open(start,end,modules=...)` freezes a resolver
   snapshot. `.page(snapshot,after=...,fields=...,limit=...,max_bytes=...)` reads
   bounded selected ranges across legacy-local and utc namespaces together.
4. The adapter returns timestamp, observed module_serial/position_history_id,
   existing identity/position epoch fields if present, source/revision/offset/
   hash/context and only selected measurement fields. Missing fields/identity
   remain missing/unknown; there is no inference of serial from module number.
   These fields map directly to the existing `CellSample` fields. A downstream
   product still owns its existing validation, metric extraction, identity
   resolution, diagnostics, segmentation and downsampling.
5. `.close` releases the derived snapshot. No scan or raw fallback is performed
   if coverage/indexing is missing. `coverage`, pending_sources and invalid_records
   remain explicit; pagination `complete` means the frozen plan was exhausted,
   **not** that all historical evidence exists or is indexed.

One page: <=256 records and <=64KiB selected Raw bytes. One FD per physical
selected path, touching ranges merged; no reads through gaps for unselected
records. All selected channels/modules share that page. A record larger than the
caller's budget yields budget_blocked without cursor advance. Existing resolver
limits (<=32-day window, <=256 selected source prefixes, SQL work budget) remain;
this is not a new user-facing query policy. Larger product work needs multiple
explicit bounded windows, not silent truncation.

Internal ranges are half-open. `inclusive_api_end=True` uses the next representable
float above the exact end **once for the whole request**. It includes the boundary
sample once across internal pages/sources without cadence-based epsilon. Two
separate inclusive requests still both include their shared endpoint, as their
contracts require. `local_day_window(date,IANA_zone)` converts actual consecutive
local midnights, preserving 23/25-hour days. Naive timestamps are rejected;
unknown historical process timezone is not recovered from a dated filename.

## Config/position provenance and temporal dependencies

`config_history.py:ConfigHistory` records schema_version, timestamp, config_id,
parameters, guardian_version, diagnostic_engine_version. It has **no effective_at
field**: its aware timestamp is the existing effective time. Ordering is timestamp,
config_id. `position_history.py:PositionSnapshot` supplies the existing validator
for effective_at/created_at/position_history_id, maintenance_event_id and six
positions. Ordering at equal effective_at is created_at then ID. Duplicate
position IDs are rejected as in PositionHistoryLog. Documentary identity and
observed cell identity remain separate, with no substitution for unknowns.

`EpochLogConsumer(registry,EpochStore(explicit_path))` is explicitly initialized
with a source-ID -> config/position mapping. It reads the actual immutable JSONL
format through the new registry's bounded block proof, without touching original
logs. Existing ConfigHistory/PositionHistoryLog writers do not yet produce these
registry receipts. Their future connection and explicit legacy onboarding remain
integration gates; merely importing the module activates nothing.

Each step verifies one <=64KiB receipt and decodes <=256 complete lines. A receipt
with more lines resumes at a durable intra-receipt offset; no total-history row
limit. Source proof, accepted epochs, raw-line provenance, change records and
journal/subcursor progress commit together. Invalid/ambiguous data raises an
error with unchanged cursor; it is not silently skipped. A different payload
with the same kind/effective time/tie identity is reported as ambiguous rather
than inventing a winner. Complete-prefix coverage must be established by the
caller from consumer progress and the explicit source inventory; `at()` only
resolves ingested epochs and returns None before the known first epoch.

Epoch selection uses the index `(kind,stamp,priority)` and a predecessor lookup.
Window fingerprints clip each actually used epoch to that window, so extending
its effective end outside an old window does not change that window's fingerprint.
The original payload and raw provenance remain available. At most256 epoch
transitions/ties per window; exhaustion raises EpochBudgetExceeded, never an
unbounded log scan or incomplete success. SQLite work is bounded as well.

`register_dependency(product,kind,start,end,context=...)` stores an immutable
publication identity, actual consumed half-open interval, context and fingerprint;
no full-log size/mtime dependency. A temporal epoch change records exactly its
direct effective span up to the next existing epoch. A durable paginated job
planner seeks a `(kind,UTC_day,dependency)` index, intersects that span with the
registered consumed intervals and commits jobs plus planning cursor together.
Dependencies registered after the change are excluded via its saved dependency
high-water mark. Repeated page membership produces one job per change/dependency.
An affected publication can keep its historical dependency; a corrected publication
must receive a new identity. No published result is rewritten here.

These are **direct input-change jobs**. Executing them, publishing corrected
products and propagating stateful Canonical/Daily successors until state/input
convergence remain later work. A config's next effective boundary is not asserted
to end all downstream state effects. No diagnosis or phase rule is redefined.

Canonical as-was dependencies use kind=config and historical parameters.
Current Condition uses the explicit current_options path (`set_current_options`,
kind=current_config) and the existing diagnostic_parameters/config_id helpers.
Historical config append cannot replace current options or invalidate that axis.
An explicit relevant current-options change affects registered current contexts,
regardless of their old input timestamps. Position dependencies can be registered
separately with their actual time/identity context. No retention duration is invented.

## Readermatrix F/R, verified against 891d335 parent

F = future complete candidate; R = required separate compatible return artifact.
Every row below remains **unwired in F and unimplemented/unverified in R**. Existing
signatures/paths were inspected, not silently replaced by these local primitives.

| Existing consumer / concrete connection point | Required connection and remaining compatibility work |
|---|---|
| `history_series.py:CellHistorySeries._paths/query_bundles`; `history_api.py`; factory `config_ui.py` | One CellWindowAdapter snapshot/page plan for all selected modules/channels; preserve metric/identity/downsampling response contract and inclusive end. Not a drop-in API replacement yet. |
| `research_timeseries.py:ResearchTimeseriesService._paths/_records/query`; `research_api.py:149,291–294,378` | Use same adapter for raw and display fallback; public cursor/query signing and coverage must carry source revision/snapshot. Keep guardian.cell_history label; existing MCP unchanged. |
| `diagnostic_startup.py`; `diagnostic_startup_io.py:paths`; `current_condition_backfill.py:CurrentConditionBackfill`; `diagnostic_backfill.py:DiagnosticAggregateBackfill` | Explicit catalog pages instead of directory walk. Preserve retention/slots/serial resolution; connect current_config separately from historical config. No startup activation in this phase. |
| `canonical_phase.py:CanonicalPhaseProjection.update_current/build/_resolve`; `CanonicalPhaseReader.query` | Time-window sources across local/UTC files; EpochStore.at(config,t) follows real historical config format. Replace global config signature with consumed dependency vector; complete state/convergence integration and reference parity still required. |
| `display_history_projection.py:_sources/build`; `display_history_reader.py:DisplayHistoryReader._valid/query_bundles` | Same source revisions for projections and tails; retain cell/Hycube channel separation, coverage and metric semantics. Current global validations untouched. |
| `daily_diagnostics.py:guardian_day/build_source_catalog/probe_daily_inputs/_risk_history_records`; `daily_diagnostic_worker.py`; `main.py:1590` | Use explicit local_day_window UTC bounds and catalog records, including DST. Preserve 14-day risk lookback and daily product semantics. Not wired. |
| `history_preparation.py`; `history_block_index.py`; `hycube_projection.py:_rebuild_cell_indexes`; `tools/history_preflight.py` | Explicit source/revision manifests for bounded maintenance and index work; no old-directory-only scan or same-stem collision. Existing helpers unchanged; current raw writer indexing unchanged. |
| `soc_crash_replay.py:audit_saved_history`; `tools/replay_soc_crashes.py` | Explicit source manifest/window iterator for offline replay; keep explicit-file mode. No replay/import/event production authorized. `tools/rs485_event_analyzer.py` is separate RS485, not a Cell migration target. |
| `main.py:88,1573,1611`; `config_ui.py:332,384`; `cell_acquisition.py` | Later composition injects one proven resolver contract into all readers before any writer change. No extra hardware calls. UTC writer remains off. |
| `config_history.py`; `position_history.py`; `history_append_receipt.py` | Existing log writers and disposable receipts remain unchanged. Registry receipt integration, ownership/rotation/revision handling and installed-source onboarding must be explicit. No automatic conversion. |

Repository search results and symbol inventory accompany the external evidence.
Dashboard/API response and installed consumers are not verified by local format
fixtures. External Raw consumers remain unknown. Plain old builds are not claimed
to be safe return artifacts after a future UTC transition. F->R->F artifact tests,
full reader semantic parity and runtime acquisition budgets are still required.

## Test evidence and scope

Real CellHistoryWriter/CellSample, ConfigHistory and PositionSnapshot/Log fixtures
in temporary directories. Tests include the audit's 2026-01-01T23:30Z sample under
2026-01-02.jsonl, the future-config/old-day case, DST23/25h, inclusive exact endpoint,
multimodule/channel reads, unknown legacy identity/time, future/retroactive epochs,
Current Condition separation, bounded partial-receipt restart, paginated persistent
jobs and process-exit transaction tests. All184 pre-existing architecture regressions
remain unchanged. Logs report actual results; no HA or power-loss claim.

All overall gates remain open. Stop at control point2b4; no following phase is
implicitly authorized by successful isolated tests.
