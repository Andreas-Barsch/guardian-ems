# Prepared application candidate — local continuation, 21 October 2026

Battery **0.9.0**, prepared and unpublished. The 0.8.9 checkpoints remain
historical intermediate source states; 0.9.0 distinguishes the explicit derived-store
migration and complete prepared application. No publication is implied. MCP **0.8.4**, Tunnel **0.8.1**,
Diagnostic Engine **0.4.12** remain unchanged. This document supersedes the
*remaining integration work* lists in the dated checkpoints of
`P0_APPLICATION_INTEGRATION.md`; those historical results are retained.
Neither the default legacy startup nor the UTC writer contract is activated by
this candidate. Local tests are not HA acceptance or installation permission.

## Actual application and ownership

`prepared_candidate.open_candidate` composes the real collector, confirmed
writers, metadata/epoch consumers, Canonical/Display/Current repositories,
manual History/Research readers, config/position/maintenance services, passive
RS485 reader and identity restore, Hycube acquisition/projection/policy, Daily
and MQTT. There is one console owner. Injected synthetic transports replace
hardware only in tests. Ordered bindings are canonical across JSON round trips.

Raw receipts, bounded indexes and published generation heads have separate
owners. Readers do not discover files or run a migration. Missing/incompatible
stores fail explicitly. Raw blocks derivation; verified immutable reads have a
separate bounded queue. Cancellation reaches SQL, selected record reads and
scientific loops. No new recognition rules, phase thresholds or scientific
formulas are introduced. Diagnostic history-to-Live handover preserves the
original chronological historical rings followed by arrival-ordered Live input.

Maintenance deletion now uses the actual existing API: durable replacement
intent, retained-request digest and bounded metadata replay. Other events retain
their bytes; retries cannot recreate a retired request. Replacement verification
runs outside the shared Raw write lock. Immutable old derived generations remain
subject to existing lease-aware retirement; this is not a secure erasure promise.
Hycube projection batches/day metadata/inventory have a recoverable publication
intent. Process-abort tests cover both coordinators, separately from power loss.

## Explicit offline preparation and entry points

`prepared_migration.py` accepts an explicit reviewed JSON inventory, not a scan
of production directories. The plan contract is `guardian-prepared-migration-1`:
`layout`, `files` (writer + relative path), `streams` (origin day, physical serial
and identity epoch), `frontier_day`, `as_of_timestamp`, `provenance`, `rollback`.
The layout contract is `guardian-prepared-candidate-1`; it explicitly supplies
paths, writers/registries, catalogs/formats, metadata mapping/origin, streams,
diagnostic name, scheduler, poll interval, Research paths, optional Daily mapping,
and runtime transport paths, cursor-key file, bind address and port.
`tests/test_prepared_migration.py:legacy_plan` and its all-source extension are
executable **synthetic** examples, not an installation inventory or real identity
origin. No operational paths, key material or invented historical mapping are
provided here.

Using the sealed candidate's Python runtime, with all source writers offline:

```
python -m prepared_migration --root <explicit-offline-root> --plan <reviewed-plan.json> --begin --steps 64
python -m prepared_migration --root <explicit-offline-root> --plan <same-reviewed-plan.json> --steps 64
python -m prepared_candidate --root <same-root> --layout <same-root>/prepared-layout.json --check
```

Repeat bounded preparation steps until `complete`. Both CLIs require sealed
version/source provenance before opening candidate stores. The plan hash freezes
inputs; existing derived work is refused, never overwritten. Source files retain
names/bytes. Steps reopen durable state, adopt confirmed prefixes, prepare all
catalogs/metadata/epochs, complete Canonical/Display/diagnostics and handover,
verify unchanged originals, and finally publish a bound layout/manifest.
One source cannot be owned by two writers. New/changed catalog state invalidates
the frozen manifest. A crash after a committed step resumes from its durable
cursor. A partially created incompatible store is preserved and fails explicitly;
there is no silent repair or automatic directory removal.

