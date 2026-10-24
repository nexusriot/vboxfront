# VBoxFront — design notes

Modular PyQt6 frontend for `VBoxManage` media operations. This document
records the architecture, the VirtualBox behaviours the design is built
around, and the testing strategy. User-facing docs live in
[README.md](README.md); the open backlog in [ROADMAP.md](ROADMAP.md).

## Ethos

The app launches media operations through `VBoxManage`, with each command
echoed into the output panel and output streamed live. Services also manage
local settings, trash, recovery backups and archive measurement. These local
actions are separate from the CLI commands and are described below; no
VirtualBox SDK/API connection is used.

## Layout

`vboxfront.py` is the CLI entry point and compatibility facade. Implementation
lives in `vboxfront_app/`, with explicit imports and no dynamic source loading.
Views and workflows depend on services/persistence, which use the shared
domain and models.
The domain layer imports without Qt or a display.

| Module | Responsibility |
| --- | --- |
| `version`, `constants`, `models` | Release version, shared CLI/UI vocabulary, media/backend/finding records |
| `domain` | Parsers, storage geometry, media analysis, filters and exported rows |
| `commands`, `compression` | Argument lists, password transport helpers and compressed streams |
| `persistence`, `trash` | QSettings library/history/folders, snapshots, persistent delete batches |
| `recovery` | Atomic operation records, exclusive target leases, backup and partial-file handling |
| `measurement` | Cancellable QThread worker and cache keyed by source identity, size and timestamps |
| `snapshots`, `library_views`, `batches` | Snapshot changes, saved filter/VM slot models, batch outcomes |
| `theme`, `widgets`, `processes` | Palette helpers, media widgets, QProcess runners |
| `disk_dialogs`, `media_dialogs`, `app_dialogs`, `attach_dialog` | Existing dialogs grouped by workflow |
| `feature_dialogs`, `features` | Recovery/trash/results/snapshot/VM dialogs and their action mixin |
| `window`, `app` | Main window orchestration and application startup |

The compatibility facade reexports existing names; new production code imports
the owning module. Tests patch collaborators where they are used, rather than
patching the old facade. PyInstaller discovers the package through normal imports;
the E2E container copies the package, and Debian staleness checks include all
package Python files. `version.py` remains the only release-version literal.

Command construction is centralised in `commands.py`'s `*_args` builders so
tests can assert argv exactly. Workflows also build simple command lists
where no shared builder is needed. The GUI passes argument lists directly
to `QProcess`; exported plans/history are shell scripts.

Alongside the package sit `tests/` (unit, offscreen Qt) and `e2e/` (the
containerised end-to-end suite and its stateful fake VBoxManage); neither is
packaged into the binary.

## Process model

Two `QProcess` wrappers with different jobs:

- **`CommandRunner`** runs visible operations, including mutations and
  explicitly requested Info/Contents/password checks, one at a time. Merged
  stdout/stderr streams into the output panel; a `finished(int)` signal drives follow-up work.
  Extras:
  - **Progress:** VBoxManage prints `0%...10%...` without newlines during
    long operations. Each output chunk is scanned for percentages with an
    8-character carry-over tail, so a value split across chunks
    (`"5" | "0%"`) still matches. The progress bar appears on the first
    match and hides when the process exits.
  - **stdin secrets:** `run(args, stdin_data=…)` writes the data on
    `started` and closes the write channel. Used for encryption passwords so
    secrets never appear in argv, the process list, or the echoed command
    line. The placeholder is the literal filename **`stdin`**, not `-`: `-`
    means "prompt on the console", and VBoxManage turns terminal echo off
    before reading it, so on a pipe it fails with *"Failed to retrieve echo
    setting (VERR_INVALID_FUNCTION)"* without ever looking at the data.
    `readPasswordFile()` special-cases the name `stdin` and plain-reads the
    stream instead — see `PASSWORD_STDIN`.
  - **A streamed stdin** for `convertfromraw stdin`, which reads a whole
    image off standard input. `stdin_data` is one buffer in memory and will
    not do for 40 GB, so `run(..., stdin_stream=…)` takes a file object and
    feeds the child a megabyte at a time, driven by `bytesWritten` — the next
    chunk only goes in once the last has drained, so the event loop keeps
    turning and the window stays usable throughout. With `stdin_total` the
    bytes written drive a real percentage instead of waiting for the child to
    print one. The stream is closed on every exit path, failed start
    included.
  - **stdin is always closed** once the payload (if any) is written. Leaving
    it open hung any command that tried to read it, with only Cancel to get
    out — which is exactly what the test fake did.
  - **two secrets:** changing an encryption password passes *both* passwords
    in one call, and stdin cannot carry them — the first
    `readPasswordFile("stdin")` consumes the whole buffered read and the
    second comes back wrong (`VBOX_E_PASSWORD_INCORRECT` on 7.2.12). So that
    one command uses `write_secret_file()`: 0600 temp files, passed by path,
    removed by `CommandRunner` on exit, on the failed-start path, and when
    VBoxManage is missing entirely. Removal unlinks the files; it does not
    guarantee secure erasure. `EncryptDialog.command_parts()` creates
    them at launch time rather than at accept time, so a dialog that is
    accepted but never run leaves nothing behind.

**Header label.** The status line is an `ElidingLabel`: a plain `QLabel` sized
itself to the full command line, which is far wider than a toolbar row, and
that dragged the header past the window edge until the label and the buttons
were squeezed out of view. It is `Ignored` horizontally and re-elides on every
resize, keeping the untruncated line in its tooltip (and in the output panel).

