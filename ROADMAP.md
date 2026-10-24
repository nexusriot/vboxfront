# VBoxFront — roadmap

Open, prioritized backlog. Effort: **S** (hours), **M** (a day-ish),
**L** (multi-day). The 2026-07-26 batch implemented the original
high-value + nice-to-have tiers (media tabs, diff-chain tree, create,
attach/detach, properties/move/encryption, progress bar, filter/sort/context
menu, batch compact, media library, settings, tests + CI) — what follows is
what's left or new.

## Delivered in 1.9.0

The two 2026-10-18 proposal rounds are implemented. The subsequent modular
refactor and recovery/library enhancements shipped in 1.10.0, listed below.
The original trash and session undo arrived in 1.8.0.

- [x] Import a compressed raw image without unpacking it
      (`convertfromraw stdin`): gzip, bzip2 and xz. Use Measure for the exact
      byte count or enter a known size; gzip trailers cannot establish it.
- [x] Export to a flat raw image (`clonemedium --format RAW`).
- [x] Health: **Clean up…** unregisters entries whose file is gone;
      **Check attach flags** reports disks attached without `--discard`.
- [x] A medium held only by a snapshot is explained, in Detach and in health.
- [x] The orphan scan can say *why* an image is unregistered (a UUID already
      taken), and leaves such a candidate unticked.
- [x] Multi-select Attach (consecutive free slots, never replacing one) and
      multi-select Properties (only what changed; an empty description box
      never blanks differing ones).
- [x] A visible queue, with "Skip the rest".
- [x] Plan mode: every batch shows its commands first, with Copy and Save.
- [x] Command history saves as a shell script.
- [x] Registry snapshots before a batch remove, batch convert or cleanup.
- [x] A details pane for the selected medium, costing no command.
- [x] A structured filter (`fmt:`, `size:>`, `vm:`, `state:`, …).
- [x] Elapsed time, an ETA, and an alert when a long operation finishes.
- [x] Registry drift detection, via a per-refresh fingerprint.
- [x] Refresh cost: a library entry whose file has not changed is no longer
      re-opened on every refresh.
- [x] A default image folder in Settings, plus per-dialog folder memory.
- [x] Palette-derived colours instead of 21 hardcoded sites.
- [x] A column chooser, and shortcuts for the actions that had none.
- [x] Resize refuses a disk with differencing children, naming the child.

## Fixed in 1.7.0

- [x] A description holding a blank line *and* a following `UUID:` line made
      the parser drop the real medium (it never reached its `Location:`) and
      put a phantom in its place whose UUID carried an embedded newline —
      which then went into the next command run on that row. The 1.1.1 fix
      covered each half of the trap separately; this is where they meet. A
      `UUID:` inside a description now opens a record only once the one being
      read has a `Location`.
- [x] A looping `Parent UUID` hid media completely: a self-parent (which
      `sethdparentuuid` will stamp) or a mutual pair were both counted as
      children, so neither reached the root list and nothing drew them, while
      the totals line went on counting them. Guarding the recursion is not
      the same as drawing the row — every record now gets exactly one.
- [x] Batch convert deleted a disk it was about to convert. Existing targets
      used to be removed before the queue starts, so a plan aiming one disk's
      clone at another's backing file destroyed that disk before its own turn.
      Re-running a convert over its own output folder did precisely that with
      the default suffix.
- [x] Convert and batch convert now name the registered medium a target
      belongs to, with its UUID, before overwriting it — "Overwrite?" also
      leaves the registry pointing at a file that is no longer the medium it
      names.
- [x] `clonemedium --existing` and `mediumio cat --output=` count as
      destructive for a history replay. The list was verb-shaped; it is now
      about what a replay would overwrite.
- [x] Resize refuses a disk with differencing children and names the child,
      the guard Format as FAT already had.
- [x] An export to a name ending in `.csv`/`.json` writes that format
      whatever the filter combo was showing.
- [x] Quitting mid-command removes the password files: `QProcess.finished`
      normally triggers removal, but application teardown can skip that
      callback, so close also requests cleanup explicitly.
- [x] **M** — Hermetic end-to-end suite in Docker (`make e2e`): the real app
      against a *stateful* fake VBoxManage — a registry that persists and
      real image files — so a workflow is checked by what it changed, not by
      what it printed. The initial suite had 40 scenarios; the current suite
      has 72. Container execution needs no VirtualBox, display or network and
      is also a CI job.

## Correctness / polish

- [x] **S** — Encrypt dialog: "Check password" button *(1.4.0)*.
- [x] **S** — Persist column widths and order, sort order, the selected tab
      and window/splitter geometry in QSettings *(1.4.0)*.
