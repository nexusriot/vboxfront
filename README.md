# VBoxFront

Current source version: **1.11.0**.

A small PyQt6 desktop frontend for `VBoxManage` media operations.

VirtualBox ships a powerful but verbose CLI for managing virtual disks.
VBoxFront wraps the media-related subcommands in a single window so you can
list, inspect, create, compact, resize, clone/convert, import, encrypt,
attach and detach disk images without memorising `VBoxManage` invocations.
Interactive operations show their command and stream its output live.
Background registry/VM probes use a separate buffered runner.

Architecture and design decisions are documented in [DESIGN.md](DESIGN.md);
the open backlog lives in [ROADMAP.md](ROADMAP.md).

## Features

- **Media tabs** — hard disks, DVD images and floppy images
  (`VBoxManage list -l hdds|dvds|floppies`), each with format, capacity,
  size on disk, type, state, attached VMs, the snapshot an attachment belongs
  to, location and UUID.
- **Multi-selection** — select several media and Compact, Remove, Detach,
  Convert, Attach or edit Properties on all of them in one queued run that
  stops at the first failure. Attach fills consecutive free slots, stepping
  over occupied ones rather than letting `storageattach` replace them
  silently; Properties shows what the selection has in common and sends only
  what you change.
- **Visible queue** — what is still waiting is listed under the output panel,
  with **Skip the rest**, which drops the queue and leaves the running
  command to finish.
- **Plan mode** (*File → Plan mode*) — every batch shows the exact commands
  it would run, with Copy as script and Save as…, and runs only if you say
  so there.
- **Registry snapshots** — a batch remove, a batch convert and a health
  cleanup each write the whole registry aside first, UUIDs and locations
  included.
- **Diff-chain tree** — differencing images are nested under their parent
  (snapshot chains at a glance).
- **Table polish** — sortable columns (sizes sort numerically),
  human-readable sizes (`10.0 GB`), tooltips with the raw values, red
  highlighting for *inaccessible* media, and a column chooser on the header
  (right-click) for the eleven columns.
- **Filter** (`Ctrl+F`) — plain words match any column, and terms narrow:
  `fmt:vdi`, `size:>10G`, `capacity:<2T`, `vm:alpha`, `state:inaccessible`,
  `type:`, `snapshot:`, `uuid:`, `location:`, `description:`. Sizes take
  `<`, `<=`, `>` and `>=` with K/M/G/T suffixes. A term naming no field — a
  pasted Windows path, say — stays a plain word.
- **Details pane** (`Ctrl+J`) — every field of the selected medium, laid out.
  It comes from the listing already on screen, so opening it runs nothing.
- **Create disk** — `createmedium` with a size in MB and format/variant
  choices taken from VirtualBox (see *Formats from VirtualBox*).
- **Formats from VirtualBox** — the create and convert dialogs ask
  `list hddbackends` which formats exist, which can be created, and which
  variants each accepts, instead of hardcoding a list. QCOW, QED and
  Parallels appear where they apply; read-only formats (DMG, VHDX) are never
  offered as a target.
- **Attach / Detach** — `storageattach` against any registered VM, with
  optional `--mtype`, `--discard` (guest TRIM, which is what makes compaction
  pay off), `--nonrotational` and `--hotpluggable`; the
  attach dialog reads the VM's controllers via
  `showvminfo --machinereadable`, pre-selects the first free port, derives the
  device number from the controller type (only IDE and floppy controllers have
  a device 1) and marks running VMs. DVD and floppy attachments can also point
  at an empty drive, the Guest Additions ISO or a **host drive**, with
  `--passthrough`, `--tempeject` and `--forceunmount`. Detach locates the right
  controller/port automatically, for one medium or for a whole selection.
- **In-use guard** — media attached to a VM show the VM names in the list;
  removing them is blocked until they are detached.
- **Compact** — reclaim unused space (`modifymedium --compact`), single disk,
  a selection, or **all VDIs in one queued batch**. Reports what it actually
  gave back, measured across the refresh that follows.
- **Resize** — grow a disk (`modifymedium --resize`) in MB or GB, with the
  current → new delta shown as you type, or to an exact byte count
  (`--resizebyte`, which also takes hex and K/M/G). Shrinking is refused with
  an explanation rather than a VirtualBox error, and so is resizing a disk
  that has differencing children — VirtualBox treats a parent as read-only,
  and the message names the child.