**Launch failures.** `run()` emits `finished` itself when VBoxManage cannot be
found, so it sets `last_command` *before* emitting — otherwise the history
files that failure against whatever ran previously and never records the
command that actually failed. Relatedly, a configured-but-unusable VBoxManage
path is honoured rather than silently ignored, and `vboxmanage_problem()`
reports it: the old check only asked whether *something* was found, so a
mistyped Settings entry produced nothing but "Failed to start" on every
command.

**Command history.** Every `CommandRunner` invocation is appended to
QSettings with its exit code and a timestamp (`load_history`/`append_history`,
newest last, capped at 200). Stored as one JSON blob rather than a QSettings
string list, because the INI backend splits lists on commas and a command
line can contain them. All visible runner commands are recorded, including
read-only Info/Contents checks. Background refresh traffic goes through
`CaptureRunner` and is excluded to avoid `list`/`showvminfo` noise on every F5.
- **`CaptureRunner`** is a one-shot buffered runner for *read-only*
  commands (`list`, `showvminfo`, refresh-time `showmediuminfo`). It may run
  while `CommandRunner` is busy. Each capture is read-only, but its listing
  may reflect an operation in progress; another refresh can be requested.

**Capture deadline.** `CaptureRunner` kills itself after
`TIMEOUT_MS` (120 s — generous, because `list -l` walks every registered
medium) and reports exit 124. Without it a wedged VBoxManage stranded
`_refreshing` forever: every later refresh folded into `_refresh_pending` and
the window silently stopped updating with nothing on screen to explain it.

**Refresh chain.** A refresh uses sequential capture steps. It initially
reads backends/system properties, reopens library paths not covered by valid
cache stamps, then runs `list -l hdds|dvds|floppies` and populates all panes. A `_refreshing`
flag coalesces re-entrant refresh requests (`_refresh_pending`).

**Quitting.** `closeEvent` asks before quitting while a command is running:
the child dies with the app, and a half-written `clonemedium` target or a
half-encrypted image is worth one prompt. Test tear-downs must therefore leave
nothing running — an unmocked `QMessageBox` there blocks the whole suite.

**Command queue.** Batch operations (Compact all VDIs, and every action that
takes a multi-selection) push `(args, stdin, follow-up)` tuples onto
`_cmd_queue`; `_on_runner_finished` pops the next on success and *drops the
whole queue* on the first failure — a failed compact usually means something
systemic (locked medium, missing binary), and silently continuing would hide
it.

The third element is what makes a batch possible. Single operations used to
park their bookkeeping in a shared `_pending_library_*` slot to be applied when
"the" command succeeded; with a queue behind it, that slot is claimed by
whichever command happens to finish, and a batch remove would prune the wrong
library entry. Each command now carries its own follow-up, and
`_run_queue()` is the single entry point that starts one.

**Reading several VMs.** `_collect_vminfo()` walks a list of VM UUIDs one
`showvminfo` at a time and hands over the lot. Sequential for the same reason
the refresh chain is: there is one VBoxSVC, and a burst of clients against it
is how the service gets wedged. Batch detach uses it — one dump per *VM*, not
per attachment, since every medium's slot is in the same dump.

## Asking VirtualBox what it supports

`DISK_FORMATS`/`DISK_VARIANTS`/`DISK_EXT` are now only a fallback. A refresh
reads `list hddbackends` once and caches it in `MainWindow.backends`, and the
create/convert dialogs take their format list, per-format variants and file
extension from there. VirtualBox is the only authority on this, and hardcoding
got three things wrong:

- **Read-only formats.** DMG and VHDX carry neither create bit, so they can
  never be a target — the old list simply never mentioned them.
- **Which formats are disks at all.** RAW registers only DVD and floppy
  extensions, which is exactly why it must not appear as a disk target. The
  old code hardcoded that exclusion; now it falls out of the data.
- **Variants.** VMDK alone got Split2G by name. In fact VDI and VHD support
  Standard+Fixed, while Parallels, QED and QCOW support Standard only.

The capability bits (`CAP_*`) are the SDK's `MediumFormatCapabilities`,
confirmed against real output: VDI lacks `CREATE_SPLIT2G` and has Discard,
iSCSI has Properties but not File, DMG has no create bit. Each backend also
carries its property schema (name, type, default), used by the existing
`MediumPropertyDialog` editor.

## Reading inside an image

`mediumio` opens a medium through VirtualBox's own backend stack, so
`ContentsDialog` can hex-dump or extract any byte range without a VM, a loop
mount, or root — on any supported format, on differencing chains, and on
encrypted media. Notes:

- `--password-file=-` is the same console trap as `--newpassword -`; the
  literal name `stdin` works, so an encrypted read needs no temp file.
- An encrypted medium refuses to be read at all without its password
  (*"Password needed for encrypted medium"*), so the dialog insists on one.
- `cat --output` **truncates its target silently**, so an existing output file
  is confirmed first. Registered backing files are refused by the window.
  Extraction is an in-place output write, without journalled rollback.
- `formatfat` writes a FAT filesystem in place. It is refused on a medium with
  differencing children: a parent in a chain is read-only and VirtualBox
  answers *"Write access denied: read-only"*. `child_names()` spots that from
  the listing, using the same `real_parent_uuid()` placeholder rule as the
  tree.
- **Do not build on `mediumio stream`.** On 7.2.12 it deadlocks (0 bytes out,
  sleeping on a futex) and takes VBoxSVC down with it, leaving every later
  command failing `NS_ERROR_FAILURE` until the service is SIGKILLed. That is
  also the concrete reason `CaptureRunner` is deadlined.

## Rescuing broken media

