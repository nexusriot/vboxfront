#!/usr/bin/env python3
"""VBoxFront - PyQt6 frontend for VBoxManage disk image operations."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from dataclasses import dataclass, field

from PyQt6.QtCore import (
    QByteArray,
    QDateTime,
    QProcess,
    QSettings,
    QTimer,
    Qt,
    QUrl,
    pyqtSignal,
)

from PyQt6.QtGui import QAction, QColor, QDesktopServices, QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

# Single source of truth for the release version: the window title, the
# PyInstaller spec's bundle version and the Makefile/build-script fallback all
# read it from here, so a bump only happens in one place. `git describe` still
# wins when the checkout has tags.
APP_VERSION = "1.5.0"

DISK_FORMATS = ["VDI", "VMDK", "VHD", "RAW"]
DISK_EXT = {"VDI": ".vdi", "VMDK": ".vmdk", "VHD": ".vhd", "RAW": ".img"}
DISK_VARIANTS = ["Standard", "Fixed"]
VMDK_VARIANTS = ["Standard", "Fixed", "Split2G"]
MEDIUM_TYPES = ["normal", "writethrough", "immutable", "shareable", "readonly", "multiattach"]
ENCRYPTION_CIPHERS = ["AES-XTS256-PLAIN64", "AES-XTS128-PLAIN64"]

# Where VBoxManage reads a password from for --newpassword/--oldpassword and
# checkmediumpwd. The documented "-" placeholder means "prompt on the console":
# it flips terminal echo off first, so on a pipe it dies with
# "Failed to retrieve echo setting (VERR_INVALID_FUNCTION)" before it ever
# looks at the data. The literal filename "stdin" is the other special case
# readPasswordFile() understands and it does a plain read, which is what a
# GUI-driven QProcess can actually feed. Verified against VBoxManage 7.2.12.
PASSWORD_STDIN = "stdin"

#: Above this, a `mediumio cat --hex` dump is confirmed first — the output panel
#: holds every line in memory, and 16 bytes per line adds up fast.
HEX_DUMP_WARN_BYTES = 1024 * 1024

# MediumFormatCapabilities bits from the VirtualBox SDK. Confirmed against
# `list hddbackends` on 7.2.12: VDI lacks CREATE_SPLIT2G (and has DISCARD),
# VMDK has it; DMG/VHDX have neither create bit because they are read-only;
# iSCSI has PROPERTIES|TCP but not FILE.
CAP_CREATE_FIXED = 0x002
CAP_CREATE_DYNAMIC = 0x004
CAP_CREATE_SPLIT2G = 0x008
CAP_DIFFERENCING = 0x010
CAP_FILE = 0x040

KINDS = ["disk", "dvd", "floppy"]
KIND_META = {
    "disk": {
        "list": "hdds",
        "title": "Hard disks",
        "attach_type": "hdd",
        "io_flag": "--disk",
        "filter": "Disk images (*.vdi *.vmdk *.vhd *.img *.raw);;All files (*)",
    },
    "dvd": {
        "list": "dvds",
        "title": "DVD images",
        "attach_type": "dvddrive",
        "io_flag": "--dvd",
        "filter": "DVD images (*.iso);;All files (*)",
    },
    "floppy": {
        "list": "floppies",
        "title": "Floppy images",
        "attach_type": "fdd",
        "io_flag": "--floppy",
        "filter": "Floppy images (*.img *.ima *.flp);;All files (*)",
    },
}

# Field labels of `list -l` / `showmediuminfo`, in the order VBoxManage emits
# them. Values can span several lines and the continuations are *not* reliably
# indented: a multi-line Description is printed flush left and may itself
# contain blank lines or "Key: value"-shaped text. So a line only opens a new
# field when its label is a known one that comes *later* in this order than the
# field being read, and a "UUID:" only opens a record when it cannot be part of
# a Description.
MEDIUM_KEYS = [
    "UUID", "Parent UUID", "State", "Access Error", "Description", "Type", "Auto-Reset",
    "Location", "Storage format", "Format variant", "Capacity", "Size on disk",
    "Encryption", "Cipher", "Password ID", "Property", "In use by VMs",
    "Child UUIDs",
]
MEDIUM_KEY_ORDER = {label: i for i, label in enumerate(MEDIUM_KEYS)}
# "Format" is the pre-6.x spelling of "Storage format"; same slot in the order.
MEDIUM_KEY_ORDER["Format"] = MEDIUM_KEY_ORDER["Storage format"]

COLUMNS = ["Name", "Format", "Capacity", "Size on disk", "Type", "State",
           "Encryption", "In use by", "Location", "UUID"]
(COL_NAME, COL_FORMAT, COL_CAPACITY, COL_SIZE, COL_TYPE, COL_STATE,
 COL_ENCRYPTION, COL_IN_USE, COL_LOCATION, COL_UUID) = range(len(COLUMNS))


@dataclass
class MediumRecord:
    uuid: str
    location: str
    fmt: str
    capacity: str
    size_on_disk: str
    state: str = ""
    access_error: str = ""
    medium_type: str = ""
    parent_uuid: str = ""
    variant: str = ""
    encryption: str = ""
    cipher: str = ""
    password_id: str = ""
    auto_reset: str = ""
    description: str = ""
    in_use: list[str] = field(default_factory=list)


@dataclass
class MediumBackend:
    """One entry of `VBoxManage list hddbackends`."""

    id: str
    capabilities: int = 0
    extensions: list[tuple[str, str]] = field(default_factory=list)
    properties: list[dict[str, str]] = field(default_factory=list)

    def can_create(self) -> bool:
        return bool(self.capabilities & (CAP_CREATE_DYNAMIC | CAP_CREATE_FIXED)
                    and self.capabilities & CAP_FILE)

    def extension_for(self, device: str = "HardDisk") -> str:
        for ext, dev in self.extensions:
            if dev == device:
                return "." + ext
        return ""

    def variants(self) -> list[str]:
        names = []
        if self.capabilities & CAP_CREATE_DYNAMIC:
            names.append("Standard")
        if self.capabilities & CAP_CREATE_FIXED:
            names.append("Fixed")
        if self.capabilities & CAP_CREATE_SPLIT2G:
            names.append("Split2G")
        return names or ["Standard"]


def parse_hddbackends(text: str) -> list[MediumBackend]:
    """Parse `VBoxManage list hddbackends`.

    Worth asking VirtualBox rather than hardcoding: it is the only authority on
    which formats can be *created* (DMG and VHDX are read-only — no create
    bit), which of them are hard disks at all (RAW only registers DVD and
    floppy extensions, which is why it must not appear as a disk target), and
    which variants each one accepts.
    """
    backends: list[MediumBackend] = []
    pattern = re.compile(
        r"Backend\s+\d+:\s+id='([^']*)'.*?capabilities=(0x[0-9a-fA-F]+|\d+)"
        r".*?extensions='([^']*)'\s+properties=\((.*?)\)\s*$",
        re.DOTALL | re.MULTILINE,
    )
    for match in pattern.finditer(text):
        ident, caps, extensions, props = match.groups()
        exts = []
        for chunk in extensions.split(","):
            m = re.match(r"\s*(\S+)\s*\(([^)]*)\)", chunk)
            if m:
                exts.append((m.group(1), m.group(2)))
        properties = [
            {"name": m.group(1), "type": m.group(2), "default": m.group(3)}
            for m in re.finditer(
                r"name='([^']*)'[^\n]*?type=(\w+)[^\n]*?default='([^']*)'", props
            )
        ]
        backends.append(MediumBackend(
            id=ident,
            capabilities=int(caps, 16) if caps.startswith("0x") else int(caps),
            extensions=exts,
            properties=properties,
        ))
    return backends


def disk_backends(backends: list[MediumBackend]) -> list[MediumBackend]:
    """Backends that can create a hard-disk image file, in listing order."""
    return [b for b in backends if b.can_create() and b.extension_for("HardDisk")]


def get_settings() -> QSettings:
    return QSettings("vboxfront", "vboxfront")


def vboxmanage_path() -> str | None:
    custom = get_settings().value("vboxmanage_path", "", str)
    if custom:
        return custom
    return shutil.which("VBoxManage") or shutil.which("vboxmanage")


def vboxmanage_problem() -> str:
    """Why VBoxManage cannot be run, or "" when it looks usable.

    A configured path is honoured even when it is wrong — silently falling back
    to PATH would hide the user's own setting — so the check has to look at it
    rather than just at whether *something* was found. Without this a bogus
    Settings entry passed the startup check and every command afterwards died
    with a bare "Failed to start".
    """
    custom = get_settings().value("vboxmanage_path", "", str)
    path = vboxmanage_path()
    if not path:
        return (
            "Could not find `VBoxManage` in PATH. Install VirtualBox, add it to "
            "your PATH, or set its location in File → Settings."
        )
    if custom and not (os.path.isfile(custom) and os.access(custom, os.X_OK)):
        return (
            f"The VBoxManage location set in File → Settings is not an "
            f"executable file:\n\n{custom}\n\nEvery command will fail to "
            "start until it is corrected or cleared."
        )
    return ""


def _as_str_list(value) -> list[str]:
    # QSettings returns None for missing keys and a bare str for 1-item lists.
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [str(v) for v in value]


def load_library(kind: str) -> list[str]:
    return _as_str_list(get_settings().value(f"library/{kind}"))


def save_library(kind: str, paths: list[str]) -> None:
    get_settings().setValue(f"library/{kind}", paths)


def add_library_path(kind: str, path: str) -> None:
    paths = load_library(kind)
    if path not in paths:
        paths.append(path)
        save_library(kind, paths)


def remove_library_path(kind: str, path: str) -> None:
    paths = load_library(kind)
    if path in paths:
        paths.remove(path)
        save_library(kind, paths)


def load_history() -> list[dict]:
    """Past commands, newest last. Stored as one JSON blob, not a QSettings
    list: the INI backend splits string lists on commas, which a command line
    can legitimately contain."""
    raw = get_settings().value("history", "", str)
    try:
        entries = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [e for e in entries if isinstance(e, dict) and "command" in e]


def append_history(command: str, code: int, when: str, limit: int = 200) -> None:
    entries = load_history()
    entries.append({"command": command, "exit": code, "when": when})
    get_settings().setValue("history", json.dumps(entries[-limit:]))


def clear_history() -> None:
    get_settings().remove("history")


def parse_media_list(text: str) -> list[MediumRecord]:
    """Parse `VBoxManage list [-l] hdds|dvds|floppies` output into records.

    Records are separated by a blank line and every field is a `Label: value`
    at column 0 — but neither can be taken at face value, because a multi-line
    Description is printed flush left and may contain blank lines and text that
    looks exactly like a field. See MEDIUM_KEYS for the rules that disambiguate
    it: a record opens at the `UUID:` following a blank line, a known label
    later in the field order opens a field, and anything else continues the
    field being read.
    """
    blocks: list[dict[str, str]] = []
    kv: dict[str, str] | None = None
    last_key = ""
    after_blank = True
    for line in text.splitlines():
        if not line.strip():
            # Keep blank lines that fall inside a value; the one separating two
            # records is stripped off again when the record is built.
            if kv is not None and last_key:
                kv[last_key] += "\n"
            after_blank = True
            continue
        label, sep, value = line.partition(":")
        label = label.strip()
        order = MEDIUM_KEY_ORDER.get(label, -1) if sep and line[0] not in " \t" else -1
        # A "UUID:" opens a record unless it could be part of the free-text
        # Description being read — that is the one field whose continuations
        # can look like anything. Anywhere else (leading warnings on the
        # merged stderr, say) it still starts the record it announces.
        if order == 0 and (after_blank or last_key != "Description"):
            kv = {"UUID": value.strip()}
            blocks.append(kv)
            last_key = "UUID"
        elif kv is None:
            pass  # noise ahead of the first record
        elif order > MEDIUM_KEY_ORDER[last_key]:
            kv[label] = value.strip()
            last_key = label
        elif last_key:
            kv[last_key] += "\n" + line.strip()
        after_blank = False

    records: list[MediumRecord] = []
    for block in blocks:
        kv = {k: v.strip("\n") for k, v in block.items()}
        if not kv.get("UUID") or "Location" not in kv:
            continue
        in_use = [e for e in kv.get("In use by VMs", "").split("\n") if e]
        records.append(
            MediumRecord(
                uuid=kv.get("UUID", ""),
                location=kv.get("Location", ""),
                fmt=kv.get("Storage format") or kv.get("Format", ""),
                capacity=kv.get("Capacity", ""),
                size_on_disk=kv.get("Size on disk", ""),
                state=kv.get("State", ""),
                access_error=kv.get("Access Error", ""),
                medium_type=kv.get("Type", ""),
                parent_uuid=kv.get("Parent UUID", ""),
                variant=kv.get("Format variant", ""),
                encryption=kv.get("Encryption", ""),
                cipher=kv.get("Cipher", ""),
                password_id=kv.get("Password ID", ""),
                auto_reset=kv.get("Auto-Reset", ""),
                description=kv.get("Description", ""),
                in_use=in_use,
            )
        )
    return records


def parse_capacity_mb(capacity: str) -> int | None:
    m = re.match(r"\s*(\d+)\s*MBytes", capacity or "")
    return int(m.group(1)) if m else None


def format_mbytes(mb: int | None) -> str:
    if mb is None:
        return ""
    if mb < 1024:
        return f"{mb} MB"
    gb = mb / 1024
    if gb < 1024:
        return f"{gb:.1f} GB"
    return f"{gb / 1024:.2f} TB"


def parse_in_use_entry(entry: str) -> tuple[str, str] | None:
    """'name (UUID: xxx) [snap (UUID: yyy)]' -> (name, vm_uuid)."""
    m = re.match(r"(.+?) \(UUID: ([0-9a-fA-F-]{36})\)", entry.strip())
    return (m.group(1), m.group(2)) if m else None


def parse_vms_list(text: str) -> list[tuple[str, str]]:
    """Parse `VBoxManage list vms` lines: "name" {uuid}."""
    vms = []
    for line in text.splitlines():
        m = re.match(r'"(.*)" \{([0-9a-fA-F-]{36})\}\s*$', line.strip())
        if m:
            vms.append((m.group(1), m.group(2)))
    return vms


def parse_machinereadable(text: str) -> dict[str, str]:
    """Parse `showvminfo --machinereadable` key=value lines (keys/values may be quoted)."""
    info: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if len(k) >= 2 and k[0] == '"' and k[-1] == '"':
            k = k[1:-1]
        if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
            v = v[1:-1]
        info[k] = v
    return info


def vm_storage_controllers(info: dict[str, str]) -> list[tuple[str, int]]:
    """[(controller name, port count)] from machinereadable VM info."""
    controllers = []
    i = 0
    while f"storagecontrollername{i}" in info:
        name = info[f"storagecontrollername{i}"]
        try:
            ports = int(info.get(f"storagecontrollerportcount{i}", "1"))
        except ValueError:
            ports = 1
        controllers.append((name, ports))
        i += 1
    return controllers


def find_attachment(info: dict[str, str], medium_uuid: str) -> tuple[str, int, int] | None:
    """Locate (controller, port, device) where the medium UUID is attached.

    Slot keys must be resolved via the known controller names because other
    per-slot keys ("<ctl>-nonrotational-<p>-<d>") share the same shape.
    """
    want = medium_uuid.lower()
    for ctl, _ports in vm_storage_controllers(info):
        prefix = f"{ctl}-ImageUUID-"
        for key, value in info.items():
            if not key.startswith(prefix) or value.lower() != want:
                continue
            m = re.match(r"(\d+)-(\d+)$", key[len(prefix):])
            if m:
                return (ctl, int(m.group(1)), int(m.group(2)))
    return None


def find_free_port(info: dict[str, str], controller: str, ports: int) -> int:
    """First port whose device-0 slot is empty, or 0 when the controller is full.

    Callers must not treat the fallback as free — see slot_occupant().
    """
    for p in range(ports):
        if not slot_occupant(info, controller, p, 0):
            return p
    return 0


def slot_occupant(info: dict[str, str], controller: str, port: int, device: int) -> str:
    """Medium in a controller slot, or "" when it is empty.

    storageattach happily *replaces* an occupied slot: it exits 0 and prints
    nothing, silently detaching whatever was there (verified on 7.2.12), so
    every attach has to check the slot for itself.
    """
    current = info.get(f"{controller}-{port}-{device}", "none")
    return "" if current in ("none", "") else current


def list_media_args(kind: str) -> list[str]:
    return ["list", "-l", KIND_META[kind]["list"]]


def create_disk_args(path: str, size_mb: int, fmt: str, variant: str) -> list[str]:
    args = ["createmedium", "disk", "--filename", path, "--size", str(size_mb), "--format", fmt]
    if variant and variant != "Standard":
        args += ["--variant", variant]
    return args


def attach_args(vm: str, controller: str, port: int, device: int, kind: str, medium: str,
                mtype: str = "", discard: bool = False, nonrotational: bool = False,
                hotpluggable: bool = False) -> list[str]:
    """Every optional flag is omitted unless asked for, so the echoed command
    stays the shortest one that does the job."""
    args = [
        "storageattach", vm,
        "--storagectl", controller,
        "--port", str(port),
        "--device", str(device),
        "--type", KIND_META[kind]["attach_type"],
        "--medium", medium,
    ]
    if mtype:
        args += ["--mtype", mtype]
    if discard:
        args += ["--discard", "on"]
    if nonrotational:
        args += ["--nonrotational", "on"]
    if hotpluggable:
        args += ["--hotpluggable", "on"]
    return args


def detach_args(vm: str, controller: str, port: int, device: int) -> list[str]:
    return [
        "storageattach", vm,
        "--storagectl", controller,
        "--port", str(port),
        "--device", str(device),
        "--medium", "none",
    ]


def properties_args(kind: str, uuid: str, new_type: str | None, new_description: str | None) -> list[str] | None:
    args = ["modifymedium", kind, uuid]
    if new_type:
        args += ["--type", new_type]
    if new_description is not None:
        args += ["--description", new_description]
    return args if len(args) > 3 else None


def move_args(uuid: str, new_path: str) -> list[str]:
    return ["modifymedium", "disk", uuid, "--move", new_path]


def encrypt_args(uuid: str, password: str, password_id: str, cipher: str) -> tuple[list[str], bytes]:
    # The password goes through stdin so it never appears in the process list
    # or the echoed command line. See PASSWORD_STDIN for why not "-".
    args = ["encryptmedium", uuid, "--newpassword", PASSWORD_STDIN,
            "--newpasswordid", password_id, "--cipher", cipher]
    return args, (password + "\n").encode()


def decrypt_args(uuid: str, password: str) -> tuple[list[str], bytes]:
    return ["encryptmedium", uuid, "--oldpassword", PASSWORD_STDIN], (password + "\n").encode()


def check_password_args(uuid: str, password: str) -> tuple[list[str], bytes]:
    return ["checkmediumpwd", uuid, PASSWORD_STDIN], (password + "\n").encode()


def mediumio_args(kind: str, medium: str, password: str = "") -> tuple[list[str], bytes | None]:
    """Common prefix of every `mediumio` call, plus its stdin payload.

    `--password-file=-` is the same console trap as elsewhere (it disables
    terminal echo and dies on a pipe), but the literal name `stdin` works, so
    an encrypted medium needs no temp file here.
    """
    args = ["mediumio", f"{KIND_META[kind]['io_flag']}={medium}"]
    if password:
        args.append(f"--password-file={PASSWORD_STDIN}")
        return args, (password + "\n").encode()
    return args, None


def mediumio_cat_args(kind: str, medium: str, offset: int = 0, size: int | None = None,
                      hex_dump: bool = True, output: str = "",
                      password: str = "") -> tuple[list[str], bytes | None]:
    """Read a byte range straight out of an image — no VM, no mount, no root."""
    args, stdin_data = mediumio_args(kind, medium, password)
    args.append("cat")
    if hex_dump:
        args.append("--hex")
    if offset:
        args.append(f"--offset={offset}")
    if size is not None:
        args.append(f"--size={size}")
    if output:
        args.append(f"--output={output}")
    return args, stdin_data


def mediumio_formatfat_args(kind: str, medium: str, quick: bool = True,
                            password: str = "") -> tuple[list[str], bytes | None]:
    args, stdin_data = mediumio_args(kind, medium, password)
    args.append("formatfat")
    if quick:
        args.append("--quick")
    return args, stdin_data


def create_diff_args(path: str, parent: str, fmt: str = "") -> list[str]:
    """A differencing child of `parent`; its size is inherited, never given."""
    args = ["createmedium", "disk", "--filename", path, "--diffparent", parent]
    if fmt:
        args += ["--format", fmt]
    return args


def create_floppy_args(path: str, size_mb: int, formatted: bool = True) -> list[str]:
    """`--variant Formatted` gives a FAT-formatted image straight away.

    Only floppies: VDI rejects the variant with VERR_FILE_NOT_FOUND, so a disk
    has to be formatted afterwards with `mediumio formatfat` instead.
    """
    args = ["createmedium", "floppy", "--filename", path, "--size", str(size_mb)]
    if formatted:
        args += ["--variant", "Formatted"]
    return args


def autoreset_args(uuid: str, enabled: bool) -> list[str]:
    return ["modifymedium", "disk", uuid, "--autoreset", "on" if enabled else "off"]


def setlocation_args(kind: str, uuid: str, path: str) -> list[str]:
    """Re-point a registry entry at a file that moved outside the app."""
    return ["modifymedium", kind, uuid, "--setlocation", path]


def repairhd_args(path: str, fmt: str = "", dry_run: bool = False) -> list[str]:
    args = ["internalcommands", "repairhd"]
    if dry_run:
        args.append("-dry-run")
    if fmt:
        args += ["-format", fmt.upper()]
    return args + [path]


def sethduuid_args(path: str, uuid: str = "") -> list[str]:
    """Stamp a fresh UUID into an image file so a copy of it can be registered."""
    return ["internalcommands", "sethduuid", path] + ([uuid] if uuid else [])


def sethdparentuuid_args(path: str, parent_uuid: str) -> list[str]:
    return ["internalcommands", "sethdparentuuid", path, parent_uuid]


def change_password_args(uuid: str, old_file: str, new_file: str,
                         password_id: str, cipher: str) -> list[str]:
    """Re-key an encrypted medium in one call, reading both secrets from files.

    Two secrets cannot share the stdin stream: the first read consumes the
    whole buffer, so the second one comes back wrong (VBOX_E_PASSWORD_INCORRECT
    on 7.2.12). Files are the only way to hand over both.
    """
    return ["encryptmedium", uuid,
            "--oldpassword", old_file,
            "--newpassword", new_file,
            "--newpasswordid", password_id,
            "--cipher", cipher]


def write_secret_file(secret: str) -> str:
    """Spill one password to a private temp file, for the two-secret call above.

    mkstemp creates it 0600 and the caller shreds it as soon as the command
    exits, so it is never readable by anyone else and never outlives the run.
    """
    fd, path = tempfile.mkstemp(prefix="vboxfront-pw-")
    with os.fdopen(fd, "w") as handle:
        handle.write(secret + "\n")
    return path


def parse_byte_size(text: str) -> int | None:
    """Accept 4096, 0x1000, 8K, 1.5M, 2G — offsets into an image are quoted
    every which way, and a hex offset is what a partition table hands you."""
    raw = (text or "").strip().replace("_", "")
    if not raw:
        return None
    try:
        if raw.lower().startswith("0x"):
            return int(raw, 16)
    except ValueError:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGTkmgt])?[Bb]?", raw)
    if not m:
        return None
    scale = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}
    value = float(m.group(1)) * scale.get((m.group(2) or "").lower(), 1)
    return int(value)


def is_differencing(medium: MediumRecord) -> bool:
    return "differencing" in medium.medium_type.lower()


def real_parent_uuid(medium: MediumRecord) -> str:
    """Parent UUID, with VBoxManage's literal "base" placeholder resolved to
    "no parent"."""
    return medium.parent_uuid if medium.parent_uuid not in ("", "base") else ""


def child_names(records: list[MediumRecord], uuid: str) -> list[str]:
    """Media in this listing whose parent is `uuid`.

    A medium with children is read-only — VirtualBox answers any write with
    "Write access denied: read-only" — so anything that writes has to check.
    """
    return [
        os.path.basename(r.location) or r.uuid
        for r in records
        if uuid and real_parent_uuid(r) == uuid
    ]


def is_uuid(text: str) -> bool:
    return bool(re.fullmatch(r"\{?[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\}?",
                             (text or "").strip()))


def same_file(a: str, b: str) -> bool:
    """True when two paths name the same file, symlinks and "…/.." included."""
    if not a or not b:
        return False
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.realpath(a) == os.path.realpath(b)


def medium_type_word(medium_type: str) -> str:
    return medium_type.split(" ")[0] if medium_type else ""


def state_word(medium: MediumRecord) -> str:
    """The State value alone.

    Belt and braces: `Access Error:` is in MEDIUM_KEYS so it no longer lands in
    `state`, but VBoxManage prints that block unindented and flush left, and
    anything else it adds there in future would fold in the same way. Every
    state comparison goes through here so one stray line cannot silently turn
    off the inaccessible highlight again.
    """
    return medium.state.split("\n")[0].strip()


def is_inaccessible(medium: MediumRecord) -> bool:
    return state_word(medium).lower() == "inaccessible"


def encryption_word(rec: MediumRecord) -> str:
    """What to show in the Encryption column: nothing at all when a medium is
    not encrypted, because "disabled" on every row is noise."""
    if rec.encryption in ("", "disabled"):
        return ""
    return rec.cipher or rec.encryption


def totals_line(records: list[MediumRecord], shown: int | None = None) -> str:
    """One-line summary: how much has been promised versus actually allocated.

    Deliberately *not* framed as reclaimable space — the gap between capacity
    and size on disk is thin-provisioning headroom that was never allocated,
    whereas compacting reclaims blocks that were allocated and then freed
    inside the guest, which `list -l` cannot see.
    """
    if not records:
        return "No media."
    capacity = [parse_capacity_mb(r.capacity) for r in records]
    on_disk = [parse_capacity_mb(r.size_on_disk) for r in records]
    total_cap = sum(v for v in capacity if v is not None)
    total_disk = sum(v for v in on_disk if v is not None)
    parts = [f"{len(records)} media"]
    if shown is not None and shown != len(records):
        parts[0] += f" ({shown} shown)"
    if total_cap:
        parts.append(f"{format_mbytes(total_cap)} provisioned")
    if any(v is not None for v in on_disk):
        share = f" ({100 * total_disk / total_cap:.0f}% allocated)" if total_cap else ""
        unknown = sum(1 for v in on_disk if v is None)
        tail = f", {unknown} unknown" if unknown else ""
        parts.append(f"{format_mbytes(total_disk)} on disk{share}{tail}")
    return " · ".join(parts)


class ElidingLabel(QLabel):
    """A label that elides to whatever width it is given.

    A plain QLabel demands the width of its full text, and a VBoxManage command
    line is far wider than a toolbar row — enough to drag the header past the
    window edge and squeeze itself and the buttons out of view. This one keeps
    the full text for the tooltip and re-elides itself whenever it is resized,
    so it can never drive the layout.
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def fullText(self) -> str:
        return self._full

    def setFullText(self, text: str):
        self._full = text
        self.setToolTip(text)
        self._elide()

    def setText(self, text: str):
        # Anything setting the text directly would otherwise desync _full and
        # lose its elision on the next resize.
        self.setFullText(text)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self):
        super().setText(self.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideMiddle, max(self.width(), 40)
        ))


