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
| Dialogs | `CreateDiskDialog`, `ResizeDialog`, `ConvertDialog`, `RemoveDialog`, `PropertiesDialog`, `EncryptDialog`, `AttachDialog`, `SettingsDialog` — all plain `QDialog` |
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
    `started` and closes the write channel. Used for encryption passwords
    (`--newpassword -` / `--oldpassword -`) so secrets never appear in argv,
    the process list, or the echoed command line.
- **`CaptureRunner`** is a one-shot buffered runner for *read-only*
  commands (`list`, `showvminfo`, refresh-time `showmediuminfo`). It may run
  while `CommandRunner` is busy; both being read-only or independent keeps
  that safe.

**Refresh chain.** A refresh is a list of sequential capture steps: first
`showmediuminfo <kind> <path>` for every library entry (see below), then
`list -l hdds|dvds|floppies`, then populate all panes at once. A `_refreshing`
flag coalesces re-entrant refresh requests (`_refresh_pending`).

**Command queue.** Batch operations (Compact all VDIs) push
`(args, stdin)` tuples onto `_cmd_queue`; `_on_runner_finished` pops the next
on success and *drops the whole queue* on the first failure — a failed
compact usually means something systemic (locked medium, missing binary),
and silently continuing would hide it.

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

- **`list -l` blocks** are separated by blank lines; keys are `Key: value`
  at column 0. Values may wrap onto **indented continuation lines** — seen
  with multi-VM `In use by VMs:` and multi-property `Property:` — which are
  appended to the previous key. `Storage format` is preferred over the
  historical `Format` key. `Size on disk`, `In use by VMs` and `Parent UUID`
  exist only in `-l` output (short `list hdds` has no size-on-disk at all).
- **`showvminfo --machinereadable`** is `key=value` with optional quotes on
  either side. Storage slots look like `"SATA Controller-1-0"="/path"` plus
  `"<ctl>-ImageUUID-<port>-<device>"="uuid"`. **Trap:** per-slot flag keys
  (`"<ctl>-nonrotational-1-0"`) have the same shape, so slot lookups must be
  prefixed with a *known controller name* (from `storagecontrollername<N>`),
  never matched with a generic `name-port-device` regex.
- **Attach/detach:** `find_attachment` locates the (controller, port,
  device) of a medium UUID; `find_free_port` picks the first port whose
  device-0 slot is `"none"`. The attach dialog fetches VM info
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
- Sorting is enabled on all columns; `MediumItem.__lt__` compares the
  numeric MB value stored in `UserRole` for the size columns, text otherwise.
  Cells show human sizes (`10.0 GB`); tooltips keep the raw `MBytes` values.
- The `Ctrl+F` filter hides rows whose *subtree* contains no match, so a
  matching child keeps its parents visible.
- Media with `State: inaccessible` are painted red; removing an attached
  medium is refused with the list of using VMs (detach first).
- Selection is remembered by UUID across refreshes so chained operations
  keep their target.

## Security

- No shell: argv lists only, `shlex.quote` used solely for *display*.
- Encryption passwords travel via stdin (`-`), never argv (see above); the
  echoed command line shows the literal `-` placeholder.
- The Remove dialog defaults to keep-file; `--delete` requires a second
  explicit confirmation.

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
`VERSION` comes from `git describe`. PyInstaller does not cross-compile —
binaries are host-arch only.

## Known limitations

| Limitation | Why / workaround |
|---|---|
| One VBoxManage command at a time | Serialised by design for transparency; batch compact queues sequentially |
| Change encryption password needs two steps | Two stdin secrets in one `encryptmedium` call is untested territory — decrypt, then encrypt |
| No host DVD drive attach | Only image files are managed; use the VirtualBox GUI for host drives |
| Library entries are paths, not UUIDs | A moved-outside-the-app file shows as a `[library] could not open` note; prune in Settings |
| `list -l` refresh cost | One process per library entry + 3 list calls; fine for typical registries |
