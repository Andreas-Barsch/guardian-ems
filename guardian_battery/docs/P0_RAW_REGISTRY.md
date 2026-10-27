# Phase 2b3: isolated Raw registry and receipt consumer

Prepared local architecture component, not activated. No main/factory/history
request/writer integration, new UTC production, existing-source import, release
or operational acceptance. Existing SOC, diagnostic and phase semantics are
unchanged. This extends the isolated catalog; its previous caller-supplied
receipt API remains available and retains its validation.

## Explicit ownership and bounded work

`RawRegistry.create(explicit_path)` creates a new private registry. It never
opens a product default directory. `register` commits a zero-byte frontier before
`append` can confirm the first complete line. Source ID, revision, writer epoch,
namespace, absolute path and operation ID are supplied explicitly. File identity
is `(st_dev, st_ino)`. Registration is not discovery or historical migration.
Nonempty files require explicit `allow_existing=True`; their bytes remain
unconfirmed until bounded `adopt_tail` calls. No runtime calls this interface.

One nonblocking flock serializes registry writers. Neither registration nor
append opens SQLite or takes a projection/catalog lock. An occupied registry
writer produces a conflict, without automatic retries. This proves lock
separation, not physical SSD/CPU/IO isolation or an HA latency budget.

Each receipt is an immutable checksummed JSON file in a numbered segment
(default 64 receipts, configurable 1–256). Paths follow sequence numbers; no
startup glob, scandir, directory sort or historical journal scan is needed.
The small atomic head identifies the committed sequence/hash. Per-source
checkpoints and presence markers support direct lookup; an absent older
checkpoint with a marker is corruption, not a fresh source. The latest
checkpoint can be reconstructed from the head receipt. No journal compaction
or retention policy is implemented in this component.

A receipt records source/revision/epoch/file ID, predecessor receipt and global
hash, start/end full-line offsets, line count, SHA-256 of the bounded bytes, and
per-source block-chain hash. These are corruption/provenance checks, not signed
attestations. At most 64 KiB of complete newline-terminated bytes per append;
JSON parsing/diagnosis is not part of this transport. Metadata files are capped
at 16 KiB; a pending intent at 128 KiB includes the exact bounded payload.

## Exact persistence order and acknowledgement

The writer uses this sequence:

1. Persist the bounded intent to a temporary file: flush, file fsync, atomic
   replace, parent-directory fsync. It names the expected head and source receipt.
2. Match an already-written pending prefix exactly and append only the missing
   suffix. Flush and fsync Raw, then fsync its parent directory. Never truncate
   Raw to roll back. Registration/adoption also fsync the owned Raw file.
3. Write the immutable receipt through flush/fsync/replace/directory fsync.
   Segment creation is followed by parent-directory fsync.
4. Publish the head through flush/fsync/replace/directory fsync. This is the
   journal commit point. Earlier receipt files are not published by themselves.
5. Persist the source marker/checkpoint, remove the intent, fsync the registry
   directory, then return the acknowledgement.

If any IO operation fails, no success is returned. After an ambiguous response,
explicit recovery consults only the head, the one intent and the affected
source checkpoint. A committed intent is cleared; an uncommitted one completes
only if its existing Raw prefix matches its stored bytes. An immediate retry
with the same operation ID/payload does not duplicate a committed append.
This is last-operation idempotence, not an unbounded operation-ID index.

Synthetic process exits exercise the individual persistence boundaries.
`fsync` ordering is a local software protocol, **not a power-loss guarantee**
for HA Green, its filesystem, controller or device cache. The real flush cost,
loss budget, storage reserve and acquisition timing remain open.

## Tail, damage, rotation and revisions

- Without an intent, extra Raw bytes stay unconfirmed. `inspect_tail` reads a
  bounded prefix; explicit `adopt_tail` confirms complete lines only. A partial
  line is never included in the frontier; oversized lines need an explicit
  decision and are not silently split or discarded.
- A changed file signature blocks normal append. Truncation/rewrite requires
  a new explicit revision; registration restarts its frontier at zero while
  preserving previous journal/catalog entries. It does not rewrite old bytes.
- A changed file ID requires a new source ID. Retaining access to the old
  physical file after rotation is a separate integration responsibility.
- Missing/corrupt head, receipt, pending intent, chain or checkpoint fails
  explicitly. No implicit recreation, reset, full scan or silent skipping.
- The writer checks its most recently confirmed block; consumers check each
  selected block against its receipt. Arbitrary external stat-preserving changes
  outside inspected ranges cannot be detected by bounded access. Concurrent
  foreign writers are outside the ownership contract. This is not whole-file
  integrity attestation or protection against deliberate metadata forgery.
- A cold catalog cannot replay historical block proofs after the corresponding
  Raw bytes have been overwritten or made inaccessible. It reports the conflict
  and retains its cursor. An already caught-up catalog can consume the explicit
  new revision. The journal is metadata plus one pending payload, not a Raw backup.

## Atomic bounded catalog consumer

`RegistryCatalogConsumer.initialize()` explicitly installs additive auxiliary
cursor/proof tables; a normal catalog constructor does not install these tables.
`step(expected_cursor=...)` reads a bounded journal page and validates selected
Raw block hashes/file identities. It then atomically commits source registration
or append receipt, block proof and durable journal cursor in one catalog
transaction. Cursor CAS rejects stale callers. Crash before commit rolls all
back; crash after commit resumes with the next receipt. The consumer does not
write Raw or initiate automatic retries. The existing indexer remains a separate
bounded step; journal confirmation is not a claim of completed time indexing.

Maximum page: 64 receipts/64 KiB receipt bytes (default 32 receipts), plus bounded
head metadata; maximum selected Raw proof bytes: 64 KiB per step. A receipt that
cannot fit the caller's remaining Raw budget stays pending. A too-small budget
returns `budget_blocked`, without advancing beyond it. An invalid later record
rolls back earlier records in the same catalog transaction.

Writer recovery can read the previous block and one pending prefix (up to
128 KiB). A new append following that recovery can verify one more previous
block (up to 192 KiB total for the combined call). Work is bounded by these
blocks, not by accumulated history. Metadata checkpoints are constant-count
lookups; raw proof verification performs no JSON decodes. A cold initial catalog
still requires multiple bounded steps through all relevant retained receipts;
there is no claim that a full rebuild costs O(1).

Tests compare the next 5-byte append after 1/30/365/730 historic receipts,
including a fresh registry object and durable consumer cursor. They also hold
a catalog write transaction while Raw registration/append succeeds. These are
synthetic local boundedness/lock tests, not production timing acceptance.

## Remaining gates

No existing writer, reader factory or HTTP path imports these components.
No HA access, new UTC writer, migration of installed data, version change or
release. Existing 124 kernel/catalog/inventory/snapshot regressions must remain
unchanged and pass alongside the new cases. All overall A1–A9/T01–T16 acceptance
items remain open; Phase 2b3 is submitted for review at its own checkpoint.