class CommandRunner(QWidget):
    """A panel that runs VBoxManage commands and streams output."""

    finished = pyqtSignal(int)  # exit code

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc: QProcess | None = None
        self._stdin_data: bytes | None = None
        self._secret_files: list[str] = []
        self._progress_tail = ""
        self.last_command = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        self.cmd_label = ElidingLabel("Idle.")
        self.cmd_label.setStyleSheet("color:#888;")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setMaximumWidth(180)
        self.progress.setVisible(False)
        self.copy_btn = QPushButton("Copy command")
        self.copy_btn.setEnabled(False)
        self.copy_btn.setToolTip("Copy the command line to the clipboard")
        self.copy_btn.clicked.connect(self.copy_command)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel)
        header.addWidget(self.cmd_label, 1)
        header.addWidget(self.progress)
        header.addWidget(self.copy_btn)
        header.addWidget(self.cancel_btn)
        layout.addLayout(header)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("Command output will appear here…")
        layout.addWidget(self.output)

    def set_status(self, text: str):
        self.cmd_label.setFullText(text)

    def is_busy(self) -> bool:
        return self.proc is not None and self.proc.state() != QProcess.ProcessState.NotRunning

    def run(self, args: list[str], stdin_data: bytes | None = None,
            secret_files: list[str] | None = None) -> bool:
        """Launch one command. `secret_files` are shredded when it exits."""
        if self.is_busy():
            return False
        vbm = vboxmanage_path()
        if not vbm:
            # Name the command that could not be launched: `finished` fires from
            # here, and a stale last_command would file this failure against
            # whatever ran before it.
            self.last_command = " ".join(shlex.quote(a) for a in ["VBoxManage", *args])
            self.output.appendPlainText(f"$ {self.last_command}")
            self.output.appendPlainText("[error] VBoxManage not found in PATH.\n")
            self._shred_secrets(secret_files or [])
            self.finished.emit(127)
            return False
        self.proc = QProcess(self)
        self._stdin_data = stdin_data
        self._secret_files = list(secret_files or [])
        self._progress_tail = ""
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._on_output)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)
        self.proc.started.connect(self._on_started)

        pretty = " ".join(shlex.quote(a) for a in [vbm, *args])
        self.last_command = pretty
        self.copy_btn.setEnabled(True)
        self.set_status(f"Running: {pretty}")
        self.output.appendPlainText(f"$ {pretty}")
        if stdin_data:
            self.output.appendPlainText("[password sent via stdin]")
        if self._secret_files:
            self.output.appendPlainText(
                f"[{len(self._secret_files)} password file(s), mode 0600, "
                "removed when the command exits]"
            )
        self.cancel_btn.setEnabled(True)
        self.progress.setValue(0)
        self.proc.start(vbm, args)
        return True

    def cancel(self):
        if self.is_busy():
            self.output.appendPlainText("[cancelled by user]")
            self.proc.kill()

    def note(self, text: str):
        self.output.appendPlainText(text)

    def copy_command(self):
        if self.last_command:
            QApplication.clipboard().setText(self.last_command)

    def _shred_secrets(self, paths: list[str] | None = None):
        for path in (paths if paths is not None else self._secret_files):
            try:
                os.remove(path)
            except OSError:
                pass  # nothing useful to do; it is a temp file either way
        if paths is None:
            self._secret_files = []

    def _on_started(self):
        if not self.proc:
            return
        if self._stdin_data:
            self.proc.write(self._stdin_data)
            self._stdin_data = None
        # Always close it: nothing further is ever written, and a command that
        # tries to read stdin would otherwise block forever on an open pipe
        # with only Cancel to get out of it.
        self.proc.closeWriteChannel()

    def _on_output(self):
        if not self.proc:
            return
        data = bytes(self.proc.readAllStandardOutput()).decode(errors="replace")
        if not data:
            return
        self.output.moveCursor(self.output.textCursor().MoveOperation.End)
        self.output.insertPlainText(data)
        self.output.moveCursor(self.output.textCursor().MoveOperation.End)
        # VBoxManage long operations stream "0%...10%...100%" without newlines;
        # keep a small tail so a percentage split across chunks still matches.
        combined = self._progress_tail + data
        matches = re.findall(r"(\d{1,3})%", combined)
        if matches:
            pct = min(int(matches[-1]), 100)
            self.progress.setVisible(True)
            self.progress.setValue(pct)
        self._progress_tail = combined[-8:]

    def _on_error(self, error):
        # FailedToStart fires *instead of* finished, so reset state here.
        if error == QProcess.ProcessError.FailedToStart:
            self.output.appendPlainText(
                "[error] Failed to start VBoxManage (not executable or missing).\n"
            )
            self._shred_secrets()
            self._reset_ui()
            proc, self.proc = self.proc, None
            if proc:
                proc.deleteLater()
            self.finished.emit(126)

    def _on_finished(self, code: int, _status):
        self.output.appendPlainText(f"[exit {code}]\n")
        self._shred_secrets()
        self._reset_ui()
        proc, self.proc = self.proc, None
        if proc:
            proc.deleteLater()
        self.finished.emit(code)

    def _reset_ui(self):
        self.set_status("Idle.")
        self.cancel_btn.setEnabled(False)
        self.progress.setVisible(False)