- **Snapshot-held media** — a medium attached only inside a snapshot is
  explained rather than failing obscurely: `storageattach` edits a VM's
  current state only, so nothing here can release it.
- **Convert / Clone** — clone to a different format *and* variant
  (`clonemedium --format --variant`), into a new file or into a pre-created
  one (`--existing`). A selection of disks can be converted into one folder in
  a single queued run, with the target names previewed first. An existing
  target is moved to a backup beside the output before cloning, so one that is
  another registered medium's backing file is named (with its UUID) before
  anything is removed, and one that is another disk in the same batch is
  refused outright. Each backup is removed only after its clone succeeds;
  failure restores the original and preserves any partial output separately.
  This also applies when Run is chosen from plan mode. Skipped targets remain
  untouched. Cloning into an existing image (`--existing`) writes in place
  and does not have automatic rollback.
- **Free-space preflight** — create, resize and clone show the target
  filesystem's free space against what the operation will take, and confirm
  before starting one that does not fit. VBoxManage does not reserve space up
  front, so an image that will not fit fails part-way and leaves a partial
  file behind.
- **Import RAW** — convert a raw `.img/.raw/.bin` into a managed format
  (`convertfromraw`); the result lands in the media library automatically.
  Import and RAW export refuse targets belonging to registered media.
- **Import compressed RAW** — the same for a `.gz/.bz2/.xz` archive, without
  unpacking it first (`convertfromraw stdin`): the archive is decompressed
  straight into VBoxManage, so a 40 GB `.img.gz` never needs 40 GB of temp
  space. Use **Measure…** to obtain the exact byte count in one streaming
  pass on a background worker, or enter a known size. Measurement can be
  cancelled; completed results are cached while the source remains unchanged.
  Gzip trailers wrap at 4 GiB and describe only the last member of concatenated archives, so they are never trusted.
  Read errors and size mismatches fail the operation; a failed import may
  leave a partial output file, which is not added to the library.
- **Export to raw** — clone a disk out to a flat image
  (`clonemedium --format RAW`), which is what feeds `dd`, a USB stick or
  qemu. RAW is never offered as a *managed* format, because its backend
  registers no hard-disk extension, so this was the one direction the app
  could not go.
- **Media health** — everything wrong with the registry that can be seen
  without running a command: media VirtualBox cannot open, backing files that
  have gone missing while the registry still says they are fine, differencing
  images whose parent is not registered, encrypted media with no password ID.
  Double-click a finding to jump to the medium, or run a read-only
  `internalcommands repairhd -dry-run` pass over the VDIs. **Clean up…**
  unregisters every entry whose backing file is gone in one pass (plain
  `closemedium` — there is nothing left to delete), and **Check attach
  flags** reads each VM and reports disks attached without `--discard`,
  which is why compacting them so often reclaims nothing.
- **Scan for unregistered images** — walk chosen folders for
  `.vdi/.vmdk/.vhd/.iso/.img` files that neither VirtualBox nor the library
  knows about, and add the ones you pick. **Why unregistered?** asks
  VirtualBox to open each one: the usual answer is that its UUID collides
  with an image already registered — a copied `.vdi` — and the fix is *Image
  UUID tools*.
- **Export** — write the current tab to CSV or JSON (*File → Export listing…*,
  `Ctrl+E`), or copy selected rows as TSV. Sizes are exported as plain
  megabyte numbers as well as formatted cells, so they can be summed
  elsewhere.
- **Drag and drop** — drop image files on the window to add them to the
  library; a dropped raw image offers Import RAW instead.
- **Properties** — change medium type
  (normal/writethrough/immutable/shareable/readonly/multiattach), the
  immutable auto-reset flag and the description
  (`modifymedium --type/--autoreset/--description`). A differencing image's
  type cannot be changed, and the dialog says so rather than letting
  VirtualBox refuse it.
- **Format properties** — view and edit a medium's format-specific properties
  (`mediumproperty get|set|delete`). The values come from the listing that is
  already on screen and the names from the format's own schema in
  `list hddbackends`, so opening the dialog runs nothing. Some properties are
  only read when the image is created (VDI's `AllocationBlockSize` among
  them): VirtualBox accepts the change and keeps the old value.
