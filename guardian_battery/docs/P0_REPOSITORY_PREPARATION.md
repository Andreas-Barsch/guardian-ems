# Local persistent preparation and cell display composition (phase 2c2)

This is an explicit local composition, not a production startup replacement.
`history_preparation.create_local_repository_preparation` requires three absolute
paths. Initialization is explicit; restore refuses missing stores. Constructors
and status do not enumerate sources, decode raw data, start threads or reconstruct
legacy diagnostic caches. No main/factory selection, UTC writer, version or
scientific threshold changes are included.

## Durable execution

`RepositoryPreparation` keeps requests, stream/day cursors, frontier provenance,
pauses, errors and epoch-dispatch cursors in the same projection SQLite database.
A nonblocking advisory lock in the derived repository directory permits one executor
per bounded unit (never the RawRegistry writer lock). Contention reports `busy`.
One `step` performs one bounded unit, under existing research cancellation checks
and, through `RepositoryStartup`, the existing `StartupBudget`/live-priority gate.
The old `DiagnosticStartup` and its cache-pair handover remain unchanged. The local
facade reports `legacy_cache_pair_ready=False`; it does not claim to have created
CurrentCondition/aggregate cache pairs. `diagnostic_startup_merge.repository_predecessor`
reads a small confirmed canonical checkpoint without constructing `SortedSamples`.

A confirmed frontier is an explicit day plus provenance, not the host clock and
not a claim that all possible measurements exist. Earlier days receive normal
close requests even when no late record arrives. The frontier day remains open.
Every registered canonical identity stream is dispatched from its durable next day.
Missing predecessors remain blocked; only the stream's declared origin permits
bootstrap. Empty open days publish an initial checkpoint, enabling consistent
empty-day display dependencies without fabricated measurements.

A configured, existing RawRegistry is watched through metadata/receipt pages.
The actual RegistryCatalogConsumer verifies one receipt, the actual SourceCatalog
indexes at most 32 records, and entry metadata dispatch advances separately with
durable offsets. Fresh receipt proofs enqueue normal catchup. Older records enqueue
corrections; declared source revisions also traverse old indexed offsets in bounded
steps so removed days are not overlooked. No RawRegistry writer, file adoption or
Raw mutation is called. The consumer must already be able to validate its previous
receipt chain; unavailable overwritten evidence is an explicit error, not a skip.

Already-ingested EpochStore changes are polled by metadata while idle. Dependency
planning and dispatch are bounded; change/dependency acknowledgement is atomic.
Closed-day corrections use the actual CanonicalDayRepository convergence driver.
Open-day corrections preserve the old publication and privately restart from its
validated predecessor. Repeated queued correction requests use an immutable
request identity, retaining the same correction after a lost acknowledgement.

Unknown physical origins are not inferred. All required identities must have
explicit canonical stream definitions. A newly observed unknown serial/position
blocks visibly; additionally, final Display record counts must match the sum of
selected Canonical identity counts. A new occupancy epoch cannot silently disappear
merely because another stream with the same serial exists. Registration of a new
valid origin/checkpoint remains explicit. This is not automatic topology invention.

## One-time legacy inventory

`request_inventory` explicitly creates a LegacySourceInventory job. Its directory
replay after a process loss is reported by that existing component and belongs only
to this one-time job. Pages are ingested into the real catalog, then indexed with
persisted cursors. A lagging queue acknowledgement recovers at most one bounded page
from the authoritative catalog cursor; it never uses an OFFSET scan over history.
Auxiliary files do not consume a global 512-file allowance. The original audit case
(one raw source plus 512 auxiliary files) is covered, followed by a warm start with
no directory or raw read.

Observed legacy provenance remains `legacy_observation_not_writer_fsync`; it is not
upgraded to a writer durability receipt. Unknown kinds or changing inventory scopes
remain explicit failures. No real migration or deletion has been performed.

## Display contract and consistency

DisplayDayRepository calls the actual existing `_channels`, `_new_bucket`, `_add`
and `_public` functions, retaining the existing resolution, extrema, mean, conflict,
protected SOC point and observation-coverage rules. Its source is the real registered
CellWindowAdapter. This phase supplies the **cell display** adapter; Hycube inputs
and the existing Hycube display path are not converted or activated by this factory.
The immutable metadata states this source scope explicitly.