- [x] **S** — Resize dialog: GB/MB unit selector and current → new delta,
      plus an exact-size mode on `--resizebyte` *(1.6.0)*. Shrinking is
      refused with a reason instead of a VirtualBox error. Note the unit
      switch has to read the spin *before* narrowing its range: setRange
      clamps first, which turned 25600 MB into 4 GB.
- [x] **S** — Convert dialog: `clonemedium --variant` and `--existing`
      *(1.6.0)*. `--existing` never goes through the overwrite path — the
      target is the image being cloned *into*, and deleting it first would
      destroy the thing the flag exists to use.
- [x] **S** — One-step encryption password change *(1.2.0)*. Two secrets on
      one stdin stream do **not** work (the first `readPasswordFile("stdin")`
      swallows the buffered read; the second fails
      `VBOX_E_PASSWORD_INCORRECT`), so it uses two 0600 temp files created at
      launch time and removed on exit.
- [x] **S** — `CaptureRunner` deadline *(1.3.0)*, 120 s. A wedged VBoxManage
      used to leave `_refreshing` set forever, silencing every later refresh.
      Reproducible: `mediumio stream` deadlocks on 7.2.12 and wedges VBoxSVC
      until it is SIGKILLed — do not build anything on that subcommand.
- [x] **S** — Re-run a command from the history panel *(1.6.0)*. Safe once
      the two ways it goes wrong are closed off: a command whose secret went
      over stdin or through a since-removed temp file is refused outright
      (it would hang or fail), and a destructive one needs the word RUN
      typed rather than a default button pressed.
- [x] **S** — Attach dialog: the device spin now comes from
      `storagecontrollertype<N>` *(1.6.0)* — only IDE (PIIX3/PIIX4/ICH6) and
      the floppy controller (I82078) address two devices per port.
- [x] **S** — The media library matched paths literally, so a `Location:`
      that VBoxManage reports differently from the stored path would leave
      Remove/Move unable to prune the entry and the medium would return on the
      next refresh. Adding and removing now match on `same_file()` (symlinks,
      `..`, and files already gone), so no second spelling is stored either.

## Delivered in 1.4.0

- [x] Remembered window/splitter/column/tab layout, per user, via QSettings.
- [x] Confirm before quitting while a command is running.
- [x] Report a configured VBoxManage path that is not executable, instead of
      letting every command fail with a bare "Failed to start".
- [x] History records the command that failed to launch, not the previous one.
- [x] "Check current password" button (`checkmediumpwd`).
- [x] Confirm before hex-dumping more than a megabyte into the output panel.

## Fixed in 1.3.1

- [x] `Access Error:` folded into `State` (a regression from the 1.1.1 parser
      rewrite): three-line State cell, no red highlight for inaccessible
      media, and Compact-all no longer skipping them. Now its own field, and
      state comparisons go through `state_word()`.
- [x] Remove refuses a medium with differencing children instead of letting
      `closemedium` fail with `VBOX_E_OBJECT_IN_USE`.
- [x] A long command line no longer drags the output panel's header past the
      window edge.

## Delivered in 1.3.0

- [x] Image contents viewer/extractor (`mediumio cat`), encrypted media
      included — hex dump or extract a byte range with no VM and no mount.
- [x] Create differencing images (`createmedium --diffparent`).
- [x] Format a medium as FAT (`mediumio formatfat`) and create pre-formatted
      floppy images (`--variant Formatted`, which VDI rejects). Refused on a
      parent, which VirtualBox treats as read-only.
- [x] Formats, variants and extensions from `list hddbackends` rather than
      hardcoded — adds QCOW/QED/Parallels, keeps read-only formats out.
- [x] Immutable `--autoreset`, and a plain explanation instead of a COM error
      when a differencing image's type cannot be changed.
- [x] Attach flags: `--mtype`, `--discard`, `--nonrotational`,
      `--hotpluggable`.
- [x] `CaptureRunner` deadline (was the open item below).

## Delivered in 1.2.0

- [x] Resolve an inaccessible medium — `modifymedium --setlocation` for a file
      that moved, `internalcommands repairhd` (dry run first) for a damaged
      image. Keys off the filesystem, not the VBoxSVC-cached `State:`.
- [x] Image UUID tools — `internalcommands sethduuid` / `sethdparentuuid`, the
      way past "a medium with the same UUID already exists" after copying a
      `.vdi`; warns before desynchronising a registered image.
- [x] Encryption column (cipher + password ID) in the listing.
- [x] Per-tab totals: provisioned versus allocated.
- [x] Command history with exit codes, plus Copy command.

## Delivered in 1.6.0

- [x] Multi-selection, and batch compact / remove / detach / convert.
- [x] Media health report, with an optional read-only `repairhd -dry-run` pass.
- [x] Scan for unregistered images; `mediumproperty` editor; snapshot column.
- [x] Export the listing to CSV/JSON, and copy rows as TSV.
- [x] Drag and drop images onto the window.
- [x] Free-space preflight before create, resize and clone.
- [x] Host drives, `emptydrive` and the Guest Additions ISO in the attach
      dialog, with `--passthrough` / `--tempeject` / `--forceunmount`.