- **Move** — relocate a disk file on disk (`modifymedium --move`).
- **Password check** — verify a medium's current password without changing
  anything (`checkmediumpwd`).
- **Encryption** — set, change or remove disk encryption (`encryptmedium`,
  requires the VirtualBox Extension Pack). One password goes via stdin
  (`--newpassword stdin`); changing a password needs two secrets in one call,
  which stdin cannot carry, so those go through 0600 temp files that are
  removed the moment the command exits. Neither ever reaches the command
  line. Removal does not guarantee secure erasure from the filesystem. The
  cipher and password ID of encrypted media are shown in the
  listing's Encryption column.
- **Remove** — unregister (`closemedium`), optionally deleting the backing
  file; defaults to the safe keep-file action. Deleting does **not** pass
  `--delete`: the file is moved into a trash folder instead (by default a
  hidden `.vboxfront-trash/` beside the image, so the move is a rename inside
  one filesystem and costs nothing however large the disk is), and
  *Media → Undo delete* puts the last delete back — the whole batch of it —
  including after restarting, library entry included. **Media → Trash
  manager…** lists indexed deleted files with logical and allocated sizes,
  restores selected files, and permanently deletes selected files after
  confirmation. Nothing is emptied automatically unless a purge rule is set
  in Settings (see *Trash purge rule*). The trash can be
  pointed elsewhere or turned off in
  *File → Settings*, and with it off `--delete` is sent for real.
- **Resolve** — rescue a medium VirtualBox can no longer read: re-point the
  registry at a file that moved (`modifymedium --setlocation`), or check and
  repair the image itself (`internalcommands repairhd`, with a dry run
  first). Relocation never touches the file, only the registry entry.
- **Image UUID tools** — stamp a fresh UUID into a copied image so it can be
  registered alongside the original, or reattach a differencing image to its
  parent (`internalcommands sethduuid` / `sethdparentuuid`). This is the way
  past *"a medium with the same UUID already exists"* after copying a `.vdi`.
- **Contents** — read a byte range straight out of an image, with no VM, no
  loop mount and no root (`mediumio cat`): hex dump it into the output panel
  or extract it to a file. Extraction refuses output paths belonging to
  registered media. Goes through VirtualBox's own backend stack, so it
  works on any supported format, on differencing chains, and on encrypted
  media (which refuse to be read without their password).
- **Differencing images** — create a child on top of an existing disk
  (`createmedium --diffparent`); writes land in the child and the parent is
  left alone. The listing already draws these chains as a tree.
- **Format as FAT** — put a filesystem on a medium (`mediumio formatfat`), or
  create a floppy image pre-formatted (`--variant Formatted`) so it is usable
  without booting a VM first. Refused on a medium with differencing children,
  which VirtualBox treats as read-only.
- **Command history** — every command run, with its exit code, in
  *File → Command history…*; copy one, all, or the current command line
  straight from the output panel, or **re-run** one. A command whose password
  went over stdin or through a temp file that has since been removed is
  refused rather than replayed into a hang; a destructive one needs the word
  RUN typed. "Destructive" is about what a replay would overwrite, not how
  the verb reads: `clonemedium --existing` and `mediumio cat --output=<file>`
  both count, because both write over something that is already there. The
  history also saves as a **shell script** — the selection or all of it —
  with any command whose password is gone flagged at the top.
- **Progress bar** — long operations (clone, compact, create) report live
  percentage parsed from VBoxManage's `0%...10%...` stream, with elapsed
  time and an estimate of what is left. Anything that ran over half a minute
  finishes with a window alert and a one-line verdict.
- **Registry drift** — the registry is fingerprinted on each refresh, and a
  change no command of ours explains is reported. The VirtualBox GUI, a VM
  starting or a second copy of this app can all edit it, and a stale row is
  how an action lands on the wrong medium.
- **Context menu** — copy UUID / location, open the containing folder, and
  all per-medium actions on right-click.
