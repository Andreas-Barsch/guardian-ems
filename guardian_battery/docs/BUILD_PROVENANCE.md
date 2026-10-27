# Battery 0.8.6 build provenance — locally prepared, unpublished

Source base is b90c134 plus the reviewed Research-budget patch and local release
preparation. Manifest, runtime and eventual BUILD_VERSION must identify 0.8.6;
paired MCP is 0.8.4, Tunnel 0.8.1, Diagnostic Engine 0.4.12.
No final source SHA, context or image exists for this uncommitted candidate.
After a separately approved release commit, package the entire Battery directory
using the existing clean-commit prepare_build_context tool below. Its resolved
commit becomes .guardian-source-commit. Do not use b90c134 for the new marker.
Dockerfile, packaging and sealing mechanisms remain unchanged; no partial overlay.

Before installation, verify a current paired Battery 0.8.5 / MCP 0.8.3 rollback
package with both images, configuration and current data/provenance. The older
Battery 0.8.4 backup does not establish this. Never overwrite newer events with
older data. No packaging, build, publication, installation or live acceptance is
claimed. The following sections retain historical preparation statements.

## Historical 0.8.5 preparation (retained)

# Battery 0.8.5 build provenance — prepared, unpublished

The approved release preparation packages only the reviewed Reader I/O fix.
Manifest, runtime and BUILD_VERSION must identify 0.8.5. Research-MCP remains
0.8.3, Tunnel 0.8.1 and Diagnostic Engine 0.4.12. After the reviewed local
release commit, the existing prepare_build_context tool packages that clean
commit; its full SHA becomes .guardian-source-commit. Dockerfile and sealing
mechanism remain unchanged. Installation and live acceptance are recorded
separately; this source document does not claim their completion.

Preserve the current 0.8.4 image/options and current data, including the 70
historical events and their provenance. Never restore old data over newer events.

## Historical 0.8.4 preparation (retained)

# Battery 0.8.4 build provenance — locally prepared, unpublished

## Current release preparation

The confirmed target pair is Battery **0.8.4** / Research-MCP **0.8.3**; Tunnel
0.8.1 and Diagnostic Engine 0.4.12 remain unchanged. Battery manifest, runtime
version and eventual `BUILD_VERSION` must all identify 0.8.4. The provenance
mechanism below is unchanged; this version preparation creates no build context
or image and performs no installation.

Functional source base: local commit `3b3f3a4`. After a separately reviewed release
commit, use the existing packaging tool on that clean final commit; its SHA, not
`3b3f3a4` or the installed `2d026dd` marker, becomes `.guardian-source-commit`.
Package the full Battery directory into a new external destination, verify hashes
and modes, and preserve the existing local add-on identity/options and `/share`.
No app-only overlay and no wholesale merge into GitHub's other Battery line.
MCP publication uses its own narrow branch on then-verified GitHub main.

Before a later installation, retain current Battery/MCP image artifacts and
configuration plus complete current Guardian data. The verified 0.8.3 source
context is not itself a saved 0.8.3 image. Restoring old code is not restoring
new-event access: old Battery readers skip schema 2 / `guardian_soc_gap_v1`.
Preserve all old/new event bytes and appropriate configuration; do not overwrite
post-update data with an old snapshot. No restore, build, version publication
or production acceptance is implied by this document.

## Historical 0.8.2 provenance preparation


## Ergänzung für die lokale Maintenance-Korrektur 0.8.3

Der unten dokumentierte Herkunftsvertrag wird unverändert für Battery 0.8.3
verwendet: Manifest, Laufzeit und `BUILD_VERSION` müssen 0.8.3 melden; der
Quellmarker muss den neuen sauberen Maintenance-Commit bezeichnen. Nicht den
0.8.2-Marker übernehmen. Das vollständige Paket entsteht mit demselben
Packaging-Werkzeug aus dem neuen Commit. Die folgenden 0.8.2-Angaben beschreiben
den bisherigen Stand. Neue Installation und Rückkehrsicherung werden extern
separat nachgewiesen; kein GitHub-Release wird damit behauptet.


This restores internal build provenance for the simple SOC-crash release without
adopting the other 0.8.1 development line. SOC rules, events, storage and the
Research API/MCP contract are unchanged. Provenance is reported in the Battery
startup log, not in a new API field. Diagnostic Engine remains 0.4.12.

## Packaging after a separately approved commit

Use Python 3.12 or newer on the packaging machine. Only after the reviewed change
has been committed and the repository is clean, run:

```sh
python3 guardian_battery/tools/prepare_build_context.py /absolute/new/battery-context
```

The destination must not exist and must be outside the repository. The tool
rejects staged, unstaged and untracked changes, resolves HEAD to a full commit
SHA, then exports **that exact SHA** with `git archive`. Ignored/untracked files
and `.git` are not copied. It writes `.guardian-source-commit` from the exported
revision, replacing any marker in the archive. No commit ID is hardcoded in the
source. The final commit cannot be named until it exists; do not reuse the
installed 0.8.1 marker or label an uncommitted build with the previous HEAD.

Retain the generated context unchanged. Later, under separate deployment
authorization, use the complete context for the existing local Battery add-on;
do not overlay only `app/` onto the old Dockerfile. Preserve the existing app
identity, options and `/share` independently. The old local build context must be
secured separately before replacement. No publication, HA build or installation
is performed by the packaging command.

## Build and runtime contract

Home Assistant supplies `BUILD_VERSION` from the manifest. An independently
invoked Docker build must explicitly supply the same value (currently 0.8.2).
The Dockerfile copies the packaged marker and invokes `tools/seal_build_info.py`
against the copied application's `GUARDIAN_VERSION`. Empty or differing build
versions and malformed markers fail the build. The writer exclusively creates
`/app/build-info.json` with `guardian_version` and `source_commit`; the Dockerfile
makes it read-only and removes the temporary writer and marker. The OCI version
label uses the same `BUILD_VERSION`. It does not claim a source revision via an
inherited base-image label.

At the beginning of `main()`, before options, services or acquisition, Battery
requires readable JSON, a matching runtime version and a full lowercase 40-digit
commit SHA. Invalid or absent metadata refuses startup. A valid start logs
Battery version, source commit and Diagnostic Engine version. Imports do not
read build metadata; isolated lifecycle tests supply an explicit fixture.

This is traceability for a trusted, unmodified packaging path, not signed
attestation. Runtime validation cannot prove that a SHA exists in Git or detect
later manual changes to a packaged context. Record the exported commit and final
image identity during the later build. No container build, image import or HA
acceptance is established by unit/packaging tests. The existing 0.8.1 rollback
images and their configuration remain separate from the new source preparation.
