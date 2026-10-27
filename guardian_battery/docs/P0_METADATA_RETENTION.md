# Local 4b1 migration and lifecycle control candidate

Local implementation only. No runtime factory, HA migration, retention schedule,
production cutoff/quota, Raw/archive deletion, version change or installation is
introduced. Scientific identity, maintenance DTO and diagnostic calculations keep
the existing contracts. The historical foundation description follows below.

## Explicit migration and incremental metadata

`MigrationManifest` records exact initialized store identities (resolved path,
device/inode), schema fingerprints, caller-confirmed origin, semantics, rollback
scope and up to 32 concrete units. Supported real adapters are catalog inventory,
selected catalog source indexing, epoch registry consumption, frozen metadata
migration, incremental metadata registry consumption, repository ownership and
publication-window indexing, and cross-store snapshot binding. Diagnostic,
analysis, preparation, canonical and display rows participate through the actual
repository ownership adapter. This is not an adoption of legacy cache pairs or a
claim that all available Raw has been imported. The manifest records the exact
selected work, not a global readiness or coverage assertion.

Each explicit `step(expected_version=...)` executes one bounded adapter page and
acknowledges its durable result. A crash between adapter commit and acknowledgement
resumes the actual adapter cursor; old input is not replayed. Registry units have
an explicit frozen final receipt sequence. New receipts need a new manifest and,
for metadata, a new immutable revision. Completed manifests return without opening
old inputs. Schema/identity/semantic changes fail rather than triggering repair.
Initialization is explicit, with repository ownership DDL committed per table;
it does not scan existing rows. The ownership backfill is separately resumable.

`MetadataLogConsumer` verifies real RawRegistry receipt/block chains, source
identity, byte extents and original line numbers. It consumes original position,
configuration, maintenance revision and technical event formats (maximum 16
records and one 64-KiB confirmed block per step). Maintenance folding uses the
existing sequential revision contract; missing serials are resolved from the
same metadata revision at the event time. Stored identities are not overwritten.
`MetadataRevision` shares immutable AVL pages with its confirmed parent and
records the parent's state hash and root digests. New input advances the copied
cursor vector; it does not reread old Raw. Backdated position observations rebuild
only their affected metadata suffix in checkpointed phases, including same-time
ordering. This is explicitly scheduled work, never a GET fallback. A very early
correction can consequently require much work over many bounded steps.

## Ownership, revocation and retention

`StoreRetirement` indexes native foreign keys, packed count descriptors and the
actual pre-FK links between jobs, publications, diagnostic generations, slots,
samples, results, caches and drafts. Unknown table shapes block adoption. Incoming
references are indexed, not found through repeated whole-store JSON scans.

The caller selects exact derived objects with a reason and rollback evidence.
Completed job removal also requires explicitly closed retry lifetime. Selection,
fencing, whole-group reference validation and deletion are separate bounded
transactions. Changed row hashes/schema or new outside references block deletion.
A partially deleted group must resume; it cannot be unfenced as if untouched.
No traversal silently expands the selected group. Window index and analysis rows
remain protected with their publication. Terminal or demonstrably superseded
canonical/display job bookkeeping can be removed while retaining its immutable
publication; requiring removal of both would unnecessarily retain predecessor
chains. Current heads, retained correction bases and unknown/rollback pins remain
roots. Known automatic pins are separately releasable by explicit policy.

`LeaseOwners` reserves actual private drafts and local reader tokens. Existing
owners are adopted as active, never presumed expired. Explicit revocation needs
the exact owner nonce and a reason/successor proof. Fenced owners cannot create
new references or modify their reserved state. Reservation release is separate
from revocation. Reader checkpoints and scope exit reject revoked tokens; normal
exception cleanup still releases the exact token. This covers published history,
window indexes, diagnostic views/results and both metadata readers. No time-based
expiry or production session policy is inferred.

Retired diagnostic slots/samples and maps cannot be removed while a retained
publication generation or current slot needs them. Current logical evidence is
not a garbage candidate. Active analyses protect statistic contributions. After
explicit removal, later analyses can recompute those derived contributions from
retained samples. `DerivedPageSweep` proposes only currently unreferenced count
or metadata pages within a sealed maintenance frontier; actual deletion still
uses the fenced lifecycle. Children may need further explicit rounds. Work is
bounded per step; total maintenance cost is not claimed independent of the amount
of accumulated garbage. Retained evidence naturally occupies storage.

`SnapshotLifecycle` binds actual canonical/display snapshot fields to catalog
owners using durable, retryable handoff. Parent removal enqueues release in the
same repository transaction. Source references are removed in bounded pages;
source rows/receipts/Raw remain. Already normally released snapshots have an
idempotent absence handoff. Explicitly created request snapshots can be revoked
by exact request identity; unowned historical snapshots are preserved. A request
cannot revoke a durable job's snapshot. Existing readers revalidate the frozen
snapshot before returning Raw records.

## Proof retention and remaining boundaries

