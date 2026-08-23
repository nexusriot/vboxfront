# VBoxFront — design notes

Single-file PyQt6 frontend for `VBoxManage` media operations. This document
records the architecture, the VirtualBox behaviours the design is built
around, and the testing strategy. User-facing docs live in
[README.md](README.md); the open backlog in [ROADMAP.md](ROADMAP.md).

## Ethos

The app is a *transparent* wrapper: every operation is exactly one
`VBoxManage` invocation, echoed verbatim into the output panel with its
output streamed live. Nothing talks to the VirtualBox API behind the
user's back, so anything the GUI does can be reproduced (and audited) in a
shell.

## Layout

Everything lives in `vboxfront.py` (~1.8k lines), deliberately single-file:
the tool is small, PyInstaller onefile packaging stays trivial, and there is
no import graph to maintain. The file is ordered so that the pure,
Qt-independent layer comes first and is importable by tests without a
display:

| Section | Contents |
|---|---|
| Constants | `KIND_META` (disk/dvd/floppy nouns, list categories, attach types, file filters), formats/variants/types/ciphers, column indices |
| Pure helpers | `parse_media_list`, `parse_machinereadable`, `parse_vms_list`, `find_attachment`, `find_free_port`, `parse_capacity_mb`, `format_mbytes`, arg builders (`create_disk_args`, `attach_args`, `encrypt_args`, …), QSettings library accessors |
| Process runners | `CommandRunner` (visible, streaming), `CaptureRunner` (silent, buffered) |
| Dialogs | `CreateDiskDialog`, `CreateDiffDialog`, `CreateFloppyDialog`, `ResizeDialog`, `ConvertDialog`, `ContentsDialog`, `RemoveDialog`, `ResolveDialog`, `UuidToolsDialog`, `PropertiesDialog`, `EncryptDialog`, `AttachDialog`, `SettingsDialog`, `CommandHistoryDialog` — all plain `QDialog` |
| Views | `MediumItem` (numeric-sorting tree item), `MediaPane` (one tab: tree + populate/filter/selection) |
| Shell | `MainWindow` (actions/menus/toolbar, refresh chain, command queue, per-op flows) |

Command construction is centralised in the module-level `*_args` builders so
tests can assert argv exactly and the UI code never assembles strings ad hoc.
There is no shell anywhere — arguments are passed as lists to `QProcess`.

## Process model

Two `QProcess` wrappers with different jobs:

- **`CommandRunner`** runs *mutating* operations, one at a time
  (`_require_idle` guards every entry point). Merged stdout/stderr streams
  into the output panel; a `finished(int)` signal drives follow-up work.
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
  - **stdin is always closed** once the payload (if any) is written. Leaving
    it open hung any command that tried to read it, with only Cancel to get
    out — which is exactly what the test fake did.
  - **two secrets:** changing an encryption password passes *both* passwords
    in one call, and stdin cannot carry them — the first
    `readPasswordFile("stdin")` consumes the whole buffered read and the
    second comes back wrong (`VBOX_E_PASSWORD_INCORRECT` on 7.2.12). So that
    one command uses `write_secret_file()`: 0600 temp files, passed by path,
    shredded by `CommandRunner` on exit, on the failed-start path, and when
    VBoxManage is missing entirely. `EncryptDialog.command_parts()` creates
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
line can contain them. Only mutating commands are recorded — the read-only
refresh traffic goes through `CaptureRunner`, and logging it would bury the
history under `list`/`showvminfo` noise on every F5.
- **`CaptureRunner`** is a one-shot buffered runner for *read-only*
  commands (`list`, `showvminfo`, refresh-time `showmediuminfo`). It may run
  while `CommandRunner` is busy; both being read-only or independent keeps
  that safe.

**Capture deadline.** `CaptureRunner` kills itself after
`TIMEOUT_MS` (120 s — generous, because `list -l` walks every registered
medium) and reports exit 124. Without it a wedged VBoxManage stranded
`_refreshing` forever: every later refresh folded into `_refresh_pending` and
the window silently stopped updating with nothing on screen to explain it.

**Refresh chain.** A refresh is a list of sequential capture steps: first
`showmediuminfo <kind> <path>` for every library entry (see below), then
`list -l hdds|dvds|floppies`, then populate all panes at once. A `_refreshing`
flag coalesces re-entrant refresh requests (`_refresh_pending`).

