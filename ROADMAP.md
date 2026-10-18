# VBoxFront — roadmap

Open, prioritized backlog. Effort: **S** (hours), **M** (a day-ish),
**L** (multi-day). The 2026-07-26 batch implemented the original
high-value + nice-to-have tiers (media tabs, diff-chain tree, create,
attach/detach, properties/move/encryption, progress bar, filter/sort/context
menu, batch compact, media library, settings, tests + CI) — what follows is
what's left or new.

## Delivered in 1.9.0

Both "Suggested next" blocks below, less the package split — deliberately
left for its own pass, since it would churn every file the features touch.
The trash and undo of 1.8.0 came from the first block too.

- [x] Import a compressed raw image without unpacking it
      (`convertfromraw stdin`): gzip, bzip2 and xz, with the byte count read
      from the gzip trailer where there is one and measured in one streaming
      pass otherwise.
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
      are all removed before the queue starts, so a plan aiming one disk's
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
      is what triggers the shred and it never arrives when the process is
      killed on the way out.
- [x] **M** — Hermetic end-to-end suite in Docker (`make e2e`): the real app
      against a *stateful* fake VBoxManage — a registry that persists and
      real image files — so a workflow is checked by what it changed, not by
      what it printed. 40 scenarios, no VirtualBox, no display, no network;
      also a CI job.

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
      `dialogs`, `ui`). At ~5k lines it is past the edge of comfortable, and
      every feature round makes the next one harder to place.

## Suggested next (proposed 2026-10-18, after the 1.7.0 review)

Ordered by value for the effort, with what each one is actually worth.

- [x] **S** — **Safety net for `closemedium --delete`.** Done in 1.8.0: the
      file is renamed into a trash folder (configurable, default
      `.vboxfront-trash/` beside the image) instead of being unlinked, and
      *Media → Undo delete* puts the last delete back — a batch as one batch
      — for the rest of the session. `--delete` is only sent when the trash
      is turned off in Settings. Still open: emptying the trash from inside
      the app, and an undo that survives a restart.
- [ ] **S** — **Default image folder in Settings.** Create disk, Create
      floppy and Create differencing all start at `~/new-disk.vdi`, which is
      almost never where images live. Remember the last folder used per
      dialog, seeded from a configurable default (VirtualBox's own
      `machinefolder` is a good first guess).
- [ ] **S** — **Honour the palette instead of hardcoded colours.** There are
      21 literal `#888` / `QColor(220, 80, 80)` / `#c05000` sites. On a dark
      theme the greys are washed out and the inline warnings are hard to
      read. Derive them from `QPalette` once and reuse, so the window matches
      whatever the user runs.
- [ ] **S** — **A visible queue.** A batch of 30 compacts is one status-bar
      number today; nothing says what is left or lets the rest be dropped
      without killing the one in flight. A small list with "skip the rest"
      turns a long batch from a commitment into a decision.
- [ ] **M** — **Plan mode.** Let a batch dialog print the exact commands it
      would run, to the output panel or the clipboard, without running them.
      It fits the ethos better than anything else here — every action is
      already a visible VBoxManage command, and this makes the list portable
      to a script or a change ticket.
- [ ] **S** — **Structured filter.** `fmt:vdi`, `size:>10G`, `vm:alpha`,
      `state:inaccessible` alongside the current substring match. The columns
      to filter on already exist; the registries where filtering matters are
      exactly the ones where a substring is not enough.
- [ ] **M** — **Registry drift detection.** VBoxFront is not the only thing
      that writes the registry; the VirtualBox GUI, a VM start, or another
      copy of this app can change it under an open window. Hash the listing
      on each refresh and say so when it moved without an action of ours,
      rather than letting a stale row target the wrong medium.
- [ ] **S** — **Explain a snapshot-only attachment.** A medium attached only
      inside a snapshot is listed as in use, but `storageattach` cannot
      detach it — the slot is not in the current state, so the batch reports
      "could not locate" and leaves the user guessing. The Snapshot column
      already knows; say "attached in snapshot X, which only deleting that
      snapshot can undo".
- [ ] **S** — **Elapsed time and ETA on the progress bar**, plus a desktop
      notification when something that ran over ~30 s finishes. (Carried over
      from the earlier backlog; a clone of a large fixed image is a coffee
      break, and the window gives no sense of how long is left.)
