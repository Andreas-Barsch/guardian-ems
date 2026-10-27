# P0 application integration — local work in progress

This is an implementation inventory, **not an acceptance or deployment report**.
No production option selects the candidate. UTC writer selection remains blocked.
Default filenames, source formats and scientific algorithms remain unchanged.

| Connection | Current implementation / verification | Remaining integration |
|---|---|---|
| Application entry | `main.create_prepared_application`; injected `main(application=...)`; explicit migration/binding gate | Complete Collector lifecycle; fresh-interpreter restart with original confirmed pair and one new diagnostic decode is tested |
| Cell acquisition → Raw | Existing `acquire_cell_round` and `CellHistoryWriter` with `ApplicationWriter`; existing BAT calls and supplied PWR samples | Full Collector scheduling/transport lifetime |
| Raw → catalogs | Registry receipt consumer; declared Cell, RS485, Hycube and Daily input formats | Durable multi-source driver and migration inventory |
| Catalog → persistent diagnosis pair | `ApplicationTail` consumes exact receipt frontier through `DiagnosticRepository` | Rotation, late corrections, historical/live handover in actual application |
| Catalog → Canonical/Display → read indexes | `RepositoryPreparation` plus durable `ApplicationTail` ACK; targeted crash tests | Full metadata/position-change and concurrent publication tests |
| HTTP History/Research | Existing `config_ui.Handler` accepts server-owned composition; same authentication | Full route inventory; unbound candidate routes explicitly return 503 instead of using legacy defaults |
| Manual UI | Existing manual-only UI unchanged | Repeat directly relevant composition/UI regression evidence at final freeze |
| Config/Position writers | Optional confirmed append backend; Config restart tip | Actual configuration/position services; epoch and metadata background drivers are available in the application lifecycle |
| RS485 | Existing passive writer accepts confirmed append backend | Actual pipeline lifecycle, catalog and Daily fan-out |
| Hycube Raw | Existing evidence writer accepts confirmed append backend | Collector factory, policy predecessor, projection and catalog fan-out |
| Daily/Risk/BMS | Optional `DailyIndexedInputs`; one Cell scan for explicit 14 Guardian days; original methods retained | Actual Daily worker is application-owned and tested; complete metadata/RS485 live fan-out remains open |
| Maintenance/Technical metadata | Existing revision adapters remain available | Real writers/services and deletion/revision compatibility; Research maintenance/alarms use bounded metadata and preserve tested responses |
| Migration/readiness | Existing `MigrationManifest`; exact application-binding gate | Bounded frozen-file adoption tested for bytes/stat preservation and restart; combined full vertical migration and complete scope proof remain open |
| Retention/lifetime | Native application task/metadata ownership and unacknowledged preparation frontier; explicit old-schema rejection | Full multi-channel lifetime matrix remains open |
| Release/runtime | None | F/R, ARM64, HA baseline, release and production gates remain open |

The initial writer run had 83 passes and one invalid synthetic RS485 fixture
(missing address). Corrected run: 84 passed. Pipeline/HTTP run: 25 passed.
The first actual-main import test was blocked by the test guard at the legacy
module-level `/share` mkdir. Moving that side effect to the default legacy
`main` branch allows isolated imports; the subsequent 3 application tests passed.
These are development checks, not a source-frozen 4c1 acceptance claim.

## Intermediate source-frozen validation

`/tmp/guardian4c1-integration-check.json` records the exact command, source
hashes and protected environment. `/tmp/guardian4c1-integration-check.log`:
**461 passed in 102.15s**, no changed tested source; AST and diff checks passed.
This run covers 29 directly affected test files. Earlier development results
are not added to this count. No production test, release or 4c1 completion is
claimed. The original checkout remains clean at `0b8a80c`.

Candidate HTTP scope is explicit: bound History and Research readers plus the
existing preparation-status endpoint. Unbound configuration, position,
maintenance-write and diagnostics routes return `application_binding_required`
(503), after the existing authentication check. Default legacy server behavior
and authentication are unchanged. This temporary gate is an outstanding
integration item, not permission to omit those connections from the candidate.

New metadata revisions retain original latest maintenance records for the
Research contract. Older local revisions lacking those records fail explicitly
with `history_preparation_required`; they are not rewritten or guessed.
The initial migration must prepare the required scope before selecting it.

## Application lifetime and readiness follow-up (local, incomplete)