class CaptureRunner(QProcess):
    """One-shot QProcess that buffers output and emits a callback.

    Deadlined, because a wedged VBoxManage otherwise strands the refresh chain
    forever: `_refreshing` stays set, every later refresh quietly folds into
    `_refresh_pending`, and the window just stops updating with nothing on
    screen to say why. VirtualBox can genuinely get into that state —
    `mediumio stream` deadlocks on 7.2.12 and takes VBoxSVC with it.
    """

    #: Generous on purpose: `list -l` walks every registered medium, and a
    #: large registry on slow storage is not a hang.
    TIMEOUT_MS = 120_000

    def __init__(self, parent, on_done):
        super().__init__(parent)
        self._buf = bytearray()
        self._on_done = on_done
        self.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.readyReadStandardOutput.connect(self._read)
        self.finished.connect(self._done)
        self.errorOccurred.connect(self._error)
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.setInterval(self.TIMEOUT_MS)
        self._deadline.timeout.connect(self._expired)
        self._deadline.start()

    def _expired(self):
        if self.state() == QProcess.ProcessState.NotRunning:
            return
        self.kill()
        on_done, self._on_done = self._on_done, None
        if on_done:
            on_done(124, "[error] VBoxManage did not answer within "
                         f"{self.TIMEOUT_MS // 1000}s; giving up on it. The "
                         "VirtualBox service may be wedged.\n")

    def _read(self):
        self._buf += bytes(self.readAllStandardOutput())

    def _done(self, code, _status):
        self._deadline.stop()
        on_done, self._on_done = self._on_done, None
        if on_done:
            on_done(code, self._buf.decode(errors="replace"))
        self.deleteLater()

    def _error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self._deadline.stop()
            on_done, self._on_done = self._on_done, None
            if on_done:
                on_done(126, "[error] Failed to start VBoxManage.\n")
            self.deleteLater()


class MediumItem(QTreeWidgetItem):
    """Tree item that sorts size columns numerically via UserRole data."""

    def __lt__(self, other):
        tree = self.treeWidget()
        col = tree.sortColumn() if tree else 0
        a = self.data(col, Qt.ItemDataRole.UserRole)
        b = other.data(col, Qt.ItemDataRole.UserRole)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return a < b
        return self.text(col).lower() < other.text(col).lower()


class MediaPane(QWidget):
    """One tab: a tree of media of a single kind (disk/dvd/floppy)."""

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.records: list[MediumRecord] = []
        self._by_uuid: dict[str, MediumRecord] = {}
        self._filter = ""
        self._columns_sized = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setSelectionBehavior(self.tree.SelectionBehavior.SelectRows)
        self.tree.setSelectionMode(self.tree.SelectionMode.SingleSelection)
        self.tree.setEditTriggers(self.tree.EditTrigger.NoEditTriggers)
        self.tree.setRootIsDecorated(kind == "disk")
        self.tree.setAllColumnsShowFocus(True)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(COL_NAME, Qt.SortOrder.AscendingOrder)
        header = self.tree.header()
        # Interactive, not ResizeToContents: the widths are the user's to set
        # (and are remembered between sessions), which ResizeToContents would
        # override on every populate. Location still takes up the slack.
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_LOCATION, QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(False)
        header.setSectionsMovable(True)
        layout.addWidget(self.tree)

        self.totals = QLabel("")
        self.totals.setStyleSheet("color:#888;")
        layout.addWidget(self.totals)

    def populate(self, records: list[MediumRecord]):
        selected = self.selected_record()
        prev_uuid = selected.uuid if selected else None

        self.records = records
        self._by_uuid = {r.uuid: r for r in records}
        self.tree.setSortingEnabled(False)
        self.tree.clear()

        # Diff-image chains: children hang under their parent when it is
        # present in the same listing; everything else is a top-level row.
        children: dict[str, list[MediumRecord]] = {}
        roots: list[MediumRecord] = []
        for r in records:
            parent = real_parent_uuid(r)
            if parent and parent in self._by_uuid:
                children.setdefault(parent, []).append(r)
            else:
                roots.append(r)

        def add(rec: MediumRecord, parent_item: QTreeWidgetItem | None, seen: set[str]):
            if rec.uuid in seen:
                return
            seen.add(rec.uuid)
            item = self._make_item(rec)
            if parent_item is None:
                self.tree.addTopLevelItem(item)
            else:
                parent_item.addChild(item)
            for child in children.get(rec.uuid, []):
                add(child, item, seen)

        seen: set[str] = set()
        for rec in roots:
            add(rec, None, seen)

        self.tree.setSortingEnabled(True)
        self.tree.expandAll()
        self.autosize_columns()
        self.apply_filter(self._filter)

        if prev_uuid:
            self.select_uuid(prev_uuid)

    def autosize_columns(self):
        """Fit the columns to their contents, once, on the first listing.

        Only a starting point: after this the widths belong to the user, and
        `mark_columns_restored()` suppresses it entirely when a saved layout is
        being applied instead.
        """
        if self._columns_sized or not self.records:
            return
        self._columns_sized = True
        for col in range(len(COLUMNS)):
            if col != COL_LOCATION:
                self.tree.resizeColumnToContents(col)

    def mark_columns_restored(self):
        self._columns_sized = True

    def _make_item(self, rec: MediumRecord) -> MediumItem:
        cap_mb = parse_capacity_mb(rec.capacity)
        size_mb = parse_capacity_mb(rec.size_on_disk)
        in_use_names = ", ".join(
            e[0] for e in (parse_in_use_entry(x) for x in rec.in_use) if e
        )
        cells = [
            os.path.basename(rec.location) or rec.uuid,
            rec.fmt,
            format_mbytes(cap_mb) or rec.capacity,
            format_mbytes(size_mb) or rec.size_on_disk,
            medium_type_word(rec.medium_type),
            state_word(rec),
            encryption_word(rec),
            in_use_names,
            rec.location,
            rec.uuid,
        ]
        item = MediumItem(cells)
        item.setData(COL_NAME, Qt.ItemDataRole.UserRole, rec.uuid)
        if cap_mb is not None:
            item.setData(COL_CAPACITY, Qt.ItemDataRole.UserRole, cap_mb)
        if size_mb is not None:
            item.setData(COL_SIZE, Qt.ItemDataRole.UserRole, size_mb)
        tooltips = [
            rec.location,
            f"{rec.fmt} ({rec.variant})" if rec.variant else rec.fmt,
            rec.capacity,
            rec.size_on_disk,
            rec.medium_type,
            "\n".join(x for x in (rec.state, rec.access_error) if x),
            f"Password ID: {rec.password_id}" if rec.password_id else rec.encryption,
            "\n".join(rec.in_use),
            rec.location,
            rec.uuid,
        ]
        for col, tip in enumerate(tooltips):
            if tip:
                item.setToolTip(col, tip)
        if is_inaccessible(rec):
            for col in range(len(COLUMNS)):
                item.setForeground(col, QColor(220, 80, 80))
        return item

    def selected_record(self) -> MediumRecord | None:
        item = self.tree.currentItem()
        if not item:
            return None
        return self._by_uuid.get(item.data(COL_NAME, Qt.ItemDataRole.UserRole) or "")

    def select_uuid(self, uuid: str):
        for item in self._iter_items():
            if item.data(COL_NAME, Qt.ItemDataRole.UserRole) == uuid:
                self.tree.setCurrentItem(item)
                return

    def _iter_items(self):
        def walk(item):
            yield item
            for i in range(item.childCount()):
                yield from walk(item.child(i))
        for i in range(self.tree.topLevelItemCount()):
            yield from walk(self.tree.topLevelItem(i))

    def apply_filter(self, text: str):
        self._filter = text
        needle = text.strip().lower()

        def visit(item) -> bool:
            self_match = not needle or any(
                needle in item.text(c).lower() for c in range(len(COLUMNS))
            )
            child_match = False
            for i in range(item.childCount()):
                if visit(item.child(i)):
                    child_match = True
            visible = self_match or child_match
            item.setHidden(not visible)
            return visible

        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))
        self._update_totals()

    def _update_totals(self):
        shown = sum(1 for item in self._iter_items() if not item.isHidden())
        self.totals.setText(totals_line(self.records, shown))


