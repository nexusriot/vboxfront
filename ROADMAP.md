# VBoxFront — roadmap

Open, prioritized backlog. Effort: **S** (hours), **M** (a day-ish),
**L** (multi-day). The 2026-07-26 batch implemented the original
high-value + nice-to-have tiers (media tabs, diff-chain tree, create,
attach/detach, properties/move/encryption, progress bar, filter/sort/context
menu, batch compact, media library, settings, tests + CI) — what follows is
what's left or new.

## Correctness / polish

- [ ] **S** — Encrypt dialog: "Check password" button (`checkmediumpwd`;
      the arg builder already exists and is tested).
- [ ] **S** — Persist column widths, sort order and window/splitter geometry
      in QSettings.
- [ ] **S** — Resize dialog: GB/MB unit selector and a target-size sanity
      hint (current → new delta).
- [ ] **S** — Convert dialog: expose `clonemedium --variant Fixed` and
      `--existing` (clone into a pre-created target).
- [ ] **M** — One-step encryption password change (`--oldpassword - --newpassword -`
      with two stdin lines; needs empirical verification of read order).

## Features

- [ ] **M** — Orphan scan: walk user-chosen folders for `*.vdi/vmdk/vhd/iso`
      not in the registry/library and offer bulk-adding them.
- [ ] **M** — Multi-select: batch remove / batch compact of a selection
      (the sequential queue infrastructure already exists).
- [ ] **M** — Snapshot context: show which snapshot an attachment belongs to
      (the `[snap (UUID: …)]` suffix is parsed but not displayed).
- [ ] **S** — `mediumproperty` viewer (AllocationBlockSize etc. are already
      captured into the record's raw block; show in Info/Properties).
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

## Non-goals (for now)

- Talking to the VirtualBox API/SDK directly — would break the
  "every action is a visible VBoxManage command" ethos.
- VM lifecycle management (start/stop/snapshot) — this is a *media* tool;
  the VirtualBox GUI does VMs well.
- i18n.