Pending application tasks own their preparation job and read-index job through
native references. The application metadata dispatcher owns both its candidate
and parent revision until publication ACK. Retirement also respects the durable
application subscriber frontier, including completed jobs not yet dispatched.
Older application table layouts require explicit migration, never an implicit
startup upgrade. Readiness binds the concrete store identities and application
schema as well as the existing provenance/options contract. A same-content
replacement database at the same path does not inherit the old readiness proof.
The entire existing BAT round owns the scheduler's Raw priority section.

The synthetic vertical restart test now includes unchanged legacy CellHistoryWriter
bytes, explicit bounded adoption, prepared diagnostic/publication stores, the real
HTTP handler, a fresh interpreter and exactly one new diagnostic decode. This is
one Cell path; it is not the complete multi-channel application acceptance.
Legacy adoption explicitly refuses an unrelated pending Raw append and leaves
its intent and source bytes unchanged.

### Confirmed append/as-of handover correction

The initial negative check exposed a real connection defect:
`CellSourceResolver._validate_ref` required the catalog's exact file signature
while a confirmed Raw append could already be ahead of that catalog.
Simply returning 503 and asking for another manual request was not accepted as
stable reading. The corrected application composition explicitly binds its Raw
registries to the Cell, RS485 and Hycube readers. It checks the existing catalog receipt and the
subsequent immutable journal chain up to a frozen confirmed head, without
advancing a consumer or reading/indexing appended Raw bytes. It then verifies
the latest confirmed file signature and the hashes of every selected original
record. Revision changes, replacement, truncation, changed selected bytes,
missing proof and unconfirmed physical changes remain unavailable.

For an unchanged selected source, its verified source tip is a bounded lookup;
unrelated journal growth does not consume an append-proof budget. A synthetic
regression appends 300 receipts to another source and prohibits a journal scan.
For changed selected sources, proof work is bounded to four existing registry pages (each at most 64 receipts
and 65,536 bytes), plus constant receipt/head metadata. Exhaustion is an explicit
unavailable proof-budget result, never an unbounded chain walk or a partial
success. This structural limit is **not** an HA performance/release budget.
Responses disclose the frozen indexed receipt/end, current confirmed end,
pending confirmed bytes, proof metadata bytes and unasserted latest Raw coverage.
No old reading is labelled current merely because the append is confirmed.

The read-index publisher now owns a native `window_index_ready` root per product
and day. Only a complete index advances that root. A newer unfinished derived
head cannot displace the previous readable publication. Display separately
reports newer Canonical availability for background refresh; a newer head alone
does not invalidate its older pinned dependency. Actual epoch/correction changes
remain blocking, including after the correction has finished: the existing
canonical revision number distinguishes a replacement from append/closure
within one revision. No scientific phase or diagnostic method changed.

Actual local HTTP tests cover 40 consecutive confirmed collection cycles with
delayed consumers, Raw -> catalog -> Canonical -> Display -> read-index handover,
Raw scheduling priority, and a full interpreter restart after explicit legacy
adoption. The counterfactual run disables only the injected reader contract in
the test process and reproduces **40/40 rejected requests**; source/Git state is
not reverted. The positive run includes 34 passed targeted tests. There is no
remaining XFAIL marker hiding the original defect.

### Local verification, 21 October 2026

The fixed-source regression run completed with **615 passed, five warnings**
in 240.54 seconds. Application/test source hashes matched before and after;
AST syntax and `git diff --check` passed. The five warnings concern the existing
Python 3.13 fork-based process-abort tests, not a demonstrated runtime failure.
Log: `/tmp/guardian4c1-asof-completed-check.log`, SHA-256
`b6f12ae0b4d11081a8f8aaed7b676a9c1bd3a782dfd9cded1f248aa9cc4a73ef`.
Its JSON manifest records the exact test selection and every source hash.
The earlier 611-pass/one-failure run remains a failed historical development
run; the correction was checked without weakening its stale-dependency assertion.
These results validate the connections described above, not the missing full
collector/service composition listed below and not HA performance.

## Finite remaining connections for the existing candidate

1. **Full Collector lifecycle:** `main.main` still selects the unchanged legacy
   production loop by default. The explicit prepared application's `run` owns
   background processing and HTTP; `collect_once` uses the real BAT acquisition,
   but the full existing PWR/stat/info/MQTT/topology poll lifecycle is not yet
   composed with the prepared stores. Complete that connection without changing
   acquisition cadence or scientific methods.
2. **Other live writer factories:** connect `create_rs485_reader`/passive pipeline,
   `collector_from_options`, Hycube policy/projection paths and Config/Position
   services to the already introduced writer/catalog/epoch/metadata contracts.
   The optional writer adapters alone do not prove those factory connections.