class CreateDiskDialog(QDialog):
    def __init__(self, backends: list[MediumBackend] | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create disk")
        self.path = ""
        self.size_mb = 0
        self.fmt = "VDI"
        self.variant = "Standard"
        # VirtualBox is the authority on what can be created and in which
        # variants; the hardcoded lists are only the fallback for when the
        # backend listing could not be read.
        self._backends = {b.id: b for b in disk_backends(backends or [])}

        layout = QFormLayout(self)
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(os.path.join(os.path.expanduser("~"), "new-disk.vdi"))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        layout.addRow("File:", path_row)

        self.fmt_combo = QComboBox()
        self.fmt_combo.addItems(
            list(self._backends) or [f for f in DISK_FORMATS if f != "RAW"]
        )
        preferred = "VDI" if "VDI" in self._backends or not self._backends else ""
        if preferred:
            self.fmt_combo.setCurrentText(preferred)
        self.fmt_combo.currentTextChanged.connect(self._on_format_changed)
        layout.addRow("Format:", self.fmt_combo)

        self.variant_combo = QComboBox()
        self.variant_combo.addItems(self._variants_for(self.fmt_combo.currentText()))
        layout.addRow("Variant:", self.variant_combo)

        self.spin = QSpinBox()
        self.spin.setRange(1, 4 * 1024 * 1024)  # up to 4 TB
        self.spin.setSuffix(" MB")
        self.spin.setValue(10240)
        self.spin.valueChanged.connect(self._update_human)
        self.human = QLabel()
        size_row = QHBoxLayout()
        size_row.addWidget(self.spin, 1)
        size_row.addWidget(self.human)
        layout.addRow("Size:", size_row)
        self._update_human(self.spin.value())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _update_human(self, mb: int):
        self.human.setText(format_mbytes(mb))

    def _variants_for(self, fmt: str) -> list[str]:
        backend = self._backends.get(fmt)
        if backend:
            return backend.variants()
        return VMDK_VARIANTS if fmt == "VMDK" else DISK_VARIANTS

    def _extension_for(self, fmt: str) -> str:
        backend = self._backends.get(fmt)
        if backend:
            return backend.extension_for("HardDisk") or ".img"
        return DISK_EXT.get(fmt, ".img")

    def _known_extensions(self) -> set[str]:
        known = set(DISK_EXT.values())
        known |= {b.extension_for("HardDisk") for b in self._backends.values()}
        return {e for e in known if e}

    def _on_format_changed(self, fmt: str):
        variants = self._variants_for(fmt)
        current = self.variant_combo.currentText()
        self.variant_combo.clear()
        self.variant_combo.addItems(variants)
        if current in variants:
            self.variant_combo.setCurrentText(current)
        path = self.path_edit.text()
        base, ext = os.path.splitext(path)
        if ext.lower() in self._known_extensions():
            self.path_edit.setText(base + self._extension_for(fmt))

    def _browse(self):
        fmt = self.fmt_combo.currentText()
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Disk file",
            self.path_edit.text(),
            f"{fmt} (*{self._extension_for(fmt)});;All files (*)",
        )
        if path:
            self.path_edit.setText(path)

    def _ok(self):
        path = self.path_edit.text().strip()
        if not path:
            QMessageBox.warning(self, "Missing file", "Please choose a target file.")
            return
        if os.path.exists(path):
            QMessageBox.warning(
                self, "File exists",
                "createmedium refuses to overwrite existing files; choose a new path.",
            )
            return
        self.path = path
        self.size_mb = self.spin.value()
        self.fmt = self.fmt_combo.currentText()
        self.variant = self.variant_combo.currentText()
        self.accept()

    def args(self) -> list[str]:
        return create_disk_args(self.path, self.size_mb, self.fmt, self.variant)


