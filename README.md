# VBoxFront

A small PyQt6 desktop frontend for `VBoxManage` media operations.

VirtualBox ships a powerful but verbose CLI for managing virtual disks.
VBoxFront wraps the media-related subcommands in a single window so you can
list, inspect, create, compact, resize, clone/convert, import, encrypt,
attach and detach disk images without memorising `VBoxManage` invocations.
Every command is shown verbatim and its output is streamed live, so it stays
transparent about what it runs.

Architecture and design decisions are documented in [DESIGN.md](DESIGN.md);
the open backlog lives in [ROADMAP.md](ROADMAP.md).

## Features

- **Media tabs** — hard disks, DVD images and floppy images
  (`VBoxManage list -l hdds|dvds|floppies`), each with format, capacity,
  size on disk, type, state, attached VMs, location and UUID.
- **Diff-chain tree** — differencing images are nested under their parent
  (snapshot chains at a glance).
- **Table polish** — sortable columns (sizes sort numerically),
  human-readable sizes (`10.0 GB`), a live filter box (`Ctrl+F`),
  tooltips with the raw values, and red highlighting for *inaccessible*
  media.
- **Create disk** — `createmedium` with format, variant and size, both taken
  from what VirtualBox reports it can create (see *Formats from VirtualBox*).
- **Formats from VirtualBox** — the create and convert dialogs ask
  `list hddbackends` which formats exist, which can be created, and which
  variants each accepts, instead of hardcoding a list. QCOW, QED and
  Parallels appear where they apply; read-only formats (DMG, VHDX) are never
  offered as a target.
- **Attach / Detach** — `storageattach` against any registered VM, with
  optional `--mtype`, `--discard` (guest TRIM, which is what makes compaction
  pay off), `--nonrotational` and `--hotpluggable`; the
  attach dialog reads the VM's controllers via
  `showvminfo --machinereadable`, pre-selects the first free port, and marks
  running VMs. Detach locates the right controller/port automatically.
- **In-use guard** — media attached to a VM show the VM names in the list;
  removing them is blocked until they are detached.
- **Compact** — reclaim unused space (`modifymedium --compact`), single disk
  or **all VDIs in one queued batch**.
- **Resize** — grow a disk (`modifymedium --resize`, MB).
- **Convert / Clone** — clone to a different format (`clonemedium --format`).
- **Import RAW** — convert a raw `.img/.raw/.bin` into a managed format
  (`convertfromraw`); the result lands in the media library automatically.
- **Properties** — change medium type
  (normal/writethrough/immutable/shareable/readonly/multiattach), the
  immutable auto-reset flag and the description
  (`modifymedium --type/--autoreset/--description`). A differencing image's
  type cannot be changed, and the dialog says so rather than letting
  VirtualBox refuse it.
- **Move** — relocate a disk file on disk (`modifymedium --move`).
- **Password check** — verify a medium's current password without changing
  anything (`checkmediumpwd`).
- **Encryption** — set, change or remove disk encryption (`encryptmedium`,
  requires the VirtualBox Extension Pack). One password goes via stdin
  (`--newpassword stdin`); changing a password needs two secrets in one call,
  which stdin cannot carry, so those go through 0600 temp files that are
  removed the moment the command exits. Neither ever reaches the command
  line. The cipher and password ID of encrypted media are shown in the
  listing's Encryption column.
- **Remove** — unregister (`closemedium`), optionally deleting the backing
  file (`--delete`); defaults to the safe keep-file action.
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
  or extract it to a file. Goes through VirtualBox's own backend stack, so it
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
  straight from the output panel.
- **Progress bar** — long operations (clone, compact, create) report live
  percentage parsed from VBoxManage's `0%...10%...` stream.
- **Context menu** — copy UUID / location, open the containing folder, and
  all per-medium actions on right-click.
- **Remembered layout** — window size and position (including which screen
  and whether it was maximised), the output-panel splitter, the selected tab,
  and each tab's column widths and order all come back next time. Columns are
  resizable and reorderable, and auto-fit once on a first run.
- **Settings** — custom `VBoxManage` location (useful when it is not on
  `PATH`) and management of the media library.
- Live command output panel with a **Cancel** button; the media list
  refreshes automatically after each operation.

### The media library (modern VirtualBox note)

Since VirtualBox dropped `VBoxManage openmedium`, there is no CLI way to
*persistently* register a standalone image: `showmediuminfo` opens it only
until the VBoxSVC daemon exits (a few seconds after the last client), and
only `createmedium` or attaching to a VM persist across restarts. VBoxFront
therefore keeps its own **library** of known image paths (in `QSettings`);
**Add file…** stores the path and every refresh re-opens library entries so
unattached images stay visible. Entries are dropped again when you remove
the medium (and can be managed in *File → Settings*).

## Requirements

- **Runtime:** [VirtualBox](https://www.virtualbox.org/) must be installed
  and `VBoxManage` available — either on `PATH` or configured in
  *File → Settings*. The app warns at startup if it is missing.
- **From source:** Python 3.10+ and `PyQt6` (see `requirements.txt`).
- **Building a binary/.deb:** `python3`, `pip`, and `binutils`
  (`strip`); `dpkg-deb` for the Debian package. `upx` is used if present.

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
installation needed. CI (GitHub Actions) runs the suite and builds the .deb
on every push.

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

`APP_VERSION` in `vboxfront.py` is the single source of truth — it names the
window title, `vboxfront --version`, the macOS bundle version and the fallback
the build scripts use. `VERSION` prefers `git describe` when the checkout has
tags, so a tagged build carries its tag (and `-dirty` when the tree is not
clean). Override it explicitly:

```sh
make VERSION=1.2.3 deb
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
vboxfront.py          Single-file PyQt6 application
vboxfront.spec        PyInstaller spec (onefile, windowed)
requirements.txt      Runtime Python dependency (PyQt6)
tests/                Unit tests (stdlib unittest, offscreen Qt)
DESIGN.md             Architecture & design notes
ROADMAP.md            Open backlog
build.py              Legacy ad-hoc PyInstaller invocation (kept for reference)
Makefile              Wrapper over scripts/*.sh
scripts/build.sh      PyInstaller build / run / test (provisions a local .venv)
scripts/build-deb.sh  Assembles the .deb from the built binary
scripts/deb/          control.tpl + desktop entry + changelog
.github/workflows/    CI: tests + .deb build
build/                PyInstaller workdir (gitignored)
```

## Notes & limitations

- All operations are exactly the `VBoxManage` subcommands shown in the output
  panel — nothing is done behind your back.
- Long operations (large compact/clone) block further actions until they
  finish; use **Cancel** to abort. Batch compact queues commands one after
  another and stops on the first failure.
- Compacting only meaningfully shrinks VDI images; other formats prompt for
  confirmation.
- Type changes and detach require the medium not to be in use by a running
  VM; VBoxManage's error is shown verbatim if it refuses.
- Encryption needs the VirtualBox **Extension Pack**; changing an existing
  password in one step is not supported (decrypt, then encrypt again).