`--check` opens/reconciles explicitly prepared derived state and checks readiness;
it is **not** a strictly read-only filesystem command. It opens no live transport.
`--run` separately starts the actual services and shuts down owned resources.
It is not enabled in the default startup and must not be used on HA without the
separate installation/migration approval and reviewed real layout.
The migration does not import historical SOC events, rewrite Raw files, activate
UTC writers, change SSDs or install dependencies. Old derived schemas (including
catalogs without change tokens) require a separately prepared store scope.

## Actual container selection and same-image return

The image's `run.sh` executes `/app/startup.py`. The operator-owned marker
`/data/guardian-prepared-activation.json` is the original private prepared-start selection;
the explicit local-add-on control-file extension below also permits a shared marker.
Migration never creates it. Absent marker means the existing `main.py` path;
a present invalid, oversized, dangling, wrong-version/source or wrong-layout-hash
marker is a fatal start error before candidate/transport construction. There is
no fallback after a failed prepared start and no second console owner.

The marker has exactly these fields (the values must be reviewed for the actual
installation, not copied from a synthetic fixture): `contract` =
`guardian-prepared-activation-1`, `guardian_version` = `0.9.0`, `source_commit` =
the sealed image's full commit, `root` = an existing absolute binding root,
`layout` = the explicitly prepared layout inside it, `layout_sha256` = the exact
layout file's SHA-256. No credentials belong in this marker. A different image
commit requires a new reviewed source binding; an old marker cannot silently
activate a different build.

For the existing code's container paths, an installation plan may use `/` as
binding root with **explicit finite paths only**: `data/options.json` for the
actual Supervisor options, original writer paths under `share/guardian_battery`,
and distinct new derived-store paths/layout under that product directory. This
is not permission to discover/scan `/`. Do not copy options to an independent
file and then expect Supervisor changes to update that copy. The actual writer
paths, metadata identities, cursor secret and storage mounts still require the
separate real inventory; no fabricated installation plan is supplied here.

Concrete later sequence for this exact image: stop the old sole writer; verify
backup scope/return artifact; run the image's offline migration entrypoint against
the reviewed original mounts/inventory with live services stopped; verify unchanged
originals and sealed `--check`; only then install the reviewed marker and start
this image through its normal `run.sh`. An ordinary image update alone does not
activate the prepared path. Do not overlay files or relabel an older image.
The actual host mount names and how to run that isolated one-off container remain
an operational prerequisite; they have not been probed or guessed here.

Return within the same immutable image: stop Battery first; preserve its current
Raw/events/options and the marker/layout; explicitly withdraw the activation
marker; start the same image via `run.sh`, which now selects legacy. This returns
the selection, not a promise of instantaneous recovery: legacy derived-state
catch-up and the real data/path mapping must be checked. Never run both paths
against the same writers or restore stale Raw over newer records. Returning to a
previous image additionally needs an available matching artifact and the separate
compatibility/backup approval. Tests prove selection and the migrated sealed
entrypoint with synthetic files; productive switching/restoration remains open.

## Local acceptance map and remaining gates

All paths below are under `guardian_battery`; test names identify executable
local evidence, not a claim that every real-device gate has passed. Final run
counts, source hash and log hashes belong to the external phase report. Tests
from overlapping selections must not be added.