class ResizeDialog(QDialog):
    def __init__(self, disk: MediumRecord, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Resize disk")
        self.size_mb = 0

        layout = QFormLayout(self)
        layout.addRow(QLabel(f"<b>{os.path.basename(disk.location)}</b>"))
        layout.addRow("Current capacity:", QLabel(disk.capacity or "?"))

        self.spin = QSpinBox()
        self.spin.setRange(1, 4 * 1024 * 1024)  # up to 4 TB
        # VBoxManage --resize takes megabytes (MB), matching the "MBytes" that
        # `list hdds` reports for capacity, so label it MB to avoid MiB/MB drift.
        self.spin.setSuffix(" MB")
        self.spin.setValue(parse_capacity_mb(disk.capacity) or 10240)
        layout.addRow("New size:", self.spin)
        layout.addRow(
            QLabel("<i>Note: VBoxManage can only grow disks, not shrink them.</i>")
        )

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Resize")
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _ok(self):
        self.size_mb = self.spin.value()
        self.accept()


class ConvertDialog(QDialog):
    def __init__(self, disk: MediumRecord, backends: list[MediumBackend] | None = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Convert / Clone disk")
        self.target_format = "VDI"
        self.target_path = ""
        # The caller deletes an existing target (clonemedium refuses to
        # overwrite) — but only once the command is certain to run.
        self.overwrite = False

        layout = QFormLayout(self)
        layout.addRow(QLabel(f"Source: <b>{os.path.basename(disk.location)}</b> ({disk.fmt})"))

        self._backends = {b.id: b for b in disk_backends(backends or [])}
        self.fmt = QComboBox()
        self.fmt.addItems(
            list(self._backends) or [f for f in DISK_FORMATS if f != "RAW"]
        )
        if disk.fmt.upper() in DISK_FORMATS and disk.fmt.upper() != "RAW":
            # default to a different format than the source
            for i in range(self.fmt.count()):
                if self.fmt.itemText(i) != disk.fmt.upper():
                    self.fmt.setCurrentIndex(i)
                    break
        self.fmt.currentTextChanged.connect(self._update_default_path)
        layout.addRow("Target format:", self.fmt)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        layout.addRow("Target file:", path_row)

        self._src = disk
        self._update_default_path(self.fmt.currentText())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Convert")
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _extension_for(self, fmt: str) -> str:
        backend = self._backends.get(fmt)
        if backend:
            return backend.extension_for("HardDisk") or ".img"
        return DISK_EXT.get(fmt, ".img")

    def _update_default_path(self, fmt: str):
        base, _ = os.path.splitext(self._src.location)
        self.path_edit.setText(base + "-converted" + self._extension_for(fmt))

    def _browse(self):
        fmt = self.fmt.currentText()
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Target file",
            self.path_edit.text(),
            f"{fmt} (*{self._extension_for(fmt)});;All files (*)",
        )
        if path:
            self.path_edit.setText(path)

    def _ok(self):
        path = self.path_edit.text().strip()
        if not path:
            QMessageBox.warning(self, "Missing path", "Please choose a target file.")
            return
        if same_file(path, self._src.location):
            QMessageBox.warning(
                self, "Same file",
                "The target is the source disk. Clone it to a different file.",
            )
            return
        exists = os.path.exists(path)
        if exists:
            ret = QMessageBox.question(
                self, "Overwrite?", f"{path} already exists. Overwrite?"
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        self.overwrite = exists
        self.target_format = self.fmt.currentText()
        self.target_path = path
        self.accept()


class RemoveDialog(QDialog):
    """Unregister a medium, optionally deleting the backing file."""

    def __init__(self, medium: MediumRecord, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Remove medium")
        self.delete_file = False

        layout = QFormLayout(self)
        layout.addRow(QLabel(f"<b>{os.path.basename(medium.location)}</b>"))
        layout.addRow("Location:", QLabel(medium.location or "?"))
        layout.addRow("UUID:", QLabel(medium.uuid))

        self.mode = QComboBox()
        # Safe default first: unregister but keep the file on disk.
        self.mode.addItems(
            ["Unregister only (keep file)", "Delete file from disk"]
        )
        layout.addRow("Action:", self.mode)
        layout.addRow(
            QLabel(
                "<i>Unregister removes the medium from VirtualBox's media "
                "registry and from VBoxFront's library. Delete also erases "
                "the file permanently.</i>"
            )
        )

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Remove")
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _ok(self):
        delete = self.mode.currentIndex() == 1
        if delete:
            ret = QMessageBox.warning(
                self,
                "Delete file?",
                "This will permanently delete the disk image file. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        self.delete_file = delete
        self.accept()


class ContentsDialog(QDialog):
    """Read a byte range out of an image without mounting it.

    `mediumio cat` opens the image through VirtualBox's own backend stack, so
    this works on any supported format, on differencing chains, and on
    encrypted media (which refuse to be read without their password) — none of
    which a host-side loop mount can manage.
    """

    def __init__(self, kind: str, medium: MediumRecord, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Image contents")
        self._kind = kind
        self._medium = medium
        self.command: tuple[list[str], bytes | None] | None = None

        encrypted = medium.encryption not in ("", "disabled")
        layout = QFormLayout(self)
        layout.addRow(QLabel(f"<b>{os.path.basename(medium.location)}</b>"))
        layout.addRow("Capacity:", QLabel(medium.capacity or "?"))

        self.offset_edit = QLineEdit("0")
        self.offset_edit.setToolTip("Decimal, 0x-hex, or a K/M/G suffix")
        layout.addRow("Offset:", self.offset_edit)
        self.size_edit = QLineEdit("512")
        self.size_edit.setToolTip("Decimal, 0x-hex, or a K/M/G suffix")
        layout.addRow("Length:", self.size_edit)

        self.hex_radio = QRadioButton("Hex dump into the output panel")
        self.save_radio = QRadioButton("Save the range to a file")
        self.hex_radio.setChecked(True)
        layout.addRow(self.hex_radio)
        layout.addRow(self.save_radio)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        layout.addRow("Output file:", path_row)

        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setEnabled(encrypted)
        self.password.setPlaceholderText(
            "" if encrypted else "not encrypted — no password needed"
        )
        layout.addRow("Password:", self.password)
        layout.addRow(QLabel(
            "<i>Reads only; the image is opened read-only. A hex dump of a "
            "large range will be large — the first sectors are usually what "
            "you want.</i>"
        ))

        self.hex_radio.toggled.connect(self._sync_fields)
        self._sync_fields()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Read")
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _sync_fields(self):
        saving = self.save_radio.isChecked()
        self.path_edit.setEnabled(saving)

    def _browse(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save range as", os.path.expanduser("~"), "All files (*)"
        )
        if path:
            self.path_edit.setText(path)

    def _ok(self):
        offset = parse_byte_size(self.offset_edit.text() or "0")
        size = parse_byte_size(self.size_edit.text())
        if offset is None:
            QMessageBox.warning(self, "Bad offset", "Offset must be a byte count.")
            return
        if not size:
            QMessageBox.warning(self, "Bad length", "Length must be a byte count above zero.")
            return
        output = ""
        if self.save_radio.isChecked():
            output = self.path_edit.text().strip()
            if not output:
                QMessageBox.warning(self, "Missing file", "Choose an output file.")
                return
            if same_file(output, self._medium.location):
                QMessageBox.warning(
                    self, "Same file",
                    "That is the image being read. Choose another output file.",
                )
                return
            # mediumio overwrites its --output silently, so ask first.
            if os.path.exists(output):
                ret = QMessageBox.question(
                    self, "Overwrite?", f"{output} already exists. Overwrite?"
                )
                if ret != QMessageBox.StandardButton.Yes:
                    return
        if self.hex_radio.isChecked() and size > HEX_DUMP_WARN_BYTES:
            ret = QMessageBox.question(
                self, "Large dump",
                f"{format_mbytes(size // (1024 * 1024)) or f'{size} bytes'} of "
                f"hex is about {size // 16:,} lines in the output panel, which "
                "will be slow.\n\nDump it anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        if self.password.isEnabled() and not self.password.text():
            QMessageBox.warning(
                self, "Password needed",
                "This medium is encrypted; VBoxManage refuses to read it "
                "without the password.",
            )
            return
        self.command = mediumio_cat_args(
            self._kind, self._medium.uuid, offset=offset, size=size,
            hex_dump=self.hex_radio.isChecked(), output=output,
            password=self.password.text() if self.password.isEnabled() else "",
        )
        self.accept()


class CreateDiffDialog(QDialog):
    """Create a differencing child on top of an existing disk.

    The listing already draws these chains as a tree; this is the other half.
    A child inherits its parent's capacity, so no size is asked for — writes
    land in the child and the parent stays untouched.
    """

    def __init__(self, parent_medium: MediumRecord, backends: list[MediumBackend] | None = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create differencing image")
        self._parent_medium = parent_medium
        self.path = ""
        self.fmt = ""

        layout = QFormLayout(self)
        layout.addRow("Parent:", QLabel(f"<b>{os.path.basename(parent_medium.location)}</b>"))
        layout.addRow("Inherited capacity:", QLabel(parent_medium.capacity or "?"))

        base, ext = os.path.splitext(parent_medium.location)
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(f"{base}-diff{ext or '.vdi'}")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        layout.addRow("Child file:", path_row)

        differencing = [
            b.id for b in (backends or []) if b.capabilities & CAP_DIFFERENCING
        ]
        self.fmt_combo = QComboBox()
        self.fmt_combo.addItem("same as parent", "")
        for ident in differencing:
            self.fmt_combo.addItem(ident, ident)
        layout.addRow("Format:", self.fmt_combo)
        layout.addRow(QLabel(
            "<i>Writes go to the child; the parent is left alone. Attach the "
            "child to the VM, not the parent.</i>"
        ))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Create")
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _browse(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Differencing image", self.path_edit.text(),
            KIND_META["disk"]["filter"],
        )
        if path:
            self.path_edit.setText(path)

    def _ok(self):
        path = self.path_edit.text().strip()
        if not path:
            QMessageBox.warning(self, "Missing file", "Choose a target file.")
            return
        if os.path.exists(path):
            QMessageBox.warning(
                self, "File exists",
                "createmedium refuses to overwrite existing files; choose a "
                "new path.",
            )
            return
        self.path = path
        self.fmt = self.fmt_combo.currentData() or ""
        self.accept()

    def args(self) -> list[str]:
        return create_diff_args(self.path, self._parent_medium.uuid, self.fmt)


class CreateFloppyDialog(QDialog):
    """Create a floppy image, FAT-formatted so it is usable straight away."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create floppy image")
        self.path = ""
        self.size_mb = 2
        self.formatted = True

        layout = QFormLayout(self)
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(os.path.join(os.path.expanduser("~"), "floppy.img"))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        layout.addRow("File:", path_row)

        self.spin = QSpinBox()
        self.spin.setRange(1, 32)
        self.spin.setSuffix(" MB")
        self.spin.setValue(2)
        layout.addRow("Size:", self.spin)

        self.format_check = QRadioButton("Format as FAT (usable immediately)")
        self.raw_check = QRadioButton("Leave it blank")
        self.format_check.setChecked(True)
        layout.addRow(self.format_check)
        layout.addRow(self.raw_check)
        layout.addRow(QLabel(
            "<i>An unformatted image has to be formatted from inside a VM "
            "before anything can be written to it.</i>"
        ))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Create")
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _browse(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Floppy image", self.path_edit.text(), KIND_META["floppy"]["filter"]
        )
        if path:
            self.path_edit.setText(path)

    def _ok(self):
        path = self.path_edit.text().strip()
        if not path:
            QMessageBox.warning(self, "Missing file", "Choose a target file.")
            return
        if os.path.exists(path):
            QMessageBox.warning(
                self, "File exists",
                "createmedium refuses to overwrite existing files; choose a "
                "new path.",
            )
            return
        self.path = path
        self.size_mb = self.spin.value()
        self.formatted = self.format_check.isChecked()
        self.accept()

    def args(self) -> list[str]:
        return create_floppy_args(self.path, self.size_mb, self.formatted)


class ResolveDialog(QDialog):
    """Rescue a medium VirtualBox can no longer read.

    Two different faults look identical in the listing. If the *file* is gone
    from where the registry expects it, the registry is what needs fixing
    (`modifymedium --setlocation`). If the file is there but unreadable, the
    image itself may be damaged, and `internalcommands repairhd` can try —
    with a dry run first, since a repair rewrites the image in place.
    """

    def __init__(self, kind: str, medium: MediumRecord, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Resolve medium")
        self._kind = kind
        self._medium = medium
        self.command: list[str] | None = None

        # Ask the filesystem, not the record: VBoxSVC caches an open medium, so
        # `State:` can still read "created" for a file that has already moved
        # out from under it (and vice versa after a repair).
        file_present = bool(medium.location) and os.path.exists(medium.location)
        repairable = kind == "disk" and file_present

        layout = QFormLayout(self)
        layout.addRow(QLabel(f"<b>{os.path.basename(medium.location)}</b>"))
        layout.addRow("State:", QLabel(state_word(medium) or "unknown"))
        layout.addRow(
            "File:",
            QLabel("present" if file_present else "missing at the registered path"),
        )
        if medium.access_error:
            reason = QLabel(medium.access_error)
            reason.setWordWrap(True)
            reason.setStyleSheet("color:#c05000;")
            layout.addRow("VirtualBox says:", reason)

        self.relocate_radio = QRadioButton("Point the registry at another file")
        self.dryrun_radio = QRadioButton("Check the image for damage (dry run)")
        self.repair_radio = QRadioButton("Repair the image (rewrites it in place)")
        self.relocate_radio.setChecked(True)
        for radio in (self.relocate_radio, self.dryrun_radio, self.repair_radio):
            layout.addRow(radio)
        self.dryrun_radio.setEnabled(repairable)
        self.repair_radio.setEnabled(repairable)
        if not repairable:
            hint = ("repairhd needs the file to exist"
                    if kind == "disk" else "repairhd only handles hard disks")
            self.dryrun_radio.setToolTip(hint)
            self.repair_radio.setToolTip(hint)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(medium.location)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        layout.addRow("New location:", path_row)

        layout.addRow(QLabel(
            "<i>Relocating only updates VirtualBox's registry; the file is not "
            "moved or touched. Repair uses <tt>VBoxManage internalcommands</tt>, "
            "which Oracle ships unsupported — take a copy of the image first.</i>"
        ))

        self.relocate_radio.toggled.connect(self._sync_fields)
        self._sync_fields()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _sync_fields(self):
        self.path_edit.setEnabled(self.relocate_radio.isChecked())

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate the image file",
            os.path.dirname(self._medium.location) or os.path.expanduser("~"),
            KIND_META[self._kind]["filter"],
        )
        if path:
            self.path_edit.setText(path)

    def _ok(self):
        if self.relocate_radio.isChecked():
            path = self.path_edit.text().strip()
            if not path:
                QMessageBox.warning(self, "Missing path", "Choose the image file.")
                return
            if not os.path.exists(path):
                QMessageBox.warning(
                    self, "No such file",
                    f"{path} does not exist. Relocating to it would leave the "
                    "medium inaccessible.",
                )
                return
            if same_file(path, self._medium.location):
                QMessageBox.warning(
                    self, "Same file",
                    "That is the location already registered — pick the file's "
                    "new home, or repair the image instead.",
                )
                return
            self.command = setlocation_args(self._kind, self._medium.uuid, path)
        elif self.dryrun_radio.isChecked():
            self.command = repairhd_args(
                self._medium.location, self._medium.fmt, dry_run=True
            )
        else:
            ret = QMessageBox.warning(
                self,
                "Repair image?",
                "repairhd rewrites the image in place and is an unsupported "
                "development tool. Copy the file first if it holds anything you "
                "cannot lose.\n\nRun the repair?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
            self.command = repairhd_args(self._medium.location, self._medium.fmt)
        self.accept()


class UuidToolsDialog(QDialog):
    """Stamp a new UUID (or parent UUID) into an image file.

    This is the way out of "cannot register the hard disk because a medium with
    the same UUID already exists" after copying a .vdi: the copy carries the
    original's UUID until `sethduuid` gives it a fresh one. It writes to the
    file, so it must not be aimed at an image VirtualBox has registered — the
    registry would still hold the old UUID.
    """

    def __init__(self, registered: dict[str, str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Image UUID tools")
        self._registered = registered
        self.command: list[str] | None = None

        layout = QFormLayout(self)
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("image file to stamp")
        self.path_edit.textChanged.connect(self._sync_fields)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        layout.addRow("Image file:", path_row)

        self.new_radio = QRadioButton("Assign a freshly generated UUID")
        self.own_radio = QRadioButton("Assign this UUID")
        self.parent_radio = QRadioButton("Assign this parent UUID")
        self.new_radio.setChecked(True)
        for radio in (self.new_radio, self.own_radio, self.parent_radio):
            layout.addRow(radio)
            radio.toggled.connect(self._sync_fields)

        self.uuid_edit = QLineEdit()
        self.uuid_edit.setPlaceholderText("00000000-0000-0000-0000-000000000000")
        layout.addRow("UUID:", self.uuid_edit)

        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        layout.addRow(self.warning)
        layout.addRow(QLabel(
            "<i>Uses <tt>VBoxManage internalcommands</tt>, which Oracle ships "
            "unsupported. Aim it at an unregistered copy: stamping a registered "
            "image desynchronises it from the registry.</i>"
        ))

        self._sync_fields()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Image file", os.path.expanduser("~"),
            KIND_META["disk"]["filter"],
        )
        if path:
            self.path_edit.setText(path)

    def _registered_as(self) -> str:
        path = self.path_edit.text().strip()
        for location, uuid in self._registered.items():
            if same_file(path, location):
                return uuid
        return ""

    def _sync_fields(self, *_args):
        self.uuid_edit.setEnabled(not self.new_radio.isChecked())
        uuid = self._registered_as()
        self.warning.setText(
            f"<b>This image is registered</b> (UUID {uuid}). Stamping it will "
            "leave the registry pointing at a UUID the file no longer has — "
            "remove it from VirtualBox first."
            if uuid else ""
        )
        self.warning.setStyleSheet("color:#c05000;" if uuid else "")

    def _ok(self):
        path = self.path_edit.text().strip()
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "No such file", "Choose an existing image file.")
            return
        if not self.new_radio.isChecked() and not is_uuid(self.uuid_edit.text()):
            QMessageBox.warning(self, "Bad UUID", "Enter a full 36-character UUID.")
            return
        if self._registered_as():
            ret = QMessageBox.warning(
                self, "Registered image",
                "VirtualBox has this image registered under its current UUID. "
                "Stamping it anyway will make the registry entry inaccessible."
                "\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        uuid = self.uuid_edit.text().strip().strip("{}")
        if self.parent_radio.isChecked():
            self.command = sethdparentuuid_args(path, uuid)
        else:
            self.command = sethduuid_args(path, "" if self.new_radio.isChecked() else uuid)
        self.accept()


class CommandHistoryDialog(QDialog):
    """Every VBoxManage command this app has run, with its exit code."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Command history")
        self.resize(760, 420)
        layout = QVBoxLayout(self)

        self.list = QListWidget()
        for entry in reversed(load_history()):
            code = entry.get("exit")
            mark = "ok " if code == 0 else f"exit {code}"
            item = QListWidgetItem(
                f"[{mark}] {entry.get('when', '')}  {entry.get('command', '')}"
            )
            item.setData(Qt.ItemDataRole.UserRole, entry.get("command", ""))
            if code != 0:
                item.setForeground(QColor(220, 80, 80))
            self.list.addItem(item)
        if not self.list.count():
            self.list.addItem("No commands recorded yet.")
        layout.addWidget(self.list)

        row = QHBoxLayout()
        copy_one = QPushButton("Copy selected")
        copy_one.clicked.connect(self._copy_selected)
        copy_all = QPushButton("Copy all")
        copy_all.clicked.connect(self._copy_all)
        clear = QPushButton("Clear history")
        clear.clicked.connect(self._clear)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(copy_one)
        row.addWidget(copy_all)
        row.addWidget(clear)
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)

    def _copy_selected(self):
        commands = [
            item.data(Qt.ItemDataRole.UserRole)
            for item in self.list.selectedItems()
            if item.data(Qt.ItemDataRole.UserRole)
        ]
        if commands:
            QApplication.clipboard().setText("\n".join(commands))

    def _copy_all(self):
        commands = [e.get("command", "") for e in load_history()]
        if commands:
            QApplication.clipboard().setText("\n".join(commands))

    def _clear(self):
        ret = QMessageBox.question(self, "Clear history", "Forget every recorded command?")
        if ret == QMessageBox.StandardButton.Yes:
            clear_history()
            self.list.clear()
            self.list.addItem("No commands recorded yet.")


class PropertiesDialog(QDialog):
    """Edit medium type and description (modifymedium --type/--description)."""

    def __init__(self, medium: MediumRecord, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Medium properties")
        self._medium = medium
        self.new_type: str | None = None
        self.new_description: str | None = None
        self.new_autoreset: bool | None = None

        layout = QFormLayout(self)
        layout.addRow(QLabel(f"<b>{os.path.basename(medium.location)}</b>"))

        self.type_combo = QComboBox()
        self.type_combo.addItems(MEDIUM_TYPES)
        current = medium_type_word(medium.medium_type)
        if current in MEDIUM_TYPES:
            self.type_combo.setCurrentText(current)
        # VirtualBox refuses any type change on a differencing medium
        # ("Cannot change the type of medium ... because it is a differencing
        # medium"), so say so here instead of surfacing that as a COM error.
        self.type_combo.setEnabled(not is_differencing(medium))
        layout.addRow("Type:", self.type_combo)

        # --autoreset governs whether an immutable disk is rolled back on every
        # VM start; it is meaningless on the other types.
        self.autoreset = QCheckBox("Reset on every VM start (immutable disks)")
        self.autoreset.setChecked(medium.auto_reset.lower() == "on")
        self._autoreset_was = self.autoreset.isChecked()
        layout.addRow("Auto-reset:", self.autoreset)

        self.desc_edit = QPlainTextEdit()
        self.desc_edit.setPlainText(medium.description)
        self.desc_edit.setMaximumHeight(90)
        layout.addRow("Description:", self.desc_edit)
        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        layout.addRow(self.hint)

        self.type_combo.currentTextChanged.connect(self._sync_fields)
        self._sync_fields()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _sync_fields(self, *_args):
        immutable = self.type_combo.currentText() == "immutable"
        self.autoreset.setEnabled(immutable and self.type_combo.isEnabled())
        if not self.type_combo.isEnabled():
            self.hint.setText(
                "<i>This is a differencing image; VirtualBox will not change "
                "the type of one. Its type follows the chain it belongs to.</i>"
            )
        else:
            self.hint.setText(
                "<i>Type changes require the disk to be detached from VMs.</i>"
            )

    def _ok(self):
        chosen = self.type_combo.currentText()
        if self.type_combo.isEnabled() and chosen != medium_type_word(self._medium.medium_type):
            self.new_type = chosen
        desc = self.desc_edit.toPlainText()
        if desc != self._medium.description:
            self.new_description = desc
        if self.autoreset.isEnabled() and self.autoreset.isChecked() != self._autoreset_was:
            self.new_autoreset = self.autoreset.isChecked()
        self.accept()

    def args(self, kind: str) -> list[str] | None:
        return properties_args(kind, self._medium.uuid, self.new_type, self.new_description)

    def autoreset_command(self) -> list[str] | None:
        """A separate call: --autoreset is disk-only and independent of the
        type/description edit, which may itself be a no-op."""
        if self.new_autoreset is None:
            return None
        return autoreset_args(self._medium.uuid, self.new_autoreset)


class EncryptDialog(QDialog):
    """Set, change or remove disk encryption (needs the Extension Pack)."""

    #: A third exec() result, distinct from Accepted/Rejected: the user asked to
    #: verify the current password rather than to change anything.
    CHECK_ONLY = 2

    def __init__(self, medium: MediumRecord, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Disk encryption")
        self._medium = medium
        #: Set when "Check current password" is pressed, so the caller runs
        #: checkmediumpwd instead of closing the dialog.
        self.check_command: tuple[list[str], bytes] | None = None

        encrypted = medium.encryption not in ("", "disabled")
        layout = QFormLayout(self)
        layout.addRow(QLabel(f"<b>{os.path.basename(medium.location)}</b>"))
        state = medium.encryption or "unknown"
        if medium.cipher:
            state += f" — {medium.cipher}"
        if medium.password_id:
            state += f" (password ID {medium.password_id})"
        layout.addRow("Current state:", QLabel(state))

        self.encrypt_radio = QRadioButton("Encrypt (set password)")
        self.change_radio = QRadioButton("Change password (stay encrypted)")
        self.decrypt_radio = QRadioButton("Decrypt (remove encryption)")
        self.encrypt_radio.setChecked(not encrypted)
        self.decrypt_radio.setChecked(encrypted)
        self.encrypt_radio.setEnabled(not encrypted)
        self.change_radio.setEnabled(encrypted)
        self.decrypt_radio.setEnabled(encrypted)
        for radio in (self.encrypt_radio, self.change_radio, self.decrypt_radio):
            layout.addRow(radio)

        self.old_password = QLineEdit()
        self.old_password.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addRow("Current password:", self.old_password)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addRow("New password:", self.password)
        self.confirm = QLineEdit()
        self.confirm.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addRow("Confirm:", self.confirm)

        self.check_btn = QPushButton("Check current password")
        self.check_btn.setToolTip(
            "Run checkmediumpwd to confirm the current password before using it"
        )
        self.check_btn.clicked.connect(self._check_password)
        layout.addRow("", self.check_btn)

        self.pwid = QLineEdit(os.path.basename(medium.location) or medium.uuid)
        layout.addRow("Password ID:", self.pwid)
        self.cipher = QComboBox()
        self.cipher.addItems(ENCRYPTION_CIPHERS)
        if medium.cipher in ENCRYPTION_CIPHERS:
            self.cipher.setCurrentText(medium.cipher)
        layout.addRow("Cipher:", self.cipher)
        layout.addRow(QLabel("<i>Requires the VirtualBox Extension Pack.</i>"))

        for radio in (self.encrypt_radio, self.change_radio, self.decrypt_radio):
            radio.toggled.connect(self._sync_fields)
        self._sync_fields()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _check_password(self):
        if not self.old_password.text():
            QMessageBox.warning(
                self, "Missing password", "Enter the password to check."
            )
            return
        self.check_command = check_password_args(
            self._medium.uuid, self.old_password.text()
        )
        self.done(self.CHECK_ONLY)

    def _sync_fields(self):
        setting = self.encrypt_radio.isChecked() or self.change_radio.isChecked()
        self.check_btn.setEnabled(not self.encrypt_radio.isChecked())
        self.old_password.setEnabled(not self.encrypt_radio.isChecked())
        self.password.setEnabled(setting)
        self.confirm.setEnabled(setting)
        self.pwid.setEnabled(setting)
        self.cipher.setEnabled(setting)

    def _ok(self):
        # Validation only — the secrets stay in the widgets until
        # command_parts() is asked for them, so a command that never launches
        # leaves nothing behind on disk.
        if not self.encrypt_radio.isChecked() and not self.old_password.text():
            QMessageBox.warning(
                self, "Missing password", "Enter the medium's current password."
            )
            return
        if not self.decrypt_radio.isChecked():
            if not self.password.text():
                QMessageBox.warning(self, "Missing password", "Enter a new password.")
                return
            if self.password.text() != self.confirm.text():
                QMessageBox.warning(self, "Mismatch", "Passwords do not match.")
                return
            if not self.pwid.text().strip():
                QMessageBox.warning(self, "Missing ID", "Enter a password identifier.")
                return
        self.accept()

    def command_parts(self) -> tuple[list[str], bytes | None, list[str]]:
        """(argv, stdin payload, secret files to shred once it exits).

        Call this immediately before launching, never at accept time: changing
        a password needs two secrets, and two secrets cannot share one stdin
        stream, so they have to be spilled to 0600 files that must not outlive
        the command.
        """
        uuid = self._medium.uuid
        if self.encrypt_radio.isChecked():
            args, stdin_data = encrypt_args(
                uuid, self.password.text(), self.pwid.text().strip(),
                self.cipher.currentText(),
            )
            return args, stdin_data, []
        if self.decrypt_radio.isChecked():
            args, stdin_data = decrypt_args(uuid, self.old_password.text())
            return args, stdin_data, []
        old_file = write_secret_file(self.old_password.text())
        new_file = write_secret_file(self.password.text())
        args = change_password_args(
            uuid, old_file, new_file, self.pwid.text().strip(),
            self.cipher.currentText(),
        )
        return args, None, [old_file, new_file]


class AttachDialog(QDialog):
    """Attach a medium to a VM's storage controller (storageattach)."""

    def __init__(self, kind: str, medium: MediumRecord, vms: list[tuple[str, str]],
                 running: set[str], capture, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Attach to VM")
        self._kind = kind
        self._medium = medium
        self._vms = vms
        self._capture = capture
        self._vminfo: dict[str, str] = {}
        self._controllers: list[tuple[str, int]] = []
        self._request = 0

        layout = QFormLayout(self)
        layout.addRow(QLabel(f"<b>{os.path.basename(medium.location)}</b>"))

        self.vm_combo = QComboBox()
        for name, uuid in vms:
            label = f"{name} (running)" if uuid in running else name
            self.vm_combo.addItem(label, uuid)
        self.vm_combo.currentIndexChanged.connect(self._on_vm_changed)
        layout.addRow("Virtual machine:", self.vm_combo)

        self.ctl_combo = QComboBox()
        self.ctl_combo.currentIndexChanged.connect(self._on_ctl_changed)
        layout.addRow("Controller:", self.ctl_combo)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(0, 29)
        self.port_spin.valueChanged.connect(self._on_slot_changed)
        layout.addRow("Port:", self.port_spin)

        self.device_spin = QSpinBox()
        self.device_spin.setRange(0, 1)
        self.device_spin.valueChanged.connect(self._on_slot_changed)
        layout.addRow("Device:", self.device_spin)

        self.status = QLabel("")
        self.status.setStyleSheet("color:#888;")
        layout.addRow(self.status)

        self.slot_status = QLabel("")
        layout.addRow(self.slot_status)

        # Disk-only storage attributes; each stays out of the command unless
        # asked for. --discard is the one that makes compaction pay off, since
        # without it the guest's TRIM never reaches the image.
        self.mtype_combo = QComboBox(self)
        self.mtype_combo.addItem("unchanged", "")
        for medium_type in MEDIUM_TYPES:
            self.mtype_combo.addItem(medium_type, medium_type)
        self.discard_check = QCheckBox("Pass guest TRIM through (--discard)", self)
        self.nonrotational_check = QCheckBox("Report as an SSD (--nonrotational)", self)
        self.hotpluggable_check = QCheckBox("Hot-pluggable slot (--hotpluggable)", self)
        for widget in (self.mtype_combo, self.discard_check,
                       self.nonrotational_check, self.hotpluggable_check):
            widget.setVisible(kind == "disk")
        if kind == "disk":
            layout.addRow("Medium type:", self.mtype_combo)
            layout.addRow(self.discard_check)
            layout.addRow(self.nonrotational_check)
            layout.addRow(self.hotpluggable_check)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Attach")
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

        if vms:
            self._on_vm_changed(0)

    def _on_vm_changed(self, _index):
        vm_uuid = self.vm_combo.currentData()
        if not vm_uuid:
            return
        self._request += 1
        request = self._request
        self.ctl_combo.clear()
        self._controllers = []
        self._vminfo = {}
        self.slot_status.setText("")
        self.status.setText("Reading VM storage configuration…")

        def done(code: int, output: str):
            # A newer selection may have superseded this request meanwhile.
            if request != self._request:
                return
            if code != 0:
                self.status.setText(f"showvminfo failed (exit {code}).")
                return
            self._vminfo = parse_machinereadable(output)
            self._controllers = vm_storage_controllers(self._vminfo)
            for name, ports in self._controllers:
                self.ctl_combo.addItem(f"{name} ({ports} ports)", name)
            self.status.setText("" if self._controllers else "This VM has no storage controllers.")

        self._capture(["showvminfo", vm_uuid, "--machinereadable"], done)

    def _on_ctl_changed(self, index):
        if index < 0 or index >= len(self._controllers):
            return
        name, ports = self._controllers[index]
        self.port_spin.setMaximum(max(ports - 1, 0))
        self.port_spin.setValue(find_free_port(self._vminfo, name, ports))
        self._on_slot_changed()

    def _on_slot_changed(self, *_args):
        occupant = self._occupant()
        self.slot_status.setText(
            f"Slot in use by {occupant} — attaching here replaces it."
            if occupant else ""
        )
        self.slot_status.setStyleSheet("color:#c05000;" if occupant else "")

    def _occupant(self) -> str:
        controller = self.ctl_combo.currentData()
        if not controller:
            return ""
        return slot_occupant(
            self._vminfo, controller, self.port_spin.value(), self.device_spin.value()
        )

    def _ok(self):
        if self.vm_combo.currentData() is None:
            QMessageBox.warning(self, "No VM", "No virtual machine selected.")
            return
        if self.ctl_combo.currentData() is None:
            QMessageBox.warning(self, "No controller", "This VM has no storage controller to attach to.")
            return
        occupant = self._occupant()
        if occupant:
            ret = QMessageBox.warning(
                self,
                "Slot in use",
                f"Port {self.port_spin.value()}, device {self.device_spin.value()} "
                f"of “{self.ctl_combo.currentData()}” already holds:\n\n{occupant}\n\n"
                "storageattach replaces it silently, detaching that medium from "
                "the VM. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        self.accept()

    def args(self) -> list[str]:
        disk = self._kind == "disk"
        return attach_args(
            self.vm_combo.currentData(),
            self.ctl_combo.currentData(),
            self.port_spin.value(),
            self.device_spin.value(),
            self._kind,
            self._medium.uuid,
            mtype=self.mtype_combo.currentData() if disk else "",
            discard=disk and self.discard_check.isChecked(),
            nonrotational=disk and self.nonrotational_check.isChecked(),
            hotpluggable=disk and self.hotpluggable_check.isChecked(),
        )


class SettingsDialog(QDialog):
    """VBoxManage location + the app-side library of known media paths."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        layout = QFormLayout(self)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(get_settings().value("vboxmanage_path", "", str))
        self.path_edit.setPlaceholderText("auto-detect from PATH")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        layout.addRow("VBoxManage:", path_row)

        self.library_list = QListWidget()
        for kind in KINDS:
            for path in load_library(kind):
                item = QListWidgetItem(f"{kind}: {path}")
                item.setData(Qt.ItemDataRole.UserRole, (kind, path))
                self.library_list.addItem(item)
        layout.addRow("Known media:", self.library_list)

        remove_btn = QPushButton("Remove selected")
        remove_btn.clicked.connect(self._remove_selected)
        layout.addRow("", remove_btn)
        layout.addRow(
            QLabel(
                "<i>Known media are re-opened on every refresh so unattached "
                "images stay visible (modern VirtualBox forgets them once "
                "VBoxSVC exits).</i>"
            )
        )

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "VBoxManage executable")
        if path:
            self.path_edit.setText(path)

    def _remove_selected(self):
        for item in self.library_list.selectedItems():
            self.library_list.takeItem(self.library_list.row(item))

    def _ok(self):
        settings = get_settings()
        settings.setValue("vboxmanage_path", self.path_edit.text().strip())
        keep: dict[str, list[str]] = {kind: [] for kind in KINDS}
        for i in range(self.library_list.count()):
            kind, path = self.library_list.item(i).data(Qt.ItemDataRole.UserRole)
            keep[kind].append(path)
        for kind in KINDS:
            save_library(kind, keep[kind])
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"VBoxFront {APP_VERSION} — VBoxManage GUI")
        self.resize(1200, 760)

        # Media parsed per kind on the last refresh.
        self.media: dict[str, list[MediumRecord]] = {k: [] for k in KINDS}
        # Path to add to the library once the current command succeeds
        # (used after `convertfromraw`, which only writes the file).
        self._pending_library_add: tuple[str, str] | None = None
        # (old, new) library path to rewrite after a successful --move.
        self._pending_library_move: tuple[str, str] | None = None
        # Library entry to drop after a successful closemedium.
        self._pending_library_remove: tuple[str, str] | None = None
        # Commands queued behind the currently running one (batch operations).
        self._cmd_queue: list[tuple[list[str], bytes | None]] = []
        self._refreshing = False
        self._refresh_pending = False
        self._chain: list[tuple[list[str], object]] = []
        # Formats/variants VirtualBox actually supports, read once and cached;
        # empty means the dialogs fall back to their hardcoded lists.
        self.backends: list[MediumBackend] = []
        self._backends_warned = False

        self._build_ui()
        self._refresh_check_vbm()
        self.refresh_media()

    def _build_ui(self):
        self.act_refresh = self._action("Refresh", self.refresh_media, "F5")
        self.act_create = self._action("Create disk…", self.create_disk, "Ctrl+N")
        self.act_add = self._action("Add file…", self.add_media_from_file)
        self.act_fromraw = self._action("Import RAW…", self.convert_from_raw)
        self.act_info = self._action("Info", self.show_info_selected, "Ctrl+I")
        self.act_compact = self._action("Compact", self.compact_selected)
        self.act_compact_all = self._action("Compact all VDIs", self.compact_all_vdis)
        self.act_resize = self._action("Resize…", self.resize_selected)
        self.act_convert = self._action("Convert / Clone…", self.convert_selected)
        self.act_props = self._action("Properties…", self.edit_properties_selected)
        self.act_move = self._action("Move…", self.move_selected)
        self.act_encrypt = self._action("Encryption…", self.encrypt_selected)
        self.act_resolve = self._action("Resolve…", self.resolve_selected)
        self.act_uuid_tools = self._action("Image UUID tools…", self.open_uuid_tools)
        self.act_create_diff = self._action("Create differencing image…", self.create_diff)
        self.act_create_floppy = self._action("Create floppy image…", self.create_floppy)
        self.act_contents = self._action("Contents…", self.show_contents_selected)
        self.act_formatfat = self._action("Format as FAT…", self.formatfat_selected)
        self.act_history = self._action("Command history…", self.open_history)
        self.act_attach = self._action("Attach to VM…", self.attach_selected)
        self.act_detach = self._action("Detach from VM…", self.detach_selected)
        self.act_remove = self._action("Remove…", self.remove_selected, QKeySequence.StandardKey.Delete)
        self.act_copy_uuid = self._action("Copy UUID", self.copy_uuid_selected)
        self.act_copy_path = self._action("Copy location", self.copy_path_selected)
        self.act_open_dir = self._action("Open containing folder", self.open_folder_selected)
        self.act_settings = self._action("Settings…", self.open_settings)
        act_quit = self._action("Quit", self.close, "Ctrl+Q")

        menu_file = self.menuBar().addMenu("&File")
        menu_file.addAction(self.act_history)
        menu_file.addAction(self.act_settings)
        menu_file.addSeparator()
        menu_file.addAction(act_quit)

        menu_media = self.menuBar().addMenu("&Media")
        menu_media.addAction(self.act_refresh)
        menu_media.addSeparator()
        menu_media.addAction(self.act_create)
        menu_media.addAction(self.act_create_diff)
        menu_media.addAction(self.act_create_floppy)
        menu_media.addAction(self.act_add)
        menu_media.addAction(self.act_fromraw)
        menu_media.addAction(self.act_compact_all)
        menu_media.addAction(self.act_uuid_tools)
        menu_media.addSeparator()
        for act in (self.act_info, self.act_contents, self.act_compact, self.act_resize,
                    self.act_convert, self.act_props, self.act_move, self.act_encrypt,
                    self.act_formatfat, self.act_resolve,
                    self.act_attach, self.act_detach):
            menu_media.addAction(act)
        menu_media.addSeparator()
        menu_media.addAction(self.act_remove)

        toolbar = QToolBar("Main")
        # saveState() skips (and warns about) any toolbar without an objectName,
        # and restoreState() matches them by it.
        toolbar.setObjectName("MainToolBar")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        for act in (self.act_refresh, self.act_create, self.act_add, self.act_fromraw):
            toolbar.addAction(act)
        toolbar.addSeparator()
        for act in (self.act_info, self.act_compact, self.act_resize,
                    self.act_convert, self.act_resolve):
            toolbar.addAction(act)
        toolbar.addSeparator()
        for act in (self.act_attach, self.act_detach, self.act_remove):
            toolbar.addAction(act)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        toolbar.addWidget(QLabel("Filter: "))
        self.filter_edit = QLineEdit()
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setMaximumWidth(240)
        self.filter_edit.textChanged.connect(self._apply_filter)
        find = QAction(self)
        find.setShortcut(QKeySequence("Ctrl+F"))
        find.triggered.connect(self.filter_edit.setFocus)
        self.addAction(find)
        toolbar.addWidget(self.filter_edit)

        self.tabs = QTabWidget()
        self.panes: dict[str, MediaPane] = {}
        for kind in KINDS:
            pane = MediaPane(kind)
            pane.tree.doubleClicked.connect(lambda *_: self.show_info_selected())
            pane.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            pane.tree.customContextMenuRequested.connect(
                lambda pos, p=pane: self._show_context_menu(p, pos)
            )
            self.tabs.addTab(pane, KIND_META[kind]["title"])
            self.panes[kind] = pane
        self.tabs.currentChanged.connect(lambda *_: self._update_actions())

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.runner = CommandRunner()
        self.runner.finished.connect(self._on_runner_finished)
        self.splitter.addWidget(self.tabs)
        self.splitter.addWidget(self.runner)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)

        self.setCentralWidget(self.splitter)
        self.setStatusBar(QStatusBar())
        self._update_actions()
        self._restore_ui_state()

    def _restore_ui_state(self):
        """Put the window back the way the user left it.

        Everything lives in the same per-user QSettings store as the rest of the
        configuration (`~/.config/vboxfront/vboxfront.conf` on Linux, the
        registry on Windows, a plist on macOS) — no hand-rolled config file, and
        no writing next to the binary.

        Qt's own saveGeometry/saveState blobs are used rather than
        hand-serialised numbers: they already carry the maximised and
        full-screen flags, the screen the window was on, and the DPI it was
        saved at, and restoreGeometry re-clamps a window whose monitor has gone
        away.
        """
        settings = get_settings()
        geometry = settings.value("ui/geometry", QByteArray(), type=QByteArray)
        if not geometry.isEmpty():
            self.restoreGeometry(geometry)
        window_state = settings.value("ui/window_state", QByteArray(), type=QByteArray)
        if not window_state.isEmpty():
            self.restoreState(window_state)
        splitter = settings.value("ui/splitter", QByteArray(), type=QByteArray)
        if not splitter.isEmpty():
            self.splitter.restoreState(splitter)
        for kind, pane in self.panes.items():
            header = settings.value(f"ui/columns/{kind}", QByteArray(), type=QByteArray)
            if not header.isEmpty() and pane.tree.header().restoreState(header):
                # A restored layout must not be overwritten by the one-off
                # autosize when the first listing arrives.
                pane.mark_columns_restored()
        index = settings.value("ui/tab", 0, type=int)
        if 0 <= index < self.tabs.count():
            self.tabs.setCurrentIndex(index)

    def _save_ui_state(self):
        settings = get_settings()
        settings.setValue("ui/geometry", self.saveGeometry())
        settings.setValue("ui/window_state", self.saveState())
        settings.setValue("ui/splitter", self.splitter.saveState())
        for kind, pane in self.panes.items():
            settings.setValue(f"ui/columns/{kind}", pane.tree.header().saveState())
        settings.setValue("ui/tab", self.tabs.currentIndex())

    def _action(self, text: str, slot, shortcut=None) -> QAction:
        act = QAction(text, self)
        if shortcut is not None:
            act.setShortcut(QKeySequence(shortcut) if isinstance(shortcut, str) else shortcut)
        act.triggered.connect(slot)
        return act

    def _update_actions(self):
        is_disk = self.current_kind() == "disk"
        for act in (self.act_create, self.act_fromraw, self.act_compact, self.act_compact_all,
                    self.act_resize, self.act_convert, self.act_props, self.act_move,
                    self.act_encrypt, self.act_create_diff):
            act.setEnabled(is_disk)
        # formatfat writes a FAT filesystem; DVDs are read-only images.
        self.act_formatfat.setEnabled(self.current_kind() in ("disk", "floppy"))

    def _show_context_menu(self, pane: MediaPane, pos):
        menu = QMenu(self)
        for act in (self.act_info, self.act_contents, self.act_props,
                    self.act_attach, self.act_detach):
            menu.addAction(act)
        menu.addSeparator()
        for act in (self.act_compact, self.act_resize, self.act_convert,
                    self.act_move, self.act_encrypt, self.act_create_diff,
                    self.act_formatfat, self.act_resolve):
            menu.addAction(act)
        menu.addSeparator()
        for act in (self.act_copy_uuid, self.act_copy_path, self.act_open_dir):
            menu.addAction(act)
        menu.addSeparator()
        menu.addAction(self.act_remove)
        menu.exec(pane.tree.viewport().mapToGlobal(pos))

    def current_kind(self) -> str:
        return KINDS[self.tabs.currentIndex()]

    def current_pane(self) -> MediaPane:
        return self.panes[self.current_kind()]

    def _apply_filter(self, text: str):
        for pane in self.panes.values():
            pane.apply_filter(text)

    def _refresh_check_vbm(self):
        problem = vboxmanage_problem()
        if problem:
            QMessageBox.warning(self, "VBoxManage unusable", problem)

    def selected_medium(self) -> MediumRecord | None:
        return self.current_pane().selected_record()

    def _require_idle(self) -> bool:
        if self.runner.is_busy():
            QMessageBox.information(
                self, "Busy", "Another VBoxManage command is still running."
            )
            return False
        return True

    def _require_selection(self) -> MediumRecord | None:
        m = self.selected_medium()
        if not m:
            QMessageBox.information(self, "No selection", "Select a medium first.")
            return None
        return m

    def _capture(self, args: list[str], on_done) -> bool:
        vbm = vboxmanage_path()
        if not vbm:
            self.statusBar().showMessage("VBoxManage not found.")
            return False
        CaptureRunner(self, on_done).start(vbm, args)
        return True

    def refresh_media(self):
        if self._refreshing:
            self._refresh_pending = True
            return
        vbm = vboxmanage_path()
        if not vbm:
            self.statusBar().showMessage("VBoxManage not found.")
            return
        self._refreshing = True
        self.statusBar().showMessage("Refreshing media…")

        # Re-open library paths first: modern VirtualBox drops standalone
        # registrations when VBoxSVC exits, so `list` alone would miss them.
        self._chain = []
        if not self.backends:
            self._chain.append((["list", "hddbackends"], self._backends_done))
        for kind in KINDS:
            for path in load_library(kind):
                self._chain.append((["showmediuminfo", kind, path], self._library_open_done(kind, path)))
        for kind in KINDS:
            self._chain.append((list_media_args(kind), self._list_done(kind)))
        self._advance_chain()

    def _backends_done(self, code: int, output: str):
        if code != 0:
            # Retried on the next refresh, but said once: every refresh
            # otherwise repeats it into the output panel.
            if not self._backends_warned:
                self._backends_warned = True
                self.runner.note(
                    "[formats] could not read `list hddbackends`; falling back "
                    "to the built-in format list"
                )
            return
        self.backends = parse_hddbackends(output)

    def _library_open_done(self, kind: str, path: str):
        def done(code: int, _output: str):
            if code != 0:
                self.runner.note(f"[library] could not open {kind} {path} (exit {code})")
        return done

    def _list_done(self, kind: str):
        def done(code: int, output: str):
            if code != 0:
                self.statusBar().showMessage(f"list {KIND_META[kind]['list']} failed (exit {code})")
                self.runner.note(output)
                self.media[kind] = []
                return
            self.media[kind] = parse_media_list(output)
        return done

    def _advance_chain(self):
        if not self._chain:
            self._finish_refresh()
            return
        args, cb = self._chain.pop(0)

        def step_done(code: int, output: str):
            cb(code, output)
            self._advance_chain()

        if not self._capture(args, step_done):
            self._chain = []
            self._finish_refresh()

    def _finish_refresh(self):
        for kind in KINDS:
            self.panes[kind].populate(self.media[kind])
        counts = ", ".join(
            f"{len(self.media[kind])} {KIND_META[kind]['list']}" for kind in KINDS
        )
        self.statusBar().showMessage(f"Registered media: {counts}.")
        self._refreshing = False
        if self._refresh_pending:
            self._refresh_pending = False
            self.refresh_media()

    def create_disk(self):
        dlg = CreateDiskDialog(self.backends, self)
        if dlg.exec() and self._require_idle():
            self.runner.run(dlg.args())

    def create_diff(self):
        m = self._require_selection()
        if not m or not self._require_idle():
            return
        if is_differencing(m):
            ret = QMessageBox.question(
                self, "Differencing image",
                f"{os.path.basename(m.location)} is itself a differencing "
                "image. Stacking another child on it works, but the chain gets "
                "one level deeper.\n\nContinue?",
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        dlg = CreateDiffDialog(m, self.backends, self)
        if dlg.exec() and self._require_idle():
            self.runner.run(dlg.args())

    def create_floppy(self):
        if not self._require_idle():
            return
        dlg = CreateFloppyDialog(self)
        if dlg.exec() and self._require_idle():
            # createmedium registers what it creates, so unlike Import RAW this
            # needs no library entry to survive a VBoxSVC restart.
            self.runner.run(dlg.args())

    def show_contents_selected(self):
        m = self._require_selection()
        if not m or not self._require_idle():
            return
        kind = self.current_kind()
        dlg = ContentsDialog(kind, m, self)
        if not dlg.exec() or not dlg.command or not self._require_idle():
            return
        args, stdin_data = dlg.command
        self.runner.run(args, stdin_data)

    def formatfat_selected(self):
        m = self._require_selection()
        if not m or not self._require_idle():
            return
        kind = self.current_kind()
        children = child_names(self.media[kind], m.uuid)
        if children:
            QMessageBox.warning(
                self, "Read-only",
                f"{os.path.basename(m.location)} is the parent of "
                f"{', '.join(children)}, which makes it read-only — "
                "VirtualBox refuses to write to it. Format the differencing "
                "image instead, or remove the child first.",
            )
            return
        ret = QMessageBox.warning(
            self,
            "Format as FAT?",
            f"This writes a fresh FAT filesystem over "
            f"{os.path.basename(m.location)}. Everything on it is lost.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        password = ""
        if m.encryption not in ("", "disabled"):
            password, ok = QInputDialog.getText(
                self, "Password", f"Password for {os.path.basename(m.location)}:",
                QLineEdit.EchoMode.Password,
            )
            if not ok or not password:
                return
        if not self._require_idle():
            return
        args, stdin_data = mediumio_formatfat_args(kind, m.uuid, password=password)
        self.runner.run(args, stdin_data)

    def add_media_from_file(self):
        kind = self.current_kind()
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select media image",
            os.path.expanduser("~"),
            KIND_META[kind]["filter"],
        )
        if not path:
            return
        # `openmedium` is gone from modern VBoxManage and showmediuminfo only
        # registers transiently, so persistence lives in the app's library.
        add_library_path(kind, path)
        self.runner.note(f"[library] added {kind} {path}")
        self.refresh_media()

    def show_info_selected(self):
        m = self._require_selection()
        if not m or not self._require_idle():
            return
        self.runner.run(["showmediuminfo", self.current_kind(), m.uuid])

    def compact_selected(self):
        m = self._require_selection()
        if not m:
            return
        if m.fmt.upper() != "VDI":
            ret = QMessageBox.question(
                self,
                "Compact",
                f"Compacting is normally only supported for VDI; this disk is "
                f"{m.fmt}. Try anyway?",
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        if not self._require_idle():
            return
        self.runner.run(["modifymedium", "disk", m.uuid, "--compact"])

    def compact_all_vdis(self):
        vdis = [r for r in self.media["disk"]
                if r.fmt.upper() == "VDI" and not is_inaccessible(r)]
        if not vdis:
            QMessageBox.information(self, "Compact all", "No accessible VDI disks found.")
            return
        ret = QMessageBox.question(
            self, "Compact all",
            f"Compact {len(vdis)} VDI disk(s) one after another?",
        )
        if ret != QMessageBox.StandardButton.Yes or not self._require_idle():
            return
        commands = [(["modifymedium", "disk", r.uuid, "--compact"], None) for r in vdis]
        first_args, first_stdin = commands[0]
        self._cmd_queue = commands[1:]
        self.runner.run(first_args, first_stdin)
        self._show_queue_status()

    def resize_selected(self):
        m = self._require_selection()
        if not m:
            return
        dlg = ResizeDialog(m, self)
        if dlg.exec() and self._require_idle():
            self.runner.run(["modifymedium", "disk", m.uuid, "--resize", str(dlg.size_mb)])

    def _clear_target(self, path: str) -> bool:
        """Delete a confirmed overwrite target right before launching.

        clonemedium and convertfromraw both fail with VERR_ALREADY_EXISTS
        rather than overwriting, so the file has to go first — but not one
        moment earlier than the command that replaces it.
        """
        try:
            os.remove(path)
        except FileNotFoundError:
            pass  # already gone; the command can create it
        except OSError as e:
            QMessageBox.critical(self, "Error", f"Could not remove {path}: {e}")
            return False
        return True

    def convert_selected(self):
        """Clone the selected disk to a different format (or same, defragmented)."""
        m = self._require_selection()
        if not m or not self._require_idle():
            return
        dlg = ConvertDialog(m, self.backends, self)
        if not dlg.exec() or not self._require_idle():
            return
        if dlg.overwrite and not self._clear_target(dlg.target_path):
            return
        self.runner.run(
            [
                "clonemedium",
                "disk",
                m.uuid,
                dlg.target_path,
                "--format",
                dlg.target_format,
            ]
        )

    def convert_from_raw(self):
        if not self._require_idle():
            return
        src, _ = QFileDialog.getOpenFileName(
            self,
            "Select RAW image",
            os.path.expanduser("~"),
            "RAW images (*.img *.raw *.bin);;All files (*)",
        )
        if not src:
            return
        fmt, dst, overwrite = ask_target_format_and_path(self, src, default_fmt="VDI")
        if not dst or not self._require_idle():
            return
        if overwrite and not self._clear_target(dst):
            return
        # convertfromraw only writes the file; remember it in the library once
        # it completes so the refresh picks it up.
        self._pending_library_add = ("disk", dst)
        self.runner.run(["convertfromraw", src, dst, "--format", fmt])

    def edit_properties_selected(self):
        m = self._require_selection()
        if not m:
            return
        dlg = PropertiesDialog(m, self)
        if not dlg.exec():
            return
        commands = [c for c in (dlg.args(self.current_kind()), dlg.autoreset_command()) if c]
        if not commands:
            self.statusBar().showMessage("Properties unchanged.")
            return
        if not self._require_idle():
            return
        self._cmd_queue = [(c, None) for c in commands[1:]]
        self.runner.run(commands[0])
        self._show_queue_status()

    def move_selected(self):
        m = self._require_selection()
        if not m:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Move disk to", m.location,
            f"{m.fmt} (*{DISK_EXT.get(m.fmt.upper(), '')});;All files (*)",
        )
        if not path or same_file(path, m.location):
            return
        if not self._require_idle():
            return
        self._pending_library_move = (m.location, path)
        self.runner.run(move_args(m.uuid, path))

    def encrypt_selected(self):
        m = self._require_selection()
        if not m or not self._require_idle():
            return
        dlg = EncryptDialog(m, self)
        result = dlg.exec()
        if not result or not self._require_idle():
            return
        if result == EncryptDialog.CHECK_ONLY:
            args, stdin_data = dlg.check_command
            self.runner.run(args, stdin_data)
            return
        args, stdin_data, secrets = dlg.command_parts()
        self.runner.run(args, stdin_data, secrets)

    def attach_selected(self):
        m = self._require_selection()
        if not m:
            return
        # Pin the kind now: the VM list is fetched asynchronously and the user
        # can switch tabs before the dialog opens, which would otherwise attach
        # this medium with the *new* tab's --type.
        kind = self.current_kind()

        def vms_done(code: int, output: str):
            if code != 0:
                self.runner.note(output)
                return
            vms = parse_vms_list(output)
            if not vms:
                QMessageBox.information(self, "No VMs", "No virtual machines are registered.")
                return

            def running_done(_code: int, running_out: str):
                running = {uuid for _n, uuid in parse_vms_list(running_out)}
                dlg = AttachDialog(kind, m, vms, running, self._capture, self)
                if dlg.exec() and self._require_idle():
                    self.runner.run(dlg.args())

            self._capture(["list", "runningvms"], running_done)

        self._capture(["list", "vms"], vms_done)

    def detach_selected(self):
        m = self._require_selection()
        if not m:
            return
        attachments = [e for e in (parse_in_use_entry(x) for x in m.in_use) if e]
        if not attachments:
            QMessageBox.information(self, "Detach", "This medium is not attached to any VM.")
            return
        if len(attachments) == 1:
            vm_name, vm_uuid = attachments[0]
            ret = QMessageBox.question(
                self, "Detach", f"Detach {os.path.basename(m.location)} from “{vm_name}”?"
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        else:
            names = [name for name, _uuid in attachments]
            name, ok = QInputDialog.getItem(
                self, "Detach", "Detach from which VM?", names, 0, False
            )
            if not ok:
                return
            vm_name, vm_uuid = attachments[names.index(name)]

        def vminfo_done(code: int, output: str):
            if code != 0:
                self.runner.note(output)
                return
            info = parse_machinereadable(output)
            slot = find_attachment(info, m.uuid)
            if not slot:
                QMessageBox.warning(
                    self, "Detach",
                    f"Could not locate the attachment slot in “{vm_name}”.",
                )
                return
            ctl, port, device = slot
            if self._require_idle():
                self.runner.run(detach_args(vm_uuid, ctl, port, device))

        self._capture(["showvminfo", vm_uuid, "--machinereadable"], vminfo_done)

    def remove_selected(self):
        m = self._require_selection()
        if not m:
            return
        if m.in_use:
            names = ", ".join(e[0] for e in (parse_in_use_entry(x) for x in m.in_use) if e)
            QMessageBox.warning(
                self, "In use",
                f"This medium is attached to: {names}.\nDetach it before removing.",
            )
            return
        children = child_names(self.media[self.current_kind()], m.uuid)
        if children:
            QMessageBox.warning(
                self, "Has children",
                f"{', '.join(children)} "
                f"{'builds' if len(children) == 1 else 'build'} on this medium, "
                "so closemedium refuses it (VBOX_E_OBJECT_IN_USE).\nRemove the "
                f"differencing {'image' if len(children) == 1 else 'images'} first.",
            )
            return
        dlg = RemoveDialog(m, self)
        if not dlg.exec() or not self._require_idle():
            return
        kind = self.current_kind()
        args = ["closemedium", kind, m.uuid]
        if dlg.delete_file:
            args.append("--delete")
        self._pending_library_remove = (kind, m.location)
        self.runner.run(args)

    def resolve_selected(self):
        m = self._require_selection()
        if not m or not self._require_idle():
            return
        kind = self.current_kind()
        dlg = ResolveDialog(kind, m, self)
        if not dlg.exec() or not dlg.command or not self._require_idle():
            return
        self.runner.run(dlg.command)

    def open_uuid_tools(self):
        if not self._require_idle():
            return
        registered = {
            rec.location: rec.uuid
            for records in self.media.values() for rec in records if rec.location
        }
        dlg = UuidToolsDialog(registered, self)
        if not dlg.exec() or not dlg.command or not self._require_idle():
            return
        self.runner.run(dlg.command)

    def open_history(self):
        CommandHistoryDialog(self).exec()

    def copy_uuid_selected(self):
        m = self._require_selection()
        if m:
            QApplication.clipboard().setText(m.uuid)
            self.statusBar().showMessage(f"Copied {m.uuid}")

    def copy_path_selected(self):
        m = self._require_selection()
        if m:
            QApplication.clipboard().setText(m.location)
            self.statusBar().showMessage(f"Copied {m.location}")

    def open_folder_selected(self):
        m = self._require_selection()
        if m:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(m.location)))

    def open_settings(self):
        if SettingsDialog(self).exec():
            # Re-check straight away: a mistyped path is far easier to connect
            # to Settings now than after the next command fails to start.
            self._refresh_check_vbm()
            self.refresh_media()

    def closeEvent(self, event):
        if self.runner.is_busy():
            ret = QMessageBox.question(
                self,
                "Command still running",
                "A VBoxManage command is still running. Quitting kills it, "
                "which can leave a half-written image behind.\n\nQuit anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._save_ui_state()
        super().closeEvent(event)

    def _show_queue_status(self):
        if self._cmd_queue:
            self.statusBar().showMessage(f"Queue: {len(self._cmd_queue)} command(s) pending.")

    def _on_runner_finished(self, code: int):
        # Only what CommandRunner ran, which is only ever a mutating command:
        # the read-only refresh traffic goes through CaptureRunner and would
        # bury the history under `list`/`showvminfo` noise on every F5.
        if self.runner.last_command:
            append_history(
                self.runner.last_command,
                code,
                QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss"),
            )
        if code == 0:
            add, self._pending_library_add = self._pending_library_add, None
            if add and os.path.exists(add[1]):
                add_library_path(add[0], add[1])
                self.runner.note(f"[library] added {add[0]} {add[1]}")
            move, self._pending_library_move = self._pending_library_move, None
            if move:
                old, new = move
                for kind in KINDS:
                    if old in load_library(kind):
                        remove_library_path(kind, old)
                        add_library_path(kind, new)
            remove, self._pending_library_remove = self._pending_library_remove, None
            if remove:
                remove_library_path(remove[0], remove[1])
        else:
            self._pending_library_add = None
            self._pending_library_move = None
            self._pending_library_remove = None
            if self._cmd_queue:
                self.runner.note(f"[queue] aborted, {len(self._cmd_queue)} command(s) dropped")
                self._cmd_queue = []

        if self._cmd_queue:
            args, stdin_data = self._cmd_queue.pop(0)
            self.runner.run(args, stdin_data)
            self._show_queue_status()
            return
        # Most operations change the registry; refresh quietly.
        self.refresh_media()


def ask_target_format_and_path(parent, src_path: str, default_fmt: str = "VDI"):
    """Prompt for format + destination when converting from raw.

    Returns (format, destination, overwrite). The destination is *not* touched
    here: the caller deletes it, once, immediately before running the command.
    """
    fmt, ok = QInputDialog.getItem(
        parent,
        "Target format",
        "Convert to:",
        [f for f in DISK_FORMATS if f != "RAW"],
        0 if default_fmt not in DISK_FORMATS else DISK_FORMATS.index(default_fmt),
        False,
    )
    if not ok:
        return None, None, False
    base, _ = os.path.splitext(src_path)
    dst_default = base + DISK_EXT.get(fmt, ".vdi")
    dst, _ = QFileDialog.getSaveFileName(
        parent,
        "Save converted image as",
        dst_default,
        f"{fmt} (*{DISK_EXT.get(fmt, '')});;All files (*)",
    )
    if not dst:
        return None, None, False
    if same_file(dst, src_path):
        QMessageBox.warning(
            parent, "Same file",
            "The target is the source image. Convert it to a different file.",
        )
        return None, None, False
    if os.path.exists(dst):
        ret = QMessageBox.question(
            parent, "Overwrite?", f"{dst} exists. Overwrite?"
        )
        if ret != QMessageBox.StandardButton.Yes:
            return None, None, False
        return fmt, dst, True
    return fmt, dst, False


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(f"vboxfront {APP_VERSION}")
        return
    app = QApplication(sys.argv)
    app.setOrganizationName("vboxfront")
    app.setApplicationName("vboxfront")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