- **Remembered layout** — window size and position (including which screen
  and whether it was maximised), the output-panel splitter, the selected tab,
  and each tab's column widths and order — hidden columns included — all come
  back next time. Columns are resizable and reorderable, and auto-fit once on
  a first run. Colours come from the Qt palette, so the greys and the inline
  warnings stay readable on a dark theme.
- **Settings** — custom `VBoxManage` location (useful when it is not on
  `PATH`), the default folder for new images (falling back to VirtualBox's
  own default machine folder rather than your home directory; each dialog
  then remembers where it was last used), the delete trash (on or off, and
  where it lives) and management of the media library.
- Live command output panel with a **Cancel** button; the media list
  refreshes automatically after each operation.

### Recovery and library views

- **File → Operation recovery…** lists failed or interrupted file-producing
  clone/import/export operations. Inspect their files, restore a saved original,
  remove an unchanged partial output with confirmation, or retry after typing
  RUN. Compressed imports reopen the original archive. Passwords and stdin
  contents are never stored. Startup reports unfinished operations in the output.
- **File → Batch results…** shows succeeded, failed and skipped commands for
  the latest batch. Select failures and type RUN to retry them. Commands needing
  stdin or expired password files must use their original dialog. Results are
  kept for the current window session.
- **File → Compare registry snapshot…** compares a saved JSON snapshot with
  the current listing by media kind and UUID. Added, missing, moved and other
  changed fields appear in a read-only table. Refresh first for current data.
- **Media → VM storage…** shows VMs, controllers, and occupied/empty slots.
  Attach an image to an empty slot or detach an occupied one. The slot is
  rechecked before issuing the command; changed slots require reopening the view.
- **Saved filters** live beside the filter box on a separate toolbar. Save the
  current expression under a name, apply it later, or delete the saved view.
- **Compare contents** (*Media → Compare contents…*) — reads two images end
  to end as the guest sees them (`mediumio cat`) and compares SHA-256 hashes,
  so a VDI and the VMDK cloned from it can be shown identical although their
  files share no bytes. The other side can be a flat RAW export. Convert,
  Clone and Flatten offer **Verify contents afterwards**.
- **Flatten chain** — select the last link of a differencing chain and clone
  it into one standalone image (`clonemedium` reads through every parent).
  The chain is checked first (every parent registered, readable and present
  on disk) and left untouched afterwards.
- **Golden image for VMs** — turns a base disk into *multiattach* and attaches
  it to the VMs you tick, each at its first free disk slot. VirtualBox gives
  every VM a differencing image of its own, so they share the base and never
  write to it. Running VMs and VMs without a free slot cannot be ticked.
- **Browse files (read-only)** — mounts an image with `vboximg-mount -g`
  (FUSE, no root) and opens the folder: `fs<N>` holds each guest filesystem
  it can read, `vol<N>` the raw partitions. *Unmount browsed images* (and
  quitting) unmounts them.
- **Disk space report** (*Media → Disk space report…*) — totals, space by VM,
  by folder and by differencing chain, the trash, and dynamic VDIs largest
  first as compact candidates (with a button to compact the ones you select).
  Built from the listing on screen; runs nothing.
- **Undo last change** (`Ctrl+Shift+Z`) — puts back the last Move (file,
  registry and library entry), Resolve re-point (`--setlocation`) or
  Properties edit, after showing the commands it will run. Remembered for the
  window session, up to 20 changes; a batch that stopped half-way undoes only
  what it did.
- **Auto-refresh** (*Media → Auto-refresh on registry change*, on by default)
  — watches `VirtualBox.xml` and refreshes when something else changes the
  registry: the VirtualBox GUI, a VM starting, another tool. Changes caused by
  this window's own commands are not refreshed twice.
- **Trash purge rule** (*File → Settings*) — optionally delete trashed files
  older than N days, or the oldest ones once the trash exceeds N GB. Applied
  at start-up and after each delete; the most recent delete is always kept so
  Undo delete keeps working. Off by default.

Output recovery records live in `$XDG_DATA_HOME/vboxfront/operations.json.d/`
(or `~/.local/share/vboxfront/operations.json.d/`). Backups and preserved partials
stay beside the output file. Recovery protects the named backing file; it does
not provide whole-VM rollback or a backup of every companion file in multi-file
image formats. Existing-image clones (`--existing`) remain in-place writes.
Trash/undo metadata and saved filters use the same per-user QSettings store as
the library. The archive measurement cache lasts for the application session.

