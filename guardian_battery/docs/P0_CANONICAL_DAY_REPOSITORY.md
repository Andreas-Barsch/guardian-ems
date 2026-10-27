# Phase 2c1: real Canonical engine, persistent daily publications

Unactivated local composition. Uses `CanonicalPhaseMachine`, its snapshot/restore,
`IdentityEpochResolver`, `_clear_daily_counts` and `clip_interval` from the existing
`canonical_phase.py`. No new phase classification, thresholds, hysteresis,
endpoint or identity rule. Existing defaults and caller APIs remain compatible.
No main/factory, UTC writer, acquisition, HA, GitHub or version change.

## Explicit execution contract

`CanonicalDayRepository(repository,catalog,epochs).initialize()` adds private job,
identity-stream, revision, dependency and timestamp-sequence storage to the explicit
ProjectionRepository path. Existing source/index and EpochStore paths are supplied
by the caller. No product defaults or directory discovery.

A stream names one existing physical occupancy identity, its serial and a documented
origin day. The existing unresolved-position-1..6 identities are also supported,
with physical_serial=None; they are not converted to guessed physical serials.
The real IdentityEpochResolver decides record membership using its unchanged
occupancy rules and a complete prior occupancy checkpoint. Re-entry creates a
different identity; it is not silently merged into the earlier stream.

- `begin_day` needs the immediately preceding published_closed checkpoint. Without
  it, returns awaiting_predecessor. Bootstrap is explicit and allowed only at the
  defined origin. If earlier position evidence would require an unknown occupancy
  origin, returns awaiting_identity_origin. A new occupancy stream may explicitly
  seed its identity-only continuation from a current, closed predecessor publication
  for the same serial; its phase engine starts separately.
- `step` consumes a bounded frozen SourceCatalog/CellWindowAdapter page. Current
  defaults:32 records, caller ceiling128 and64KiB Raw bytes. Sorting uses record UTC
  timestamp, source ID/revision and physical offset, never local filename day.
  Selected measurements feed the actual machine and captured historical config.
- `refresh` on an idle open job explicitly captures a later indexed prefix. It
  processes only the new ordered delta. Known newly indexed records behind its
  consumed sort key are rejected as requiring explicit correction; none is skipped.
- `request_close` captures the current confirmed/indexed source prefixes and moves
  to closing_requested. Exhaustion publishes_closed, including empty days. This
  requires no late record: a normal day boundary can explicitly close its prior day.
  It is an explicit orchestrator operation, not a new autonomous scheduler or
  invented grace period. Closed means a finished named input revision, not proof of
  complete measurements, discovery or no possible future late data.
- Ordinary open pages publish coherent incremental revisions. Corrections build
  a full replacement privately in bounded pages, then switch the head once. An
  open successor corrected from its new predecessor remains **open**, never falsely
  closed merely because its currently confirmed prefix is exhausted.

Epoch capture is bounded and uses actual ConfigHistory timestamp/parameters and
PositionSnapshot effective_at records. Current Condition is not substituted for
Canonical's historical as-was configuration. Parameters stored on the explicit
stream are the existing Canonical machine parameters, unchanged by the adapter.

## Complete checkpoints without accumulating JSON arrays

The existing engine retains count_timestamps for pending diagnostic/visual
intervals. Hydrating that whole list at every page would grow with the accumulated
phase. `canonical_checkpoint_storage.py` supplies immutable balanced timestamp
sequences in the derived repository. Append, concatenation and lookup use bounded
nodes/logarithmic paths rather than copying a whole prior interval. A checkpoint
stores a root/count/digest descriptor; the exact sequence remains accessible in
bounded pages. Leaves and branches are checked when accessed, UPDATE is prohibited,
and missing/damaged nodes fail explicitly. This is corruption detection, not a
security attestation or backup.

`CanonicalPhaseMachine` accepts an optional timestamp_sequence_factory. Without
it, ordinary Python lists and the existing JSON snapshot contract remain unchanged.
The storage adapter packs/unpacks explicit descriptors. The same real reduction
logic concatenates sequences; it does not approximate counts or timestamps.
Existing clip_interval can binary-search these sequences. Scalar/full continuation
state includes identity/serial, source cursor and record proof, last timestamp/order,
config revision/current position, axis/stable/candidate and candidate time, pending
diagnostic and visual suffix, relative endpoint state and semantic/algorithm IDs.

Day closure stores the day's pending output before calling the existing daily
count reset. Thus canonical cumulative counts and ongoing phase start remain,
while next-day count timestamps start empty. Endpoint/hysteresis/candidate state
continues exactly. Intervals are clipped by the existing function with a half-open
internal day bound; no sample at next midnight is counted in the earlier day.
Empty days carry the exact continuation without fake samples or reset-to-rest.

This phase stores explicit identity streams; automatic all-identity orchestration
and public aggregate/UI composition are not activated. Inactive/returned identities
remain separate streams. Unknown physical identity remains explicitly unresolved.

