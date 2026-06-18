# VBoxFront

A small PyQt6 desktop frontend for `VBoxManage` disk-image operations.

VirtualBox ships a powerful but verbose CLI for managing virtual disks.
VBoxFront wraps the disk-related subcommands in a single window so you can
list, inspect, compact, resize, clone/convert and import disk images without
memorising `VBoxManage` invocations. Every command is shown verbatim and its
output is streamed live, so it stays transparent about what it runs.

## Features

- **Disk registry view** — lists all media known to VirtualBox
  (`VBoxManage list hdds`) with format, capacity, on-disk size, location and
  UUID.
- **Add existing image** — register a `.vdi/.vmdk/.vhd/.img` file
  (`openmedium`).
- **Compact** — reclaim unused space (`modifymedium --compact`), with a
  warning for non-VDI formats.
- **Resize** — grow a disk to a new size in MiB (`modifymedium --resize`).
- **Convert / Clone** — clone to a different format
  (`clonemedium --format`).
- **Import RAW** — convert a raw `.img/.raw/.bin` into a managed format
  (`convertfromraw`); the result is automatically registered.
- **Remove** — unregister a disk (`closemedium`), optionally deleting the
  backing file (`--delete`); defaults to the safe keep-file action.
- **Info** — full medium details (`showmediuminfo`).
- Live command output panel with a **Cancel** button; the disk list refreshes
  automatically after each operation.

## Requirements

- **Runtime:** [VirtualBox](https://www.virtualbox.org/) must be installed and
  `VBoxManage` must be on `PATH`. The app warns at startup if it is missing.
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

## Building

The `Makefile` is a thin wrapper over the scripts in `build/`.

```sh
make            # build a standalone binary -> dist/vboxfront
make deb        # build a Debian package    -> dist/vboxfront_<ver>_<arch>.deb
make run        # run from source (sets up a local .venv)
make clean      # remove build/, dist/, .venv and PyInstaller caches
make help       # list all targets
```

The binary is produced with PyInstaller using `vboxfront.spec` (onefile,
windowed, Qt WebEngine / 3D / QML excluded to keep it small). Builds happen
inside a project-local `.venv` so they never touch system Python packages.

PyInstaller cannot cross-compile, so the binary and `.deb` are always built
for the **host architecture**.

### Versioning

`VERSION` defaults to `git describe` and falls back to `1.0.0` outside a git
checkout. Override it explicitly:

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
vboxfront.py        Single-file PyQt6 application
vboxfront.spec      PyInstaller spec (onefile, windowed)
requirements.txt    Runtime Python dependency (PyQt6)
build.py            Legacy ad-hoc PyInstaller invocation (kept for reference)
Makefile              Wrapper over scripts/*.sh
scripts/build.sh      PyInstaller build (provisions a local .venv)
scripts/build-deb.sh  Assembles the .deb from the built binary
scripts/deb/DEBIAN/   control.tpl + desktop entry template
build/                PyInstaller workdir (gitignored)
```

## Notes & limitations

- All operations are exactly the `VBoxManage` subcommands shown in the output
  panel — nothing is done behind your back.
- Long operations (large compact/clone) block further actions until they
  finish; use **Cancel** to abort.
- Compacting only meaningfully shrinks VDI images; other formats prompt for
  confirmation.