### The media library (modern VirtualBox note)

VBoxFront keeps its own **library** of known image paths in `QSettings`,
independently of VirtualBox's registry. This preserves the Add file list when
VBoxSVC forgets a standalone image's temporary registration. Images created or
attached through VirtualBox can also appear through its own registry.
**Add file…** stores the path. Refresh reopens new or changed entries and
uses cached file timestamps/sizes for unchanged ones. If the listing loses an
entry, its cache is invalidated so the next refresh reopens it; another refresh
may be needed after VBoxSVC exits. Entries are dropped again when you remove
the medium (and can be managed in *File → Settings*).

## Command line

The same binary runs headless when given a subcommand, for scripts and cron.
It reads the same settings and media library as the window.

```sh
vboxfront list [--kind disk|dvd|floppy|all] [--csv]   # JSON by default
vboxfront health [--json] [--strict]   # exit 1 on errors (2 on warnings with --strict)
vboxfront space [--json]
vboxfront compact-all                  # prints the script it would run
vboxfront compact-all --run            # runs it, stopping at the first failure
vboxfront verify <medium> <medium>     # exit 1 when contents differ
vboxfront verify <medium> image.img --file
```

Exit code 3 means VBoxManage could not be used or a medium was not found.
Commands run by `compact-all --run` appear in the window's command history.

## Requirements