Retirement plans/decisions, migration manifests, page-sweep cursors and completed
snapshot release records have explicit archival/pruning operations. Caller-supplied
archive SHA-256 and retry-closure attestations are required; these methods do not
assert that an external backup exists or was fsynced. Nonces or never-reused release
IDs prevent stale proof from deleting a different run. The caller must export the
full bounded-page audit before pruning. Old classic metadata migration proofs are
owned with the explicitly retired metadata revision. Published/canonical source
provenance, RawRegistry receipts, source/catalog/inventory observations and epoch
inputs are not erased by derived GC. Unknown owners and unconfirmed historical
schemas remain protected rather than guessed collectible.

All tests use the existing filesystem/network guard and synthetic local stores.
The external 4b1 report records the frozen source hashes, exact commands, results,
crash matrix and limitations. Local page/SQL bounds do not establish HA latency,
disk reserve or physical I/O capacity. Main/startup, Raw-tail and Daily integration,
productive owner policy, HA baseline/capacity, and actual F/R installation artifacts
remain separate open gates. This candidate is not overall P0 acceptance or deployment
authorization.

---

## Historical foundation checkpoint at 533c052 (unchanged)

# Local metadata and retention foundations — incomplete 4b1 checkpoint

This is an explicit local-only foundation. It does not activate migration,
retention, startup work, or any productive factory. It is not completion of 4b1.

## Metadata input

`MetadataMigration` writes the existing PreparedMetadata tables behind readiness
flags. The caller supplies a bounded list of frozen JSONL inputs, their declared
origins, and provenance. Supported inputs are existing PositionSnapshot records,
actual TechnicalEventSource records, and explicitly prepared TimelineEvent DTOs.
Unknown formats, origins, changed inputs and conflicting revision contracts fail.
No legacy diagnostic/cache pair is accepted or guessed. No source file is changed.

`initialize`, `begin`, and repeated `step(expected_version=...)` are explicit work,
never request fallbacks. A step reads at most 64 records and 65536 source bytes,
or projects at most 64 ordered position snapshots. Defaults are structural test
bounds, not approved HA latency/memory limits. Sorted staging preserves effective
and capture-time ordering. Identity IDs and epoch meanings match the existing
resolver, including intermediate same-time changes and absence epochs.

The durable cursor and derived writes commit together. Restart continues the
recorded page; a completed job returns without reopening its old inputs. Every
consumed line has a byte-extent/hash proof. Input file identity/size/mtime is checked
before/after reading and before readiness publication. This is not a cryptographic
proof against a hostile writer restoring file metadata: the caller must supply
frozen inputs. No claim is made that a live production log is frozen.

Not yet implemented: raw maintenance revision folding/position enrichment,
incremental extension into a new immutable revision without replaying the prior
input, and a unified migration manifest spanning the actual catalog, inventory,
diagnostic, analysis and publication stores. Existing config/position registry
adapters are unchanged. Explicitly empty input scope must be reviewed by callers;
a supplied origin/provenance declaration is not independently authenticated.

## Conservative count-node retention

`DerivedRetention.initialize` installs reference-maintenance triggers on actual
TEXT payload columns and count child edges. `index_step` backfills existing owners
in bounded row pages; no initial whole-store scan is hidden in initialization.
Valid JSON count descriptors are conservatively treated as references, even in
unanticipated payload fields. Owner deletion/update removes those references.
Incomplete reference migration blocks retention. Schema or trigger changes block
a previously approved deletion plan.

`begin(policy=...)` requires an explicit reason and rollback-evidence declaration.
The only implemented scope is `unreferenced_counts`. `decide_step` records retain/
eligible decisions without deleting; `delete_step` rechecks incoming references
and node digest in its transaction before deleting. New references after a
decision protect their node. Existing payloads, private checkpoints, publications
and tree parents are owners, so their referenced trees remain intact. Separate
plans may reclaim children after their unreachable parents disappear; there is
no automatic retry, period, production cutoff, quota or disk-reserve policy.

This does NOT yet release permanent pins or dispose completed jobs, private
orphan drafts, inventory/window snapshots, retired diagnostic slot/sample/statistic
versions, analysis results or the retention/migration audit tables themselves.
Those objects remain retained. Cross-store reader leases, correction ownership,
compatible rollback roots and lost-owner fencing need implementation before a
broader reclamation claim. Current count retention therefore cannot demonstrate
bounded total store growth. It must not be presented as general GC or F/R proof.

## Validation and integration boundary

Tests use synthetic files and the existing guard. They cover original resolver
parity, input mutation, CAS, restart, record/byte bounds, each metadata commit
boundary, reference-index/decision/delete crash points, new references after a
decision, schema changes, and many synthetic count revisions. Existing guarded
regressions are recorded separately in the external checkpoint evidence.

Production main, Raw-tail integration, Daily-window integration, HA capacity and
F/R installation artifacts remain outside this checkpoint. Raw/archive deletion
is neither implemented nor authorized. Overall P0 gates remain open.