3. **Remaining service/read routes:** bind actual Maintenance/Technical writers,
   configuration/position/maintenance services, Research policy,
   `daily-diagnostics`, `diagnostic-evidence`, `events/low-voltage` and supplementary
   coverage. Candidate routes currently gated with explicit 503 remain open
   integration items; no legacy default fallback is permitted.
4. **Combined vertical acceptance:** run the real composed lifecycle with those
   sources, completed/open days, delayed consumers, cancellation, late/revised
   input and position changes; restart the full process and prove completed-day
   reuse plus only new delta work. The current Cell vertical result is one part
   of this matrix, not a substitute for the other connections.
5. **Existing local release/compatibility checks:** freeze the complete candidate,
   verify its diff, logs, migration/rollback scope and local compatibility checks
   for independent review. F/R, ARM64, actual HA baseline/acceptance and productive
   activation remain separate, unapproved gates. No version/release work or
   production access was performed in this follow-up.


## Collector/service continuation (local work, 21 October 2026)

The five-item inventory above describes checkpoint `3a97023`; the continuation
below advances it without claiming a completed application candidate.

- `create_prepared_collector` binds the existing PWR/stat/info/BAT parsing,
  event/state/trend handling, topology confirmation, persistent paired analysis
  and derived MQTT worker to explicit local dependencies. Main's default
  production selection is unchanged. Confirmed position changes block stale
  identity assignment; unrelated technical/config appends do not erase identity.
- Actual PositionHistoryService and MaintenanceService use immutable metadata
  readers for current, historical, revision and request-ID lookups. Maintenance
  time-window selection uses a separate ordered tree, before event decoding;
  old unrelated maintenance and technical records are not scanned. An oversized
  selected result fails explicitly instead of returning a truncated success.
- An exact newly confirmed maintenance tip can be read before metadata publication
  for the existing create-event/record-position transaction. Receipt, selected
  block hash and file signature are checked before/after; no consumer advances
  in a request. Other pending service queries remain explicit unavailable.
- HTTP uses the actual service APIs and existing validation inside the shared
  cancellation/query budget. Raw JSONL formats, scientific methods and versions
  remain unchanged. Added metadata roots require explicit preparation/migration;
  old layouts are not silently considered ready.
- Startup failure closes already owned HTTP/worker/transport resources. Shutdown
  attempts all resources even if one fails, and reports failures. Synthetic tests
  inject failure during discovery, MQTT startup and tail-thread startup.

Focused development checks: 26 collector/lifecycle/service/application tests
passed (54.31 s); 72 maintenance/API/metadata tests passed (5.24 s). Selections
can overlap and must not be added. Failed earlier fixture runs are retained in
`/tmp/guardian4c1-*` logs; these are local evidence, not HA measurements.

Still incomplete: RS485/Hycube factories and policy/projection integration,
complete collector timing, Config service/epoch fanout, prepared maintenance
hard deletion and retired-request ledger, remaining Research routes, large
complete Position-service lists, combined multi-source process restart and the
full release/compatibility acceptance. Maintenance hard deletion is explicitly
blocked for a confirmed writer until source replacement is supported; this is
an integration gap, not a change accepted for production. Existing legacy
behavior remains unchanged. No productive access, version change or release.


Frozen continuation regression: **703 passed, five warnings in 269.11 s**;
source hashes identical before/after, AST syntax and diff checks passed.
`/tmp/guardian4c1-collector-services-check.json` records selection and hashes;
log SHA-256 `4c61945b963aba3f5857ba3d9810ff54c6ab22439a5c258f547f08ca2c5165e2`.
Warnings are the existing Python 3.13 fork/process-abort warnings. This local
checkpoint does not close the remaining integration or runtime acceptance gates.


Further factory connection: the existing `create_rs485_reader` can receive an
explicit resolver/opener/clock without hardware discovery; unchanged defaults
retain the legacy selection and distinct-port protection. The actual passive
pipeline can resolve identities from the prepared PositionHistoryService.
A synthetic wire test traverses the real reader thread, frame/correlation
pipeline, confirmed writer, registry and catalog and checks original frame
bytes plus identity. Old Raw position lookup, hardware discovery and wire writes
are forbidden by that test. **87 tests passed in 0.82 s** in the selected
RS485/factory regression. This does not yet prove the combined application
start/stop, RS485 MQTT or full restart acceptance.


## Current follow-up — 21 October 2026

The complete local factory, explicit offline migration, real CLI and remaining
acceptance gates are documented in [P0_PREPARED_CANDIDATE.md](P0_PREPARED_CANDIDATE.md).
The earlier incomplete-checkpoint lists and test results above remain historical.