| Requirements | Implementation / local tests | Limit |
|---|---|---|
| A1 / R7,R8; T11–T14 | `work_scheduler`, `local_history_composition`, `history_ui`; `test_local_history_http`, `test_local_history_window_matrix`, `test_history_ui`, `test_history_p0`, `test_timeline_ui` | Actual HA browser/network acceptance separate |
| A2 / R2,R4; T02–T04 | Registry/catalog durable cursors, prepared lifecycle; `test_source_catalog`, `test_prepared_candidate`, `test_prepared_migration` | Warm zero-Raw refers to fully confirmed preparation; interrupted unfinished work resumes |
| A3 / R2,R6; T08–T10 | Paired diagnostic publication, delta state, original scientific methods; `test_diagnostic_repository`, `test_diagnostic_live_handover`, `test_daily_indexed_inputs`, `test_work_scheduler_load` | Device timing/memory/IO budgets unknown |
| A4 / R3,R4; T03,T05,T07 | `canonical_day_repository`, `display_day_repository`; corresponding repository tests and `test_source_catalog` | No UTC writer activation |
| A5 / R5,R1; T06,T09 | Metadata roots/epochs, as-of readers; `test_metadata_revision`, `test_history_epoch_adapter`, `test_prepared_position_service`, `test_prepared_config_service` | Requires reviewed real identity origin and options |
| A6 / R3; T05,T07,T16 | Legacy timestamp convention kept explicit; DST/catalog and Daily parity tests; offline migration | Productive migration and return path not exercised |
| A7 / R6; T10,T11,T15 | Shared scheduler, empty passive-reader wait, bounded source proofs; `test_work_scheduler_load`, `test_rs485_evidence`, `test_confirmed_index_append` | Mac synthetic throughput is no HA jitter approval |
| A8 / R9; T02,T04,T12,T15 | Indexed source inventory and fixed window plans; `test_source_catalog`, `test_local_history_window_matrix`, `test_prepared_migration` | OS cache coldness, full device/disk failure matrix not claimed |
| A9 / R4,R8; T03,T07,T08,T13,T16 | Immutable publications + durable intents; `test_projection_publication`, `test_prepared_maintenance_service`, `test_raw_registry_replacement`, lifecycle/lease tests | Process abort is not power-loss recovery proof |
| R1; T01,T09,T14 | Original methods retained; `test_build_provenance`, diagnostic/Daily parity and timeline tests | No P1/product mixing; final source-bound regression required |
| T16 release | Explicit sealed CLI, original bytes preserved, independent derived preparation, migration resume | ARM64 build/smoke, HA hashes, `/share` backup/restore proof and operator return procedure remain separate gates |

T01–T16 describe planned acceptance categories, not sixteen new tests. Local
fault/scale/parity cases cover their stated subsets; unresolved real hardware,
full-load and return gates must not be converted into PASS by a green unit suite.

## Capacity and reproducibility

`tests/profile_daily_capacity.py` is opt-in under the existing test guard.
It generates or explicitly reopens **synthetic** 14-day / six-module data at
60 s or 10 s intervals, reports files/bytes/decodes/SQL/RSS and guard time.
`--science` executes the original complete Daily calculation and concurrently
runs the actual prepared collector with synthetic one-module console replies.
Historical input in this profile is **CatalogOnly**, not proof of the complete
RawRegistry ingestion chain; the all-source application tests cover that chain
on a smaller fixture. Physical I/O and HA timing are deliberately unknown.

Earlier local 10 s input-only result: 725,760 records, 272,885,760 Raw bytes,
86.686 s reading, 1,647,263,744-byte peak RSS, 53.537 s guard. This is neither a
whole-Daily memory upper bound nor a release budget. The earlier 60 s result used
30-minute partitions and is historical; the current implementation uses bounded
15-minute pieces, unchanged per-page limits, fixed confirmed prefixes and
per-record hash checks. Current results must cite their actual source revision.

### Dense full-Daily failure and correction

The first 10 s full-science experiment did not finish successfully. At
13:12:13.954 MESZ on 21 October the macOS process sample showed all main-thread
samples in nested dictionary/list destruction, other Python threads waiting for
the GIL, and a 5.9 GB physical footprint. The last regular synthetic collector
log was 13:10:54.938: at least 79.016 seconds without the next normal cycle log.
The process was explicitly stopped; no complete result or invented lost-slot
count is claimed. Input-only success did not detect this failure.