## Atomic publication, private progress and races

A private transaction commits timestamp nodes, emitted rows, full engine state,
source sort key/input proof and a prepared-page record. Nothing private is reachable
from a publication. Staging is replayable after an ambiguous acknowledgement.
ProjectionRepository's existing publish body is factored into an internal
transaction method, preserving public behavior. The adapter commits publication,
bundle/head, physical source high-water cursor and its corresponding engine/job
cursor in **one** repository transaction. These physical cursors describe the
selected day's referenced input envelope; the exact next-record cursor is the
stored sort key, not a claim that every byte in a mixed-day file belongs to this day.
Source/revision keys are unambiguous structured pairs.

Each day/identity has a separate head/product partition, so bundle membership does
not grow by adding every historical day. The preceding closed bundle is an explicit
dependency. All published revisions are pinned in this local prototype; historical
values and timestamp roots stay readable after a newer head and repository GC.
Retention/unpinning/count-node GC require a later explicit policy and are not
silently applied here. Consequently disk growth/reserve remains an operational gate.

The consumed-epoch race is handled in this order:

1. Capture actual epoch records and their clipped fingerprint before processing.
2. Durably register the dependency with **expected consumed fingerprint**. A changed
   vector is stale/retry, not a new supposedly valid dependency.
3. Hold the EpochStore write guard, validate those precommitted dependencies again,
   hold the derived catalog's short guard for frozen source revisions, then publish.
   Validate the exact preceding publication head inside the repository transaction.
4. Record published-bundle/dependency correspondence atomically with publication.

A crash may leave an unused reservation, never a published result without its
precommitted dependency. Changes after reservation include that dependency in their
high-water mark. Changes before registration or before guarded publication reject
the candidate. RawRegistry registration/append uses neither guarded database;
physical IO competition and HA timing still need measurement. Fixed lock order is
epoch -> catalog -> repository, only around the bounded publication transaction.

`publication_validity`/read_page distinguish historical revision, stale epoch
inputs and pending correction, with explicit as-of-frozen-input Raw coverage.
`published_state` is the immutable checkpoint/provenance accessor, not an assertion
that its bundle is the latest valid evidence. The adapter never claims discovery
or newest Raw coverage from a version string or a successful read.

## Corrections and convergence

A late-record notification explicitly requests a correction at its affected day;
no global history scan is used to rediscover work. `enqueue_epoch_job` consumes an
actual EpochStore invalidation job and maps it to the currently published
bundle/dependency. Unpublished or superseded reservations cannot invalidate a new
current publication merely by being present in the epoch log. Replaying a job is
idempotent. No production event dispatcher is wired in this phase.

Correction starts with the preceding confirmed day checkpoint (strictly before the
affected day), or the explicitly authorized origin. It reads only that day's frozen
sources, stages replacement pages, seals and atomically publishes a new revision.
The durable correction cursor advances one successor at a time. It stops only when
full continuation state (including occupancy), the consumed source-record proof AND
epoch vector converge, or at the known published/open frontier. There is no fixed
number-of-days cutoff. An open frontier is rebuilt with the corrected predecessor
and remains open for subsequent confirmed deltas. Missing predecessor/changed
predecessor or stale captured epochs require explicit resolution/retry; no neutral
bootstrap or retry loop is invented.

Old revisions remain immutable and readable. Direct changes may propagate beyond
an epoch's next effective boundary because engine state carries history; the
correction driver verifies actual state/vector convergence rather than assuming
that boundary ends the effect.

## Validation and remaining limits

Tests use the real engine as continuous reference, actual registered schema1 Raw
and real epoch formats in guarded temporary directories. They cover midnight,
restart, pending phase/counts, empty coverage, append-only deltas, old revisions,
private replacements, publication/engine-cursor process exits, epoch publication
races, predecessor CAS, late records, retroactive configuration/position changes,
new occupancy streams, open successors and correction beyond a short fixed horizon.
The timestamp store is tested with8192 values, exact concatenation, bounded descriptor,
restart/page semantics and damage. Existing Canonical tests and all220 prior
architecture regressions are retained unchanged.

These are local process-abort tests, not power-loss or HA resource tests. Existing
legacy Canonical builders/readers are not switched over. No automatic origin/source
inventory, UTC migration, scheduler/HTTP integration, F/R release artifact, deployed
reader compatibility or real collection-timing guarantee is claimed. Legacy unknown
provenance and receipt/onboarding gates still apply. All overall A/T acceptance
gates remain open. Stop at control point2c1 for independent review.

Position dependency fingerprints preserve every ordered transition at the predecessor effective timestamp and inside the day, including equal-time intermediate absence/reentry. This matches the actual identity resolver; a winner-only position lookup remains unchanged but cannot substitute for the consumed identity trace. A newly inserted intermediate transition invalidates publication even when the final position winner stays unchanged. The trace retains the explicit 256-record budget.