- [ ] **M** — **Faster refresh** (carried over). The chain is strictly
      sequential: one `showmediuminfo` per library entry plus three `list -l`
      calls. Cache the library probes by mtime, or overlap them — they are
      read-only, and a large library makes every F5 and every post-command
      refresh visibly slow.
- [ ] **M** — **Split the module into a package** (carried over, and now
      overdue at ~5.2k lines). `parsers`, `args`, `dialogs`, `ui`. Each round
      makes the next one harder to place, and the e2e suite now gives the
      cover to do it safely.

## Suggested next, round two (proposed 2026-10-18)

Where the first block was mostly safety and polish, this one is about reach:
things VBoxManage can already do that the app never offers, and places where
a feature stops one step short of the job.

- [ ] **S** — **Import a compressed raw image without unpacking it first.**
      `convertfromraw stdin <out> <bytes>` is in 7.2.12's usage and the app
      has never used it: today a 40 GB `.img.gz` has to be extracted to a
      40 GB temp file before Import RAW will look at it. Piping
      `gunzip -c`/`xz -d`/`zstd -d` into the child's stdin needs the byte
      count up front, which `gzip -l` or the archive header gives. The stdin
      plumbing already exists for passwords.
- [ ] **S** — **Export to a flat raw image.** `clonemedium` takes
      `--format RAW` (confirmed in its usage line), but the format combos are
      built from `disk_backends()`, which drops RAW because its backend
      registers no HardDisk extension — so the one direction the app cannot
      go is *out*. Import RAW has existed since 1.1.0 with no counterpart. A
      separate "Export to raw image…" action that bypasses the backend list
      is the asymmetry fixed; it is what feeds `dd`, a USB stick or qemu.
- [ ] **S** — **Let the health dialog clean up after itself.** It already
      finds registry entries whose backing file is gone — the most common
      way a registry rots — and can only point at them. One guided
      "unregister the N entries whose file is missing" pass (plain
      `closemedium`, nothing deleted, since the file is gone already) turns
      the report into a fix. The batch queue and the blocked/removable
      sorting from Remove are both already there.
- [ ] **M** — **Flag disks attached without `--discard`.** This is why a
      compact so often gives nothing back: without it the guest's TRIM never
      reaches the image, so freed blocks stay allocated forever. The attach
      dialog offers the flag and nothing ever reports its absence.
      `showvminfo --machinereadable` already prints
      `"<ctl>-discard-<port>-<device>"="off"` per slot; the cost is that the
      health pass would have to read the VMs, not just the registry.
- [ ] **S** — **A details pane for the selected medium.** `Info` shells out
      to `showmediuminfo` and dumps text into the output panel, yet every
      field is already parsed into the `MediumRecord` on screen — description,
      properties, the full in-use list, access error, parent. A side panel
      costs no command and stops the output panel being used as a viewer.
- [ ] **M** — **Multi-select for Attach and Properties.** 1.6.0 took compact,
      remove, detach and convert multi-select and left these two behind, so
      filling a VM's ports or setting `multiattach` on six disks is still one
      dialog at a time. Attach needs a port-allocation loop over the free
      slots; Properties needs the dialog to show what the selection has in
      common and only send what was changed.
- [ ] **S** — **Export selected history as a shell script.** The history
      already holds every command verbatim with its exit code, and "every
      action is a visible VBoxManage command" is the whole ethos — one more
      step makes a session reproducible somewhere without a GUI, or pasteable
      into a change ticket. Pairs with plan mode above.
- [ ] **S** — **Tell the orphan scan why an image cannot be registered.** It
      finds unregistered images and adds them to the library, but the common
      reason one is unregistered is that its UUID collides with an image that
      already is — a copied `.vdi`. The scan could read each candidate's UUID
      (`showmediuminfo <path>`, which it effectively does anyway) and say
      "collides with X; Image UUID tools can stamp it", which is the exact
      fix the app already ships and nothing currently points at.
- [ ] **S** — **Snapshot the registry before a destructive batch.** The CSV
      /JSON export already serialises the whole listing. Writing one
      automatically beside the trash before a batch remove or batch convert
      costs nothing and is the difference between "something went wrong" and
      "here is what it looked like". Complements the 1.8.0 trash.
- [ ] **S** — **Column chooser and more shortcuts.** Eleven columns are
      always shown, which is a horizontal scroll on a laptop, and only six
      actions have keys (no compact, resize, attach or detach). Both are
      small, and the header state is already persisted.

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