CellRisk now expands private observations for one physical identity at a time
and releases them in batches of 256 with GIL yields, including cancellation.
The per-cell original calculations, within-identity sample ordering and final
global ranking remain unchanged. A frozen complete original-result hash and
existing scientific tests check parity; the large counterproof must be recorded
separately. The profile now records actual interstart gaps, late full periods,
terminal gap and periodic Python stack traces; it does not invent HA budgets.

## Release and return gates

Before any installation: a clean complete source commit, matching sealed build
metadata, independent ARM64 verification, explicit real installation inventory,
reviewed options/identity mapping, backup scope including relevant `/share`, and
a physically available matching return image. Source availability alone does
not prove an installable return artifact. Preserve current Raw/events/options;
never restore an older `/share` snapshot over newer data as an automatic rollback.
Separate HA dashboard/UI files are separate artifacts. Full P0 acceptance still
requires the real same-window baseline plus Live jitter/misses, console occupancy,
CPU/IO/backlog and render evidence; numerical device budgets remain unapproved.


## Local add-on entrypoint extension — unpublished, 21 October 2026

The normal Supervisor `update`/`rebuild` and `start` route can select explicit
offline preparation without a Docker shell or a changed container command.
This extension is a new candidate after abf6dcd, not covered by its old image
identity or its full test run. No production activation is claimed.

An operator-reviewed `/share/guardian_battery/guardian-prepared-migration-request.json`
selects only offline preparation. Exact fields: `contract` =
`guardian-prepared-migration-request-1`, `guardian_version`, sealed `source_commit`,
absolute existing `root`, `plan` inside that root, `plan_sha256` over its exact bytes,
and integer `steps` from 1 through 1,000,000. Startup validates all bindings before opening
migration stores, then reuses `PreparedMigration.begin/step` from the existing CLI.
One add-on start executes at most that many durable steps and exits. This explicit
offline work budget may span multiple CLI batches; each individual step retains
its existing bounded I/O/transaction contract. It is not a live background job or
a promise of a duration. Choose the budget from the measured copy rehearsal. Repeat explicit
starts until the logged phase is `complete`; interruption resumes durable state.
No collector, serial, MQTT publisher, HTTP service or automatic activation is started.
The request stays present even after completion, so an ordinary restart cannot
silently switch to acquisition. Disable operator-managed automatic restarts for
this offline procedure; do not confuse an expected stopped state with failure.

After the separate migration/readiness review, the operator withdraws the migration
request (preserving it as evidence) and supplies
`/share/guardian_battery/guardian-prepared-activation.json`, using exactly the
existing activation contract and source/version/layout-hash checks above. This
shared control file solves the unavailable private `/data` access in the ordinary
local-add-on workflow. The root can still bind actual `data/options.json` and
`share/guardian_battery` paths. Two activation files, or any activation together
with a migration request, fail before acquisition or migration. Invalid, oversized
or dangling controls never fall back to legacy. No files are deleted or withdrawn
automatically. Shared controls require the same trusted operator custody as other
Guardian files in `/share`; do not include credentials.

For return, stop the sole writer and preserve current data and control files, then
withdraw both shared controls and the private activation marker if present. A
subsequent start selects legacy; historical backups must never overwrite newer Raw.
Actual backup coverage, real-copy rehearsal, final source/image binding and HA
acceptance remain separate requirements. No ordinary startup migrates implicitly.

For a layout rooted at `/` (to bind actual `/data/options.json`), the reviewed
migration plan must select a persistent `state_directory` inside its root, e.g.
a distinct explicitly reviewed directory under `/share/guardian_battery`. The
migration checkpoint and output layout are placed there. Omitting this optional
field retains the existing offline-root placement for old plans and fixtures;
container-root temporary state is not suitable for an operational migration.
The explicit state directory is part of the frozen plan hash and cannot escape
the migration root. Rebuilds must preserve this directory and all derived stores.