The listing paints `State: inaccessible` red, and `ResolveDialog` is what to
do about it. Two faults look identical there and need opposite fixes:

- The **file moved** and the registry still points at the old path →
  `modifymedium <kind> <uuid> --setlocation <path>`. This rewrites the
  registry only; the image is never touched. Verified on 7.2.12: a medium
  whose file was moved behind VirtualBox's back goes from `inaccessible`
  straight back to `created`.
- The **image itself is damaged** → `internalcommands repairhd`, offered as a
  dry run first because a real repair rewrites the image in place. Oracle
  ships `internalcommands` explicitly unsupported ("will change in
  incompatible ways without warning"), which the dialog says out loud.

**Trap:** the dialog decides which options apply by asking the *filesystem*,
not by reading `rec.state`. VBoxSVC caches an open medium, so `State:` can
still say `created` for a file that has already moved out from under it.

`UuidToolsDialog` covers the other half of the same problem: copying a `.vdi`
produces two files claiming one UUID, and VirtualBox refuses to register the
second. `internalcommands sethduuid <file> [uuid]` stamps a new one (and
`sethdparentuuid` reattaches a differencing image to its parent). Because it
writes to the *file*, the dialog cross-checks the path against every
registered location and demands confirmation before desynchronising a
registry entry from the image it names.

## The VirtualBox registration problem

Empirically verified against VBoxManage 7.2.12 (2026-07-26):

- `VBoxManage openmedium` **no longer exists** (removed after VirtualBox 4.x).
- `showmediuminfo disk <path>` opens an unregistered medium **transiently**:
  the registration lives only inside the running `VBoxSVC` and disappears a
  few seconds after the last client exits.
- Only two things persist across VBoxSVC restarts: `createmedium` (writes the
  global `VirtualBox.xml` registry, until `closemedium`) and attachment to a
  VM (stored in the VM's registry).

Consequence: a registry-only listing cannot durably show unattached images.
VBoxFront therefore keeps its own **media library** — per-kind path lists in
`QSettings` (`library/disk|dvd|floppy`):

- *Add file…* and *Import RAW* append to the library (RAW via
  `_pending_library_add`, applied only when the conversion exits 0).
- Every refresh re-opens each library path with `showmediuminfo` *before*
  listing, so library entries appear in `list -l` output like any registered
  medium (with capacity, state, attachments).
- *Remove* drops the entry (`_pending_library_remove`); *Move* rewrites it
  (`_pending_library_move`). Both are applied only on exit 0.
- Stale/broken entries produce a one-line `[library] could not open …` note
  in the output panel and can be pruned in *File → Settings*.

## Parsing notes (all fixture-tested)

- **`list -l` blocks** are nominally separated by blank lines, with keys as
  `Key: value` at column 0 — but neither holds in general. Values wrap onto
  continuation lines that are **indented** for `In use by VMs:` and
  `Property:`, and **flush left** for a multi-line `Description:`, which is
  free text and may therefore contain blank lines *and* lines shaped exactly
  like a field. **Trap:** a description line reading `UUID: …` used to become
  the record's UUID, silently retargeting every later operation (compact,
  resize, `closemedium --delete`) at another medium, and a description
  containing a blank line used to split the block and drop the medium from
  the listing altogether. The parser therefore works off `MEDIUM_KEYS`, the
  field labels in VBoxManage's own emission order: a record opens only at a
  `UUID:` that is not inside a `Description`, a field opens only on a known
  label that comes **later in that order** than the field being read, and
  everything else continues the current value — blank lines included, so the
  one separating two records is stripped off again when the record is built.
  Unknown labels are continuations too, which keeps warnings merged in from
  stderr from eating the record that follows them. **The two halves of that
  trap meet** when a description holds a blank line *and* the line after it
  reads `UUID: …`: the blank line is the record separator, so the parser
  believed it, and the real medium — which had not reached its `Location:`
  yet — was discarded while the phantom that replaced it carried a UUID with
  a newline embedded in it, straight into the next command run on that row.
  A `UUID:` inside a description therefore opens a record only once the one
  being read already has a `Location`, which is the point at which VBoxManage
  could have finished printing it; before that it is description text, not a
  record. **That cuts both ways:** a
  real field left out of `MEDIUM_KEYS` silently becomes part of the previous
  value. It happened with `Access Error:`, which VBoxManage prints (with an
  unindented `VD: error …` continuation) for a medium it cannot open — folded
  into `State`, it put a three-line blob in the State column, stopped
  `State == "inaccessible"` from ever matching (no red highlight, and
  Compact-all stopped skipping broken disks), and was only caught by looking
  at a screenshot of the running app. Every state comparison now goes through
  `state_word()`, which takes the first line, so a future stray field degrades
  to a cosmetic wart rather than a silent behaviour change. `Storage format` is
  preferred over the historical `Format` key (both share a slot in the
  order). `Size on disk`, `In use by VMs` and `Parent UUID` exist only in
  `-l` output (short `list hdds` has no size-on-disk at all).
- **`showvminfo --machinereadable`** is `key=value` with optional quotes on
  either side. Storage slots look like `"SATA Controller-1-0"="/path"` plus
  `"<ctl>-ImageUUID-<port>-<device>"="uuid"`. **Trap:** per-slot flag keys
  (`"<ctl>-nonrotational-1-0"`) have the same shape, so slot lookups must be
  prefixed with a *known controller name* (from `storagecontrollername<N>`),
  never matched with a generic `name-port-device` regex.
- **Attach/detach:** `find_attachment` locates the (controller, port,
  device) of a medium UUID; `find_free_port` picks the first port whose
  device-0 slot is `"none"`, falling back to port 0 on a full controller.
  **Trap:** that fallback is not free, and `storageattach` on an occupied
  slot exits 0 having *silently replaced* what was there, detaching it from
  the VM — so the attach dialog reads the slot back with `slot_occupant`,
  flags it inline, and requires an explicit confirmation before overwriting
  one. The attach dialog fetches VM info
  asynchronously through an injected `capture(args, cb)` callable (stale
  responses are discarded via a request counter), which also makes the
  dialog fully testable with a canned callback.

## Reach beyond the obvious subcommands

Two things VBoxManage can do that no combo box ever offered:

- **`convertfromraw stdin <target> <bytes>`** imports an image off standard
  input, so a compressed archive can be decompressed straight into it and
  never exists unpacked. The catch is the byte count: it is mandatory, it is
  the *uncompressed* length, and it is how the backend sizes the target
  before a sector arrives. A gzip trailer stores only the last member's
  length modulo 2^32: neither highly compressed images above 4 GiB nor
  concatenated members can be sized reliably from it. All compressed inputs
  therefore require **Measure…** (one streaming pass counting decompressed
  bytes without writing them) or a known exact size entered by the user.
  Stream errors and length mismatches mark the command as failed even if the
  child exits with zero; failed imports are not added to the library. Partial
  output files may remain and are not automatically deleted.
- **`clonemedium --format RAW`** writes a flat image. RAW is absent from every
  format combo because `disk_backends()` drops it: its backend registers no
  HardDisk extension, so it can never be a *managed* medium. That is a sound
  reason to keep it out of Create and Convert and a bad reason to have no way
  out at all — Import RAW had existed since 1.1.0 with no counterpart. Export
  is therefore its own action, bypassing the backend list, and says plainly
  that a raw image is flat: the whole capacity, whatever the source has
  allocated.

## Batches

- The queue is **visible**: `CommandRunner.show_queue()` lists what is still
  waiting, and **Skip the rest** drops the queue while leaving the running
  command alone (Cancel is what stops that one). Thirty compacts used to be a
  single number in the status bar.
- **Plan mode** routes every `_run_queue` through `PlanDialog` first: the
  commands as a quoted shell script, with Copy and Save, and nothing runs
  unless the user says so there. It fits the ethos better than anything else
  in the app — every action is already a visible VBoxManage command, and this
  makes the list portable. Batch convert is careful to delete no overwrite
  target while only a plan is being shown. After Run, `_queue_before` prepares
  each target immediately before its command, taking one registry snapshot
  before the first clone. A failure or Skip leaves later targets untouched.
- **Registry snapshots** (`write_registry_snapshot`) are written before a
  batch remove, a batch convert and a health cleanup, into
  `~/.local/share/vboxfront/snapshots/`. They undo nothing — the trash does
  that for files — but they record every UUID and location as they were,
  which is what a rebuild needs. A snapshot that cannot be written is noted
  and the operation goes ahead: refusing to delete a medium because a log
  file failed would be the worse behaviour.
- **Multi-select Attach** allocates slots with `free_slots()`, which steps
  over occupied ones rather than replacing them. `storageattach` replaces an
  occupied slot silently, and a batch is exactly where nobody is watching
  each command go by; too few free slots is refused outright rather than
  quietly attaching some of the selection.
- **Multi-select Properties** sends only what was changed. The dangerous
  default is a batch stamping the first medium's description onto the rest,
  so a selection that disagrees shows an empty box with a placeholder saying
  so, and an empty box means "leave each one alone".

## Checks that need a command

`health_findings()` is a pure function over the parsed registry, which is why
the health dialog costs nothing to open. Two things do not fit that shape and
are therefore opt-in buttons:

- **Clean up…** unregisters every entry whose backing file is gone — the
  commonest way a registry rots, and until 1.9.0 the dialog could only point
  at it. Plain `closemedium`, never `--delete`: there is no file left to
  delete, and asking for one would only fail.
- **Check attach flags** needs a `showvminfo` per VM, because whether a slot
  passes the guest's TRIM through lives there and not in `list -l`.
  `discard_findings()` reports every disk attached with `--discard` off,
  which is the usual reason a compact reclaims nothing: without it the
  guest's TRIM never reaches the image, so blocks freed inside the guest stay
  allocated forever.

Findings carry a `code` as well as prose, so the fixes key off the code —
matching on the wording would break the moment it changed.

**Snapshot-only media** get their own finding, and their own message in
Detach. `storageattach` edits a VM's *current* state, so a medium held only
by a snapshot has no slot to clear; the old behaviour was "could not locate
the attachment slot", which reads like a bug in the app rather than a fact
about VirtualBox. `snapshot_only()` keys off the trailing `[snap (UUID: …)]`
the listing already carries.

**Why an image is unregistered** is a question only VirtualBox can answer, so
the orphan scan asks it: `showmediuminfo <path>` on each candidate, and
`parse_uuid_collision()` on the refusal. The usual answer is a UUID that is
already taken — a copied `.vdi` — and the fix is *Image UUID tools*, which
the app had shipped since 1.2.0 with nothing pointing at it. A colliding
candidate is unticked, because adopting it would only produce a failing
re-open on every refresh.

## UI model

- Three `MediaPane` tabs (hard disks / DVDs / floppies). Disk-only actions
  (create, compact, resize, convert, properties, move, encrypt, import RAW)
  are disabled on the other tabs.
- Selection is **extended**: compact, remove, detach and convert take whatever
  is selected and queue one command per medium. `_require_selection_multi()`
  falls back to the focused row when nothing is explicitly selected, so a
  single click still works the way it always did. `populate()` restores a
  multi-row selection across the refresh that follows each batch — losing it
  there would mean re-picking the rows every time.
- Batch remove sorts its targets into removable and blocked (attached to a VM,
  or the parent of a differencing image) and says which is which, rather than
  letting `closemedium` refuse half of them one error at a time.
- The **Snapshot** column comes out of the same `In use by VMs` entry as the VM
  name: `vm (UUID: …) [snap name (UUID: …)]`. The name was already in the
  listing, so nothing extra is run for it — the match is anchored on the
  trailing `(UUID: …)]` because a snapshot name can itself contain brackets.
- The disk tree nests **differencing images** under their parents
  (`Parent UUID` chains; unknown parents fall back to top level).
  DVDs/floppies render flat. **Trap:** guarding the *recursion* against a
  cycle is not the same as drawing one. A medium whose parent is itself —
  which `sethdparentuuid` will stamp without complaint — or a pair that name
  each other were both counted as children, so neither reached the root list
  and nothing ever drew them: the rows simply disappeared while the totals
  line went on counting them, and an image that is not on screen cannot be
  selected, inspected or repaired. A self-parent is now ignored when roots
  are picked, and a final pass gives a row to anything the walk did not
  reach, so the invariant is "every record gets exactly one row", whatever
  the chain claims.
- The **Encryption** column shows the cipher of an encrypted medium (or
  `enabled` when the cipher is unknown) and nothing at all otherwise —
  `disabled` on every row is noise; the tooltip carries the password ID.
- Each tab footers a `totals_line`: how many media, how much capacity is
  provisioned, and how much is actually allocated. Deliberately *not* framed
  as reclaimable space — the gap between capacity and size-on-disk is
  thin-provisioning headroom that was never allocated, whereas compacting
  reclaims blocks that were allocated and then freed inside the guest, which
  `list -l` cannot see.
- Sorting is enabled on all columns; `MediumItem.__lt__` compares the
  numeric MB value stored in `UserRole` for the size columns, text otherwise.
  Cells show human sizes (`10.0 GB`); tooltips keep the raw `MBytes` values.
- The `Ctrl+F` filter hides rows whose *subtree* contains no match, so a
  matching child keeps its parents visible.
- Media with `State: inaccessible` are painted red (via `is_inaccessible`,
  never a bare string compare — see the parsing note above), with
  VirtualBox's own `Access Error` text in the State tooltip and in the Resolve
  dialog. Removing is refused for a medium attached to a VM (detach first) or
  one that has differencing children, which `closemedium` rejects with
  `VBOX_E_OBJECT_IN_USE`.
- Selection is remembered by UUID across refreshes so chained operations
  keep their target.

**Health, without launching VBoxManage.** `health_findings()` inspects parsed
registry records and the local filesystem: media VirtualBox cannot open,
backing files that are gone while `State:` still says `created` (VBoxSVC caches it and only re-checks
when something opens the medium), differencing images whose parent is not
registered, encrypted media with no password ID. It deliberately stays quiet
about the merely unusual so an empty result means something. The optional
`repairhd -dry-run` and attach-flag scans run commands; the repair pass only
takes VDIs whose file is actually there — `-format` has to be told which format, and
a wrong guess is a confusing error rather than a check.

**Free space.** `space_warning()` compares `shutil.disk_usage` on the first
existing parent of the target against `allocation_estimate()`: a dynamic image
writes little more than a header, so only a Fixed one has to fit whole. It
warns and never refuses — the estimate cannot know about compression, reflinks
or a quota about to be raised. Checked before launching because VBoxManage does
not reserve space up front: an image that will not fit fails somewhere in the
middle and leaves a partial file behind.

**Export.** CSV via the `csv` module rather than joins — a location or a
description can contain the separator, and quoting it is not the caller's job.
Sizes go out as plain megabyte numbers next to the formatted cells, because the
point of exporting is to add them up somewhere else and "10.0 GB" does not sum.
The TSV clipboard form flattens embedded tabs and newlines instead: it has no
quoting layer, and a mangled cell is worse than a space. The export always
covers the whole tab, never the filter — an export that silently dropped rows
would be worse than none.

**Re-running a command.** History stores the printed command line, so a replay
has to be checked twice: `rerun_blocker()` refuses anything whose secret is
gone (a `stdin` placeholder, or a `vboxfront-pw-` temp file that was removed
when the command exited) — replaying those hangs or fails — and
`is_destructive_command()` decides whether confirmation is a button or the word
RUN typed out. This is what makes the feature safe enough to ship after being
deliberately left out of 1.2.0.

## Remembered UI state

Window geometry, the `QMainWindow` state, the splitter position, the selected
tab and each pane's header layout are saved on close and restored in
`_build_ui`, all in the same per-user `QSettings` store as the rest of the
configuration — `~/.config/vboxfront/vboxfront.conf` on Linux, the registry on
Windows, a plist on macOS. No hand-rolled config file and nothing written
beside the binary.

Qt's own `saveGeometry`/`saveState` blobs are stored rather than
hand-serialised numbers: they already carry the maximised and full-screen
flags, which screen the window was on and the DPI it was saved at, and
`restoreGeometry` re-clamps a window whose monitor has since disappeared.
Consequences worth knowing:

- **Restoring can legitimately shrink a window** to fit the available screen.
  A test that asserts an exact restored size has to pick one that fits, or it
  is asserting the clamp rather than the round-trip.
- A layout saved by an older version has the wrong section count, so
  `QHeaderView::restoreState` refuses it and returns false. Panes fall back to
  their one-off autosize; `mark_columns_restored()` is only called when the
  restore actually succeeded.
- Columns are `Interactive` (and movable) rather than `ResizeToContents`:
  contents-based sizing recomputes on every populate and would overwrite the
  widths the user chose. `autosize_columns()` fits them once, on the first
  listing, and only when nothing was restored.
- The toolbar carries an `objectName`; `saveState()` skips — and warns about —
  any toolbar without one.

Adding the Snapshot column changed the section count, so layouts saved by
1.5.0 and earlier are refused by `QHeaderView::restoreState` and fall back to
the one-off autosize. That is the documented behaviour of the fallback rather
than a regression, and it costs one re-fit on the first run after upgrading.

The window filter text is deliberately *not* persisted: starting up with rows
hidden by a filter set days ago looks like missing media.

## Security

- No shell: argv lists only, `shlex.quote` used solely for *display*.
- Encryption passwords travel via stdin, never argv (see above); the echoed
  command line shows the literal `stdin` placeholder.
- The Remove dialog defaults to keep-file; deleting the file requires a
  second explicit confirmation.
- **Deleting a file is a move, not an unlink.** `closemedium --delete` is
  never sent while the trash is on (the default): the medium is unregistered
  with a plain `closemedium`, and the backing file is then renamed into a
  trash folder by the app itself — `os.replace`, falling back to
  `shutil.move` across filesystems. The default trash is a hidden
  `.vboxfront-trash/` beside the image, which keeps that rename inside one
  filesystem: instant for a 200 GB disk, where a copy is not. An image whose
  own folder cannot be written falls back to `$XDG_DATA_HOME/vboxfront/trash`,
  accepting a slow copy over refusing to delete. This is the whole point of
  the feature: guards make a mistake less likely, only a trash makes one
  survivable, and a rename costs nothing.
  - The move happens in the per-command follow-up, so it runs **only after
    that medium's `closemedium` has exited 0**. Before the command VirtualBox
    still holds the medium; after a failure the medium is still registered,
    and a file moved out from under it would be a second fault on top of the
    first.
  - *Media → Undo delete* restores the newest batch — one `Remove` is one
    mistake, so a remove of five disks puts all five back — and re-adds each
    path with `add_library_path`, because the medium was unregistered and the
    library entry is what re-opens it on the next refresh. `TrashStore` saves
    batch metadata in QSettings and reloads it at startup, so undo survives a
    restart. Trash manager shows logical/allocated sizes, supports selected restore,
    and asks before permanently deleting selected files.
  - Two refusals keep the undo from becoming the thing it protects against: a
    name already taken in the trash is suffixed rather than overwritten (two
    `disk.vdi` from different folders land together), and a restore whose
    original path is occupied again fails loudly and leaves the file in the
    trash rather than writing over whatever is there now.
  - Trash folders are skipped by the orphan scan — offering to adopt an image
    the user has just deleted is the opposite of what that scan is for.
  - Turning the trash off in Settings sends a real `--delete` again, and the
    confirmation says "permanently" instead of naming a destination. The
    dialog reads the setting once, at construction: the promise the
    confirmation makes has to be the one the removal keeps.
- `clonemedium` and `convertfromraw` refuse existing target files.
  `MainWindow._clear_target` now preserves a confirmed overwrite target in a
  uniquely named backup beside it. `OperationJournal` writes intent before
  moving the file, associates the actual command before launch, and removes
  the backup only after a successful command with an output file. A failure
  preserves partial output separately and restores the original. Each batch
  prepares only the command about to run. Existing-image clones (`--existing`)
  retain their in-place behavior and do not support automatic rollback.
- Recovery records are individual atomic JSON files in the per-user data
  directory, so separate instances cannot overwrite each other's records.
  A QLockFile lease prevents concurrent replacement or restoration of the same
  output. Startup marks abandoned operations interrupted without moving files;
  Operation recovery allows inspection, restore, confirmed partial removal and
  a typed RUN retry. Partial identity is checked before removal so a later good
  output cannot be deleted by an old failure record. Recovery tracks the named
  backing file, not every companion file of a multi-file image format.
- BatchReport retains succeeded, failed and skipped entries for the latest
  batch in the window. Batch results retries only selected failures after RUN,
  preserving per-command follow-ups and preparation. Stdin and expired-secret
  commands must return to their original dialog. Batch outcomes are session
  state; file-producing failures also have persistent recovery records.
- **A clone target is checked against the whole registry, not just its own
  source.** A batch target must never be another selected source: deleting
  that target before its clone could destroy data still needed by the batch.
  Re-running conversion in a folder holding earlier outputs can cause this,
  so it is refused outright. A target that is any
  other registered medium's file is named (basename and UUID) in a warning
  that defaults to No, because saying yes to a bare "Overwrite?" also leaves
  VirtualBox's registry entry pointing at a file that is no longer the medium
  it names. `registered_at()` does the lookup through `same_file()`, so a
  symlinked path still matches.
- **The history's re-run guard asks whether a replay would overwrite
  anything**, not whether the verb looks dangerous. `--existing` is on the
  destructive list because it is the one `clonemedium` mode that writes into
  an image that is already there (every other one refuses an existing target,
  so a replay can only fail), and any `--output=<file>` is too, because
  `mediumio` overwrites it silently — the dialog confirms that the first time
  round and a replay would skip the question. `--output=-` is stdout and does
  not count.

## Testing

`make test` → stdlib `unittest`, offscreen (`QT_QPA_PLATFORM=offscreen`),
no display, no VirtualBox needed. `tests/__init__.py` points
`XDG_CONFIG_HOME` at a temp dir so QSettings never touch the real config, and
`XDG_DATA_HOME` with it — the delete trash falls back there.

- `test_parsing.py` — parsers against **real captured 7.2.12 output**
  (including the continuation-line and `-nonrotational-` traps).
- `test_args.py` — exact argv for every builder, compression helpers and
  version/build-input consistency; asserts passwords absent.
- `test_features.py` — service contracts and UI integration for recovery,
  target leases, persistent trash, measurement, snapshot comparisons, saved
  filters, VM slots and batch results.
- `test_dialogs.py` — dialog accept-logic with `QMessageBox` mocked; the
  attach dialog runs against a canned `capture` callback.
- `test_window.py` — `MainWindow` integration against a **fake VBoxManage
  shell script** that serves canned listings and logs every call: populate,
  tree nesting, filter, inaccessible highlight, queue ordering and per-command
  follow-ups, batch remove guards, library re-open calls, selection restore,
  drops, export, health and orphan flows. The fixture lives in
  `FakeVBoxManageTest`, which the suites inherit — subclassing a class that
  *has* tests re-runs all of them once per suite. Its `hdds_output()` is a
  hook rather than a constant, so the delete-trash suite can list images it
  really put on disk and then assert on the files themselves.

`make e2e` → the hermetic **end-to-end** suite in Docker (`e2e/`), or
`make e2e-local` against the host Python. Where the unit suite proves each
command is *built* right, this one proves the workflows built out of them
*work*: the real `MainWindow`, real `QProcess` launches, real files — only
VBoxManage is replaced.

- `e2e/fake_vboxmanage.py` is a **stateful** stand-in, not a canned-output
  script: a JSON registry plus real (sparse) image files, so a create is
  visible to the next list, a clone leaves a second file, a resize moves the
  number the listing reports, `closemedium --delete` really unlinks (which
  is what the trash-off scenario checks is no longer sent by default), and
  `encryptmedium` really reads the secret off stdin and checks it later. It
  implements only what the app invokes and exits 1 on anything else, so a
  command the app starts sending without a handler behind it fails loudly
  rather than passing silently. It also keeps each medium's UUID **inside**
  its image file, as VirtualBox does, which is what makes a copied `.vdi`
  refuse to register alongside its original — the scenario the orphan scan's
  collision message exists for. An earlier version guessed at that by
  comparing file contents, and declared two blank images of the same size
  copies of each other.
- `e2e/test_e2e.py` drives 88 scenarios across create/remove (delete into the
  trash, undo as a batch and newest-first, a DVD coming back to its own shelf,
  a failed unregister that must not move anything, and the real `--delete`
  once the trash is turned off through the Settings dialog itself),
  resize and compact (asserting the reclaim line against the real `st_blocks` delta),
  clone and batch clone, attach/detach round trips, differencing chains,
  the encryption round trip and the two-file re-key, the media library across
  a simulated VBoxSVC restart, import RAW, health and resolve, `mediumio`
  extraction, properties, move, queue abort, history, and export. It also
  covers overwrite rollback, persistent undo/filters, guarded batch retry,
  VM storage actions and snapshot comparisons.
- Container execution uses `--network none` and requires no VirtualBox or
  display. Fixtures isolate their files and settings in temporary locations;
  a locally supplied `XDG_CONFIG_HOME` is honored by the E2E package. The container runs both
  suites, and CI runs the container.

Driving modal dialogs needs `QDialog.exec` stubbed for the whole turn of the
event loop, not just the call: Attach reads `list vms` first and builds its
dialog in the callback, so a patch that ended with the action let the real
modal `exec()` through and hung the suite. For the same reason "settled"
has to include the in-flight `CaptureRunner` children — the runner can look
idle while a detach has not started yet.

Two Qt traps the suite has already paid for: `QDropEvent` does not take
ownership of its `QMimeData`, so a locally-built one is collected while the
event still points at it (a segfault, not a failure); and patching a dialog's
`exec` cannot fill in attributes the window reads, because `__init__` assigns
them per instance and shadows the patched class attribute — the tests swap the
whole dialog class for a stub instead.

CI (`.github/workflows/ci.yml`) runs on pushes and pull requests. The unit
job gates two independent jobs: containerised tests and binary/.deb packaging.
The build job does not wait for E2E. Historical design notes record a real
7.2.12 create/list/remove pass; this refactor's current real-VirtualBox smoke
check is documented in REVIEW.md and was limited by a COM/IPC service error. The fake is faithful to the output
shapes captured from 7.2.12, but it is still a fake: it cannot catch
VBoxManage changing its mind about them.

## Packaging

PyInstaller onefile/windowed via the hand-maintained `vboxfront.spec`
(WebEngine/QML/3D/Multimedia excluded; strip + upx). The `.deb` is assembled
with plain `dpkg-deb` (`scripts/build-deb.sh`): `Recommends: virtualbox`
rather than `Depends` (VirtualBox may come from Oracle's repo), version
strings are made dpkg-legal by prefixing bare git hashes with `0.0.0~git.`.
`VERSION` comes from `git describe` when the checkout has tags and otherwise
falls back to `APP_VERSION` in `vboxfront_app/version.py`, which is the
canonical runtime version — the spec's bundle version and
`scripts/build-deb.sh` parse it out rather than restate it, and
`tests/test_args.py` fails the build if any of them drifts or if the top
changelog stanza names a different version. PyInstaller does not
cross-compile — binaries are host-arch only. `VERSION` overrides Debian
package metadata, not the runtime or macOS bundle version.

## Additional library workflows

SavedFilterStore persists named expressions in QSettings; applying one uses the
existing filter box and matching code. Snapshot comparison validates exported
JSON and compares rows by kind/UUID, displaying additions, missing records,
location changes and other field changes without editing the registry. Snapshot
filenames include milliseconds and a random suffix to avoid replacing another
snapshot written in the same second.

VM storage renders VM/controller/slot trees using controller geometry. Attach
requires an empty slot, detach requires confirmation, and both reread VM info
and verify that the displayed occupant has not changed before queuing a command.
This is a storage view; it adds no VM start/stop/snapshot lifecycle controls.

Archive measurement runs in a parentless QThread retained until completion.
Cancellation requests interruption between decompressed chunks. Results are
cached only when the source stat signature is unchanged. The cache includes
path, device/inode, length, nanosecond mtime and ctime and lasts for the session.
The dialog refuses Import while a measurement is still active.

## Workflows built from existing commands

Release 1.11.0 adds no new kind of VBoxManage call; each workflow strings
together commands the app already ran one at a time, and keeps the rule that
every action is a visible command.

- **Compare contents** hashes `mediumio cat --output=-` for both media on a
  worker thread (`verify.py`), stderr to a temp file so a chatty failure
  cannot stall the stream. Container formats differ, guest data does not, so
  this is the only meaningful equality between a VDI and its VMDK clone.
- **Flatten** is `clonemedium` of the last link: VirtualBox reads through the
  parents, so the clone is the merged disk. `reports.chain_problems` refuses
  a chain with an unregistered parent, an inaccessible link or a link whose
  file is gone — the case the registry still calls "created".
- **Golden image** is `modifymedium --type multiattach` then one
  `storageattach` per VM. VirtualBox creates each VM's differencing image
  itself; the app only picks a free slot (`domain.first_free_disk_slot`, never
  a floppy controller, never an occupied slot) and takes a registry snapshot.
- **Browse** runs `vboximg-mount --image <uuid> -g <dir>` under
  `$XDG_RUNTIME_DIR/vboxfront-mounts`. The mount is judged by the mountpoint
  appearing, so it works whether the tool stays in the foreground or forks.
- **Undo last change** records inverse commands from each forward command's
  follow-up, so only what succeeded is undone. Properties are restored with
  auto-reset first, while the disk is still immutable.
- **Auto-refresh** watches VirtualBox.xml *and* its folder: VBoxSVC saves by
  rename, which drops a plain file watch. Events are debounced, compared by
  (mtime, size, inode), and ignored while a command or refresh of ours runs
  and for three seconds after a refresh, since our own command already
  triggers one.
- **CLI** (`cli.py`) reuses the parsers, builders, library and history; it
  never imports QtWidgets. `compact-all` prints its plan unless `--run`.

## Known limitations

| Limitation | Why / workaround |
|---|---|
| One visible operation at a time | CommandRunner and batches are serial; read-only CaptureRunner jobs may overlap them |
| Changing an encryption password spills two secrets to disk | Two stdin secrets in one `encryptmedium` call do not work (the first read swallows the buffer), so the re-key writes two 0600 temp files and removes them when the command exits — including on the way out of the app |
| Library entries are paths, not UUIDs | A moved-outside-the-app file shows as a `[library] could not open` note; prune in Settings |
| `list -l` refresh cost | Three list calls plus new/changed library probes (and initial backend/system-property probes). Missing library entries invalidate their cache stamps and are reopened on the next refresh, so a transient missing row may require another refresh |
| Create-time medium properties | `mediumproperty set` exits 0 and changes nothing for a property the backend only reads at creation (VDI's `AllocationBlockSize`); the dialog says so |
| `repairhd -dry-run` sign-off | Prints "Corrupted VDI image repaired successfully" whatever it found, having written nothing; the verdict is the line above it |
| Free-space estimates | A guess: they cannot know about compression, reflinks or a quota about to change, so they warn and never refuse |
| A resize or format needs a childless medium | A parent is read-only to VirtualBox; both actions name the child and stop rather than passing `VERR_VD_IMAGE_READ_ONLY` along |
| Trash purge is opt-in | Off by default; Settings can purge by age or total size. The newest delete batch is never purged, so a cap smaller than one disk still leaves Undo delete working |
| Undo depends on its persisted index | Delete batches survive restarts through QSettings; clearing or losing those settings leaves the trash files in place but removes their original-path metadata |
| A trashed file still occupies its disk | A rename frees nothing: the space comes back only when the trash folder is cleared |
| An ETA is linear extrapolation | From the only signal there is, the percentage VBoxManage prints; a clone is not uniform work, so it is prefixed "~" and nothing is shown below 5% or in the first seconds |
| Drift is reported, not resolved | Unexpected registry changes are noted after refresh; listings can become stale again and failed captures or library-cache invalidation may require another refresh |
| Compressed import is gzip/bzip2/xz | All three are standard library, so nothing has to be installed. zstd and lz4 would need an external binary, and a missing one is a worse failure than not offering the format |
| Undo last change is session-only | Move, re-point and Properties edits are undone by commands built from the listing before the change; after a restart the registry may have moved on, so nothing is persisted |
| Compare contents reads everything | `mediumio cat` streams the whole guest-visible disk on both sides; a 100 GB disk is 200 GB read. Encrypted media are not offered |
| Browse needs FUSE and a partition table | `vboximg-mount` ships with VirtualBox on Linux and macOS, not Windows, and is always read-only here. On 7.2 it refuses a blank disk ("Error parsing volumes on disk") and shows an unpartitioned filesystem only as `vhdd`. Its daemon holds the medium for a moment after unmount, so an immediate Remove can report it in use |
| The end-to-end suite talks to a fake | Faithful to 7.2.12's output shapes, but it cannot notice VBoxManage changing them; the real-VirtualBox pass stays manual |