One display step updates one channel at the four existing resolutions. The current
raw record is persisted while its channels are processed; continuation does not
re-read or decode it. Only after its last channel does the record cursor/hash advance.
Metadata distinguishes an in-progress record from a caught-up prefix. Per-bucket
internal/public JSON is capped at 60,000 bytes, and the prepared page at 1 MiB;
overflow is explicit, never truncation or a whole-day fallback. A raw read is at most
64 KiB and one record. These are structural limits, not HA timing acceptance budgets.

Checkpoint, bucket/cadence updates and publication share a transaction. A lost
acknowledgement replays prepared results without another raw read. Corrections keep
all bucket changes private and stream their final replacement four buckets per page.
Old published revisions remain readable. A captured canonical bundle set, its epoch
fingerprints and the active source snapshot are checked at publication. All selected
canonical streams must agree on input and epoch vectors; final display input hash
and identity record counts must agree with that canonical prefix. Late input cannot
be treated as an ordinary append. Stale dependencies are exposed by reads.

`display_window_store.repository_page` and `canonical_window_store.repository_page`
are **explicit paginated repository entry points**, not drop-in replacements for
the legacy gzip/SQLite window API. They return immutable publication/validity metadata.
Canonical count ropes remain descriptors; `CanonicalDayRepository.clipped` performs
exact existing clipping without expanding whole phases. Interactively indexed
window-query integration and the legacy reader selection remain separate work.

## Bounds, retention and remaining integration

No call materializes all days or all queued jobs. Registry/index/inventory/epoch
units have explicit page bounds. Identity dependency sets are capped at 64 and
report overflow. Published revisions are conservatively pinned. Canonical count
nodes, display revisions, completed jobs, abandoned private drafts and inventory
records currently have **no automatic retention policy**. Storage can therefore grow;
there is no claim of production storage acceptance. Cleanup must eventually use
bounded ownership/reachability traversal, not a full Python materialization, and
must preserve pinned readers and rollback evidence. This phase deletes no real data.

The 272 earlier architecture/canonical tests remain unchanged. Added tests cover
real bucket parity, confirmed append and late input, revisions, open/closed epoch
corrections, empty-day closure, warm restore, explicit missing origin/predecessor,
actual process exits, cancellation, atomic cursor rollback and the startup audit.
Legacy preparation/startup/display regressions are also executed. All tests use
synthetic temporary files under the existing production-path/network test guard.

This is not full legacy startup migration: diagnostic cache-pair conversion,
automatic establishment of previously unknown identity origins, Hycube adaptation,
interactive window-reader replacement, retention/GC policy, F/R artifacts and real
HA acquisition timing remain open. No production integration, deployment, resource
acceptance or overall A1–A9/T01–T16 approval follows from these local tests.


## 2c2 review correction: inventory handoff and binding

The inventory catalog cursor acknowledges catalog ingestion only. The queue cursor
acknowledges dispatch. If catalog ingestion committed before a crash, preparation
replays the entire unacknowledged range in pages of at most 32 manifest entries,
even when the catalog already marked the inventory complete. It never skips that
range or starts ingesting a newer page first. Idempotent child-job insertion and
the queue's dispatch acknowledgement share one repository transaction. A crash
before enqueue, after enqueue or before cursor update therefore cannot lose a job;
a crash after commit cannot create a second job for the same inventory entry.

Dispatch reads the immutable SourceCatalog observation binding. The manifest's
stat fingerprint is only `observed_revision`; index jobs use `catalog_revision`,
`receipt` and `frontier` from the actual binding. Both indexing and entry-metadata
iteration are bounded by that captured frontier. A later confirmation cannot
silently expand the older job. The original observation and source records remain
unchanged. A superseded active revision or missing receipt fails explicitly.

`needs_receipt_or_revision` produces a visible blocked decision job, not an index
job for an unregistered candidate. `known_prefix_unconfirmed_tail` may index only
the bound confirmed prefix and also retains a visible decision for the unknown tail.
Other known or newly registered neighbours continue through their index jobs.
This does not claim that a combined Canonical/Display window is usable while one
of its physical source prerequisites remains unresolved. Pending decisions are
not silently closed; explicit source handling and a fresh observation are required.
Older index jobs without an observation binding fail with a precise fresh-inventory
requirement rather than guessing one. No real installation state was migrated.

Regression evidence includes process exits and exception rollback at five handoff
points, a catalog-finished manifest with a multi-page dispatch gap, confirmed append
reinventory, changed/unconfirmed and unchanged/new neighbours, and later confirmation
without widening a saved prefix. All 418 preceding tests remain unmodified.
