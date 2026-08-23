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
- [ ] **S** — Resize dialog: GB/MB unit selector and a target-size sanity
      hint (current → new delta); `--resizebyte` allows exact sizes, and
      `parse_byte_size()` already accepts hex and K/M/G suffixes.
- [ ] **S** — Convert dialog: expose `clonemedium --variant` and `--existing`
      (clone into a pre-created target). The per-format variant list is
      already available from `list hddbackends`.
- [x] **S** — One-step encryption password change *(1.2.0)*. Two secrets on
      one stdin stream do **not** work (the first `readPasswordFile("stdin")`
      swallows the buffered read; the second fails
      `VBOX_E_PASSWORD_INCORRECT`), so it uses two 0600 temp files created at
      launch time and shredded on exit.
- [x] **S** — `CaptureRunner` deadline *(1.3.0)*, 120 s. A wedged VBoxManage
      used to leave `_refreshing` set forever, silencing every later refresh.
      Reproducible: `mediumio stream` deadlocks on 7.2.12 and wedges VBoxSVC
      until it is SIGKILLed — do not build anything on that subcommand.
- [ ] **S** — Re-run a command from the history panel. Deliberately left out
      of 1.2.0: replaying `closemedium --delete` from a list is too easy to do
      by accident, so history only offers Copy for now.
- [ ] **S** — Attach dialog: the device spin is always 0–1 regardless of
      controller type (SATA only has device 0), so an impossible slot is only
      caught by VBoxManage's error. Derive the range from
      `storagecontrollertype<N>`.
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

## Features

- [ ] **M** — Orphan scan: walk user-chosen folders for `*.vdi/vmdk/vhd/iso`
      not in the registry/library and offer bulk-adding them.
- [ ] **M** — Multi-select: batch remove / batch compact of a selection
      (the sequential queue infrastructure already exists).
- [ ] **M** — Snapshot context: show which snapshot an attachment belongs to
      (the `[snap (UUID: …)]` suffix is parsed but not displayed).
- [ ] **S** — `mediumproperty` viewer/editor. Confirmed read/write on 7.2.12
      (`mediumproperty [disk|dvd|floppy] get|set <medium> <name> [<value>]`),
      the values are already captured into the record's raw block, and
      `MediumBackend.properties` now supplies each format's schema (name,
      type, default) to build the form from.
- [ ] **L** — Optional VM panel: list VMs with their storage trees, as an
      alternate root view (media-per-VM instead of VM-per-media).

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