- **Runtime:** [VirtualBox](https://www.virtualbox.org/) must be installed
  and `VBoxManage` available — either on `PATH` or configured in
  *File → Settings*. The app warns at startup if it is missing.
- **From source:** Python 3.10+ and `PyQt6` (see `requirements.txt`).
- **Building a binary/.deb:** `python3`, `pip`, and `binutils`
  (`strip`); `dpkg-deb` for the Debian package. `upx` is used if present.
- **Running `make e2e`:** Docker. (`make test` and `make e2e-local` need
  neither Docker nor VirtualBox.)

## Running from source

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 vboxfront.py
```

Or simply:

```sh
make run
```

## Tests

```sh
make test
```

Runs the unit test suite (parsers against captured VBoxManage 7.2 output,
command builders, dialog logic, and MainWindow integration against a fake
`VBoxManage`) with the offscreen Qt platform — no display or VirtualBox
installation needed.

```sh
make e2e
```

Runs the unit suite **and** a hermetic end-to-end suite inside Docker. Where
the unit tests check that each command is built correctly, the end-to-end
tests check that the workflows built out of them work: the real application,
real `QProcess` launches and real files on disk, driven against a *stateful*
fake `VBoxManage` (`e2e/`) that keeps a registry and writes actual image
files. A created disk is visible to the next listing, a clone leaves a second
file, a resize moves the number the listing reports, and
`closemedium --delete` really unlinks — so each scenario asserts on what
changed rather than on what was printed.

The container runs with `--network none` and needs no VirtualBox and no
display. To run the end-to-end suite against the host Python instead:

```sh
make e2e-local
```

CI (GitHub Actions) runs on pushes and pull requests. After the unit job
passes, the containerised test job and the binary/.deb build job run
independently; packaging is not gated on E2E success.

## Building

The `Makefile` is a thin wrapper over the scripts in `scripts/`.

```sh
make            # build a standalone binary -> dist/vboxfront
make deb        # build a Debian package    -> dist/vboxfront_<ver>_<arch>.deb
make run        # run from source (sets up a local .venv)
make test       # run the unit test suite
make clean      # remove build/, dist/, .venv and PyInstaller caches
make help       # list all targets
```

The binary is produced with PyInstaller using `vboxfront.spec` (onefile,
windowed, Qt WebEngine / 3D / QML excluded to keep it small). Builds happen
inside a project-local `.venv` so they never touch system Python packages.

PyInstaller cannot cross-compile, so the binary and `.deb` are always built
for the **host architecture**.

### Versioning

`APP_VERSION` in `vboxfront_app/version.py` is the single source of truth for
the runtime version — it names the window title, `vboxfront --version`, the
macOS bundle version and the fallback the build scripts use. `VERSION` prefers `git describe` when the checkout has
tags, so a tagged build carries its tag (and `-dirty` when the tree is not
clean). This controls the Debian package version; it does not change
`APP_VERSION` or the version reported by the executable. Override the package
version explicitly:

```sh
make VERSION=1.10.0 deb
```

## Installing the .deb

```sh
sudo dpkg -i dist/vboxfront_*_amd64.deb
```

This installs:

- `/usr/bin/vboxfront` — the standalone binary
- `/usr/share/applications/vboxfront.desktop` — menu entry

The package *recommends* `virtualbox` (for `VBoxManage`) but does not hard-
depend on it, so it can be used with a VirtualBox installed from another
source.

Remove with:

```sh
sudo dpkg -r vboxfront
```

## Project layout

```
vboxfront.py          CLI entry point and compatibility imports
vboxfront_app/        Application package (domain, services, Qt UI)
  version.py          Single source of the application version
  app.py              QApplication startup, or the CLI when given a subcommand
  cli.py              Headless subcommands (list, health, space, compact-all, verify)
  icon.svg            Application icon (window, .desktop, PyInstaller)
  constants.py        CLI/UI vocabulary and capability flags
  models.py           Media, backend, trash and health records
  domain.py           Parsers, filters, media analysis and exports
  commands.py         VBoxManage argument builders
  reports.py          Space report and differencing-chain helpers (no Qt)
  verify.py           Content hashing through `mediumio cat`
  mounts.py           Read-only browsing through vboximg-mount
  watcher.py          VirtualBox.xml change detection
  workflows.py        Flatten, compare, golden image and browse actions
  persistence.py      Settings, library, folders and registry snapshots
  recovery.py         Durable output backups and operation records
  trash.py            Persistent deleted-file index
  measurement.py      Cancellable archive worker and size cache
  snapshots.py        Snapshot validation and comparison
  library_views.py    Saved filters and VM slot models
  batches.py          Batch outcome model
  processes.py        Visible and captured QProcess runners
  widgets.py          Media tree, details and shared widgets
  theme.py            Palette-based semantic colours
  *_dialogs.py        Dialog groups; attach_dialog.py handles attachments
  features.py         Recovery and library-view UI actions
  feature_dialogs.py  Recovery, trash, results, comparison and VM views
  window.py           Main window and media workflows
vboxfront.spec        PyInstaller spec (onefile, windowed)
requirements.txt      Runtime Python dependency (PyQt6)
tests/                Unit tests (stdlib unittest, offscreen Qt)
e2e/                  Hermetic end-to-end suite + stateful fake VBoxManage
e2e/Dockerfile        Container the end-to-end suite runs in
e2e/run.sh            Build and run it (`make e2e`)
.dockerignore         Keeps the e2e build context to the sources it copies
DESIGN.md             Architecture & design notes
ROADMAP.md            Delivered features and open backlog
REVIEW.md             Review findings, verification and known limitations
build.py              Legacy ad-hoc PyInstaller invocation (kept for reference)
Makefile              Wrapper over scripts/*.sh
scripts/build.sh      PyInstaller build / run / test (provisions a local .venv)
scripts/build-deb.sh  Assembles the .deb from the built binary
scripts/deb/          control.tpl + desktop entry + changelog
scripts/render_icon.py  Rasterises icon.svg for the Windows/macOS builds
.github/workflows/    CI: unit tests + containerised e2e + .deb + Windows/macOS builds
build/                PyInstaller workdir (gitignored)
```

## Notes & limitations

- VBoxManage commands are shown in the output panel. Local trash moves and
  recovery backups are also reported there.
- Long operations (large compact/clone) block another visible operation;
  background refresh captures may still run. The window remains responsive; use **Cancel** to abort. Batch compact queues commands one after
  another and stops on the first failure.
- Compacting only meaningfully shrinks VDI images; other formats prompt for
  confirmation.
- Type changes and detach require the medium not to be in use by a running
  VM; VBoxManage's error is shown verbatim if it refuses.
- Encryption needs the VirtualBox **Extension Pack**.
- `repairhd -dry-run` writes nothing, but on 7.2.12 it signs off with
  "Corrupted VDI image repaired successfully" whatever it found — the line
  above that one is the verdict. The health dialog says so before running it.
