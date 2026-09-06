# VBoxFront — roadmap

Open, prioritized backlog. Effort: **S** (hours), **M** (a day-ish),
**L** (multi-day). The 2026-07-26 batch implemented the original
high-value + nice-to-have tiers (media tabs, diff-chain tree, create,
attach/detach, properties/move/encryption, progress bar, filter/sort/context
menu, batch compact, media library, settings, tests + CI) — what follows is
what's left or new.

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
      launch time and shredded on exit.
- [x] **S** — `CaptureRunner` deadline *(1.3.0)*, 120 s. A wedged VBoxManage
      used to leave `_refreshing` set forever, silencing every later refresh.
      Reproducible: `mediumio stream` deadlocks on 7.2.12 and wedges VBoxSVC
      until it is SIGKILLed — do not build anything on that subcommand.
- [x] **S** — Re-run a command from the history panel *(1.6.0)*. Safe once
      the two ways it goes wrong are closed off: a command whose secret went
      over stdin or through a since-shredded temp file is refused outright
      (it would hang or fail), and a destructive one needs the word RUN
      typed rather than a default button pressed.
- [x] **S** — Attach dialog: the device spin now comes from
      `storagecontrollertype<N>` *(1.6.0)* — only IDE (PIIX3/PIIX4/ICH6) and
      the floppy controller (I82078) address two devices per port.
- [ ] **S** — The media library matches paths literally, so a `Location:`
      that VBoxManage reports differently from the stored path would leave
      Remove/Move unable to prune the entry and the medium would return on the
      next refresh. Not reproducible on 7.2.12 (`RTPathAbs` keeps symlinks and
      QFileDialog hands over clean absolute paths) — match on `same_file()` if
      it ever shows up.

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
- [ ] **L** — Optional VM panel: list VMs with their storage trees, as an
      alternate root view (media-per-VM instead of VM-per-media). Most of the
      parsing exists (`vm_storage_controllers`, `controller_types`,
      `find_attachment`) — what is missing is the pane and the action routing.
- [ ] **M** — Refresh cost: the chain is strictly sequential, one
      `showmediuminfo` process per library entry plus three `list -l` calls.
      Fine for a small library, visibly slow for a large one; the library
      probes are read-only and could overlap, or be cached by mtime.
- [ ] **S** — Elapsed time and an ETA on the progress bar, and a notification
      when an operation that ran for more than half a minute finishes.
- [ ] **M** — Split the single module into a package (`parsers`, `args`,
      `dialogs`, `ui`). At ~4k lines it is at the edge of comfortable, and
      every feature round makes the next one harder to place.

## Packaging / project

- [ ] **S** — Ship an own icon (the `.desktop` currently borrows
      `Icon=virtualbox`); wire it into PyInstaller spec (ico/icns) too.
- [ ] **M** — CI matrix: Windows and macOS PyInstaller builds (spec already
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