**Quitting.** `closeEvent` asks before quitting while a command is running:
the child dies with the app, and a half-written `clonemedium` target or a
half-encrypted image is worth one prompt. Test tear-downs must therefore leave
nothing running — an unmocked `QMessageBox` there blocks the whole suite.

**Command queue.** Batch operations (Compact all VDIs) push
`(args, stdin)` tuples onto `_cmd_queue`; `_on_runner_finished` pops the next
on success and *drops the whole queue* on the first failure — a failed
compact usually means something systemic (locked medium, missing binary),
and silently continuing would hide it.

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
carries its property schema (name, type, default), which is what a future
`mediumproperty` editor needs.

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
  is confirmed first. Nothing is deleted by the app — mediumio writes it.
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
  stderr from eating the record that follows them. **That cuts both ways:** a
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

## UI model

- Three `MediaPane` tabs (hard disks / DVDs / floppies). Disk-only actions
  (create, compact, resize, convert, properties, move, encrypt, import RAW)
  are disabled on the other tabs.
- The disk tree nests **differencing images** under their parents
  (`Parent UUID` chains; unknown parents fall back to top level, cycles are
  guarded). DVDs/floppies render flat.
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

The window filter text is deliberately *not* persisted: starting up with rows
hidden by a filter set days ago looks like missing media.

## Security

- No shell: argv lists only, `shlex.quote` used solely for *display*.
- Encryption passwords travel via stdin, never argv (see above); the echoed
  command line shows the literal `stdin` placeholder.
- The Remove dialog defaults to keep-file; `--delete` requires a second
  explicit confirmation.
- `clonemedium` and `convertfromraw` fail with `VERR_ALREADY_EXISTS` rather
  than overwriting, so a confirmed overwrite target has to be deleted first.
  That happens in `MainWindow._clear_target`, immediately before the command
  is launched — never while the dialog is still up, where a busy runner would
  leave the file destroyed and nothing written in its place. Both dialogs also
  refuse a target that `same_file()` resolves to the source, which otherwise
  deleted the very disk being converted.

## Testing

`make test` → stdlib `unittest`, offscreen (`QT_QPA_PLATFORM=offscreen`),
no display, no VirtualBox needed. `tests/__init__.py` points
`XDG_CONFIG_HOME` at a temp dir so QSettings never touch the real config.

- `test_parsing.py` — parsers against **real captured 7.2.12 output**
  (including the continuation-line and `-nonrotational-` traps).
- `test_args.py` — exact argv for every builder; asserts passwords absent.
- `test_dialogs.py` — dialog accept-logic with `QMessageBox` mocked; the
  attach dialog runs against a canned `capture` callback.
- `test_window.py` — `MainWindow` integration against a **fake VBoxManage
  shell script** that serves canned listings and logs every call: populate,
  tree nesting, filter, inaccessible highlight, compact-all queue ordering,
  library re-open calls, selection restore.

CI (`.github/workflows/ci.yml`) runs the suite and builds the `.deb`
artifact on every push. A manual end-to-end pass against a real VirtualBox
(create → list → remove) was verified on 7.2.12; it is not part of CI since
runners have no VirtualBox.

## Packaging

PyInstaller onefile/windowed via the hand-maintained `vboxfront.spec`
(WebEngine/QML/3D/Multimedia excluded; strip + upx). The `.deb` is assembled
with plain `dpkg-deb` (`scripts/build-deb.sh`): `Recommends: virtualbox`
rather than `Depends` (VirtualBox may come from Oracle's repo), version
strings are made dpkg-legal by prefixing bare git hashes with `0.0.0~git.`.
`VERSION` comes from `git describe` when the checkout has tags and otherwise
falls back to `APP_VERSION` in `vboxfront.py`, which is the one place the
release version is written down — the spec's bundle version and
`scripts/build-deb.sh` parse it out rather than restate it, and
`tests/test_args.py` fails the build if any of them drifts or if the top
changelog stanza names a different version. PyInstaller does not
cross-compile — binaries are host-arch only.

## Known limitations

| Limitation | Why / workaround |
|---|---|
| One VBoxManage command at a time | Serialised by design for transparency; batch compact queues sequentially |
| Change encryption password needs two steps | Two stdin secrets in one `encryptmedium` call is untested territory — decrypt, then encrypt |
| No host DVD drive attach | Only image files are managed; use the VirtualBox GUI for host drives |
| Library entries are paths, not UUIDs | A moved-outside-the-app file shows as a `[library] could not open` note; prune in Settings |
| `list -l` refresh cost | One process per library entry + 3 list calls; fine for typical registries |
