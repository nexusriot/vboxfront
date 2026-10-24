# Review — 2026-10-22

Applies to source version **1.10.0** (1.11.0 features are covered by their own tests).

Reviewed the application, command construction, destructive file workflows,
process lifecycle, compressed imports, test fixtures, build scripts, CI and
user/design/backlog documentation. All fixes below are uncommitted.

## Findings fixed

| Priority | Finding and impact | Fix |
| --- | --- | --- |
| P1 | Batch conversion deleted all existing targets before the first clone. Failure or Skip could destroy outputs for commands that never ran. | Prepare each overwrite target immediately before its own command; stop on preparation failure. |
| P1 | Choosing Run in plan mode bypassed batch overwrite preparation and the registry snapshot. | Use the same per-command preparation in both modes, with one snapshot before the first clone. Cancelling the plan changes no target. |
| P1 | Gzip size inference trusted the last four bytes when the value exceeded the compressed size. Wrapped lengths and concatenated members could silently truncate imported disks. | Require measurement or a known exact size for every compressed input. Test concatenated members and a plausible wrapped trailer. |
| P1 | Decompression errors could still produce a successful import; corrupt XZ raises LZMAError, which was uncaught in measurement and Qt callbacks. | Catch decompressor errors, validate stream length, and propagate failure even when the child exits zero. Failed imports are not added to the library. |
| P1 | RAW export deleted an existing target before the free-space warning and final busy check. Cancelling could destroy the file without starting an export. | Complete preflight before preparing the overwrite; the replacement now preserves a backup. |
| P1 | RAW import and byte-range extraction could overwrite another registered image after a generic overwrite prompt. | Refuse registered backing files across all media kinds. Apply the same complete registry check to RAW export. |
| P2 | QProcess crash exit status was ignored, allowing a zero crash code to be treated as success and run follow-ups or queued commands. | Normalize crashes to a nonzero result in both process runners; drain remaining output on completion. |
| P2 | Closing a running batch left pending queue entries alive until process completion/destruction. | Explicitly discard pending commands before cancelling during close. |

## Coverage and documentation

Added unit/integration regressions for overwrite timing, plan Run/Cancel,
preparation failure, skipped targets, export cancellation, registered-target
guards, gzip sizing, malformed XZ, stream failures and length mismatch,
crash status, and closing a running batch. Added E2E workflows proving plan
mode overwrites succeed and a first-clone failure preserves a later target.

README and DESIGN now describe measurement requirements, failure semantics,
and per-command overwrite preparation. ROADMAP no longer lists delivered
1.9.0 proposals as open. Trash management and persistent undo were subsequently
implemented in 1.10.0; remaining limitations are recorded below.

## Architecture refactor and follow-up features

The refactor moved production code into `vboxfront_app` with explicit module
boundaries for domain/models, commands, persistence, services, process runners,
widgets and dialog groups. `vboxfront.py` remains the entry point and compatibility
facade. Build version discovery, Debian source staleness checks and the Docker
source copy were updated. The refactor passed all 401 unit/integration tests and
65 E2E tests before feature implementation began.

All eight follow-up features are implemented:

1. Recoverable overwrite targets: keep a backup beside the output, remove it
   after success, and restore it after failure. Target leases prevent concurrent
   replacement/restoration by another application instance.
2. Trash manager: logical/allocated sizes, selected restore/permanent deletion,
   and persisted batch undo across application restarts.
3. Operation recovery: atomic per-operation records, interrupted operations,
   inspection, guarded partial cleanup and confirmed retry with prior settings.
4. Background archive measurement: cancellable QThread and source-signature cache.
5. VM storage tree: controllers/slots with direct attach/detach and an occupant
   recheck immediately before a command is queued.
6. Registry snapshot comparison: added/missing/moved/changed media by kind/UUID.
7. Saved named filters: save, replace, apply and delete, persisted in QSettings.
8. Latest-batch results: succeeded/failed/skipped outcomes and confirmed retry
   of selected failures, preserving command preparation and follow-ups.

The new service and integration tests cover rollback, interruption, concurrent
access, safe partial cleanup, retry completion, persistent trash, saved filters,
measurement/cache/cancellation, snapshot validation/comparison and VM slot
changes. Seven additional E2E workflows cover overwrite success/failure, restart
undo/filter persistence, batch retry, VM attach/detach and snapshot comparison.
The offscreen main-window layout was rendered and inspected; filters have their
own toolbar so the controls remain visible at the default window size.

## Version and repository hygiene

The source version and top Debian changelog entry are 1.10.0. Runtime, Makefile
fallback and Debian changelog parsing agree. VERSION remains a package-metadata
override and does not rewrite the executable's version.

Ignore rules cover build/venv outputs, Python bytecode, generated specs (except
the maintained vboxfront.spec), backup specs, image trash/recovery files and
platform desktop metadata. Git checks verified 15 generated paths are ignored,
14 source/build-input paths remain visible, and no tracked file matches an
ignore rule. Ordinary image and JSON fixtures are intentionally visible.
Docker ignores also exclude runtime trash and recovery files from build contexts.
Local Markdown links and changelog date/weekday fields were checked.

## Validation scope

The final suites contain 429 passing unit/integration tests and 72 passing E2E
tests. E2E uses real Qt processes and files against the repository's stateful fake
VBoxManage. Python compilation, shell syntax, whitespace checks, CLI version and
Makefile version checks also pass.

A separate smoke check used isolated temporary files/settings with installed
VirtualBox 7.2.4. RAW-to-VDI conversion succeeded, but a real clone could not start
the COM service (`NS_ERROR_SOCKET_FAIL`). Real VirtualBox clone/attach/detach
workflows remain unverified in this environment. The Docker daemon is unavailable,
so E2E was run locally with the same offscreen suite rather than in a container.
No VM was started, no commits were created, and no package/release was built.

Automatic overwrite recovery protects the named backing file. It does not back
up every companion file of multi-file image formats, and in-place `--existing`
clones have no automatic rollback. Complete batch outcomes last for the window
session; failed file-producing operations have persistent recovery metadata.
Registry snapshots contain metadata, not disk contents.