- [x] Compact reports the space it actually reclaimed.

## Features

- [x] **M** — Orphan scan *(1.6.0)*, comparing real paths so a symlinked VM
      folder does not report every disk in it as an orphan.
- [x] **M** — Multi-select: batch compact / remove / detach / convert
      *(1.6.0)*. The queue entries grew a per-command follow-up — a single
      shared pending slot cannot work for a batch, because it would be
      claimed by whichever command happened to finish.
- [x] **M** — Snapshot column *(1.6.0)*. No `snapshot list` call needed: the
      name is already in the `[snap (UUID: …)]` suffix.
- [x] **S** — `mediumproperty` viewer/editor *(1.6.0)*. Verified on 7.2.12:
      `set` exits 0 on a create-time property (VDI's `AllocationBlockSize`)
      and the value stays as it was, so the dialog says so rather than
      leaving the user to wonder why nothing changed. The schema lookup has
      to upper-case the format — `list -l` reports `vdi`, backend ids are
      `VDI`.
- [x] **L** — VM storage view with controller/slot trees and attach/detach
      actions. Slot occupants are rechecked before issuing commands.
- [x] **M** — Refresh cost *(1.9.0)*: unchanged library probes are cached by
      mtime and size. Missing registry entries invalidate the cached stamps
      so the next refresh reopens them. The chain stays sequential.
- [x] **S** — Elapsed time and ETA *(1.9.0)*, plus a window alert and verdict
      when an operation running for more than half a minute finishes.
- [x] **M** — Split the application into `vboxfront_app` domain, command,
      persistence, service, process, widget and dialog modules, retaining the
      entry-point import facade. The refactor passed the full baseline suites.

## Remaining work from the 2026-10-18 proposals

The original feature proposals are delivered. Registry snapshots live in the
per-user data directory. Long operations use a window alert; native desktop
notifications remain a possible enhancement.

## Delivered in 1.11.0 (2026-10-24)

- [x] Compare contents (`mediumio cat` hashes), and verify after convert/flatten.
- [x] Flatten a differencing chain into one standalone image.
- [x] Disk space report: by VM, folder and chain, trash, compact candidates.
- [x] Auto-refresh when VirtualBox.xml changes outside the window.
- [x] Headless CLI: `list`, `health`, `space`, `compact-all`, `verify`.
- [x] Read-only browsing through `vboximg-mount -g`.
- [x] Golden image: multiattach base attached to several VMs.
- [x] Opt-in trash purge by age or size.
- [x] Undo last change for Move, Resolve re-point and Properties.
- [x] Library entries match by file, not spelling.
- [x] Own icon; Windows and macOS builds in CI.

## Delivered in 1.10.0 (2026-10-22)

- [x] Recoverable overwrite targets for file-producing clone/import/export.
- [x] Trash manager with selected restore/delete and persistent batch undo.
- [x] Failed/interrupted operation recovery with partial files and guarded retry.
- [x] Background archive measurement, cancellation and source-signature cache.
- [x] VM/controller/slot storage view with direct attach/detach actions.
- [x] Read-only registry snapshot comparison.
- [x] Named saved filters with apply/replace/delete controls.
- [x] Batch outcome summary with guarded retry of selected failures.

Still open: whole-image backup for multi-file formats, rollback for in-place
`--existing` clones, and persistent batch outcomes (file failures are persistent;
the complete batch report lasts for the window session).

## Packaging / project

- [x] **S** — Ship an own icon (`vboxfront_app/icon.svg`): window, `.desktop`,
      `.deb` hicolor theme, and PyInstaller ico/icns via
      `scripts/render_icon.py` *(1.11.0)*.
- [x] **M** — CI matrix: Windows and macOS PyInstaller builds *(1.11.0;
      not yet seen green — first run pending)* (spec already
      has the Darwin BUNDLE branch; VBoxManage default paths differ —
      the Settings override covers it).
- [ ] **S** — Lintian-clean pass over the `.deb` (changelog is gzipped and
      md5sums present; verify current warnings).

- [ ] **M** — Host raw-disk access (`createmedium --variant RawDisk` with the
      `RawDrive`/`Partitions` properties). Considered and declined for now:
      partition detail needs privileges the app does not have
      (`list hostdrives` returns E_ACCESSDENIED here), and a GUI button that
      points a VM at `/dev/nvme0n1` is a foot-gun that wants more thought than
      a dialog.

## Non-goals (for now)

- Talking to the VirtualBox API/SDK directly — would break the
  "every action is a visible VBoxManage command" ethos.
- VM lifecycle management (start/stop/snapshot) — this is a *media* tool;
  the VirtualBox GUI does VMs well.
- i18n.
