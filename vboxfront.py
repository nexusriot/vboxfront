#!/usr/bin/env python3
"""VBoxFront - PyQt6 frontend for VBoxManage disk image operations."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass, field

from PyQt6.QtCore import (
    QProcess,
    QSettings,
    Qt,
    QUrl,
    pyqtSignal,
)

from PyQt6.QtGui import QAction, QColor, QDesktopServices, QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
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

DISK_FORMATS = ["VDI", "VMDK", "VHD", "RAW"]
DISK_EXT = {"VDI": ".vdi", "VMDK": ".vmdk", "VHD": ".vhd", "RAW": ".img"}
DISK_VARIANTS = ["Standard", "Fixed"]
VMDK_VARIANTS = ["Standard", "Fixed", "Split2G"]
MEDIUM_TYPES = ["normal", "writethrough", "immutable", "shareable", "readonly", "multiattach"]
ENCRYPTION_CIPHERS = ["AES-XTS256-PLAIN64", "AES-XTS128-PLAIN64"]

KINDS = ["disk", "dvd", "floppy"]
KIND_META = {
    "disk": {
        "list": "hdds",
        "title": "Hard disks",
        "attach_type": "hdd",
        "filter": "Disk images (*.vdi *.vmdk *.vhd *.img *.raw);;All files (*)",
    },
    "dvd": {
        "list": "dvds",
        "title": "DVD images",
        "attach_type": "dvddrive",
        "filter": "DVD images (*.iso);;All files (*)",
    },
    "floppy": {
        "list": "floppies",
        "title": "Floppy images",
        "attach_type": "fdd",
        "filter": "Floppy images (*.img *.ima *.flp);;All files (*)",
    },
}

COLUMNS = ["Name", "Format", "Capacity", "Size on disk", "Type", "State", "In use by", "Location", "UUID"]
COL_NAME, COL_FORMAT, COL_CAPACITY, COL_SIZE, COL_TYPE, COL_STATE, COL_IN_USE, COL_LOCATION, COL_UUID = range(9)


@dataclass
class MediumRecord:
    uuid: str
    location: str
    fmt: str
    capacity: str
    size_on_disk: str
    state: str = ""
    medium_type: str = ""
    parent_uuid: str = ""
    variant: str = ""
    encryption: str = ""
    description: str = ""
    in_use: list[str] = field(default_factory=list)


def get_settings() -> QSettings:
    return QSettings("vboxfront", "vboxfront")


def vboxmanage_path() -> str | None:
    custom = get_settings().value("vboxmanage_path", "", str)
    if custom:
        return custom
    return shutil.which("VBoxManage") or shutil.which("vboxmanage")


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


def parse_media_list(text: str) -> list[MediumRecord]:
    """Parse `VBoxManage list [-l] hdds|dvds|floppies` blocks separated by blank lines.

    Values that wrap onto extra lines (e.g. multiple "In use by VMs" entries,
    or multi-line "Property") are indented continuations of the previous key.
    """
    records: list[MediumRecord] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        if not block.strip():
            continue
        kv: dict[str, str] = {}
        last_key: str | None = None
        for line in block.splitlines():
            if not line.strip():
                continue
            if line[0] in " \t":
                if last_key:
                    kv[last_key] += "\n" + line.strip()
                continue
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            last_key = k.strip()
            kv[last_key] = v.strip()
        if "UUID" not in kv or "Location" not in kv:
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
                medium_type=kv.get("Type", ""),
                parent_uuid=kv.get("Parent UUID", ""),
                variant=kv.get("Format variant", ""),
                encryption=kv.get("Encryption", ""),
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
    for p in range(ports):
        if info.get(f"{controller}-{p}-0", "none") == "none":
            return p
    return 0


def list_media_args(kind: str) -> list[str]:
    return ["list", "-l", KIND_META[kind]["list"]]


def create_disk_args(path: str, size_mb: int, fmt: str, variant: str) -> list[str]:
    args = ["createmedium", "disk", "--filename", path, "--size", str(size_mb), "--format", fmt]
    if variant and variant != "Standard":
        args += ["--variant", variant]
    return args


def attach_args(vm: str, controller: str, port: int, device: int, kind: str, medium: str) -> list[str]:
    return [
        "storageattach", vm,
        "--storagectl", controller,
        "--port", str(port),
        "--device", str(device),
        "--type", KIND_META[kind]["attach_type"],
        "--medium", medium,
    ]


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
    # The password goes through stdin ("-") so it never appears in the
    # process list or the echoed command line.
    args = ["encryptmedium", uuid, "--newpassword", "-", "--newpasswordid", password_id, "--cipher", cipher]
    return args, (password + "\n").encode()


def decrypt_args(uuid: str, password: str) -> tuple[list[str], bytes]:
    return ["encryptmedium", uuid, "--oldpassword", "-"], (password + "\n").encode()


def check_password_args(uuid: str, password: str) -> tuple[list[str], bytes]:
    return ["checkmediumpwd", uuid, "-"], (password + "\n").encode()


def medium_type_word(medium_type: str) -> str:
    return medium_type.split(" ")[0] if medium_type else ""


class CommandRunner(QWidget):
    """A panel that runs VBoxManage commands and streams output."""

    finished = pyqtSignal(int)  # exit code

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc: QProcess | None = None
        self._stdin_data: bytes | None = None
        self._progress_tail = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        self.cmd_label = QLabel("Idle.")
        self.cmd_label.setStyleSheet("color:#888;")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setMaximumWidth(180)
        self.progress.setVisible(False)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel)
        header.addWidget(self.cmd_label, 1)
        header.addWidget(self.progress)
        header.addWidget(self.cancel_btn)
        layout.addLayout(header)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("Command output will appear here…")
        layout.addWidget(self.output)

    def is_busy(self) -> bool:
        return self.proc is not None and self.proc.state() != QProcess.ProcessState.NotRunning

    def run(self, args: list[str], stdin_data: bytes | None = None) -> bool:
        if self.is_busy():
            return False
        vbm = vboxmanage_path()
        if not vbm:
            self.output.appendPlainText("[error] VBoxManage not found in PATH.\n")
            self.finished.emit(127)
            return False
        self.proc = QProcess(self)
        self._stdin_data = stdin_data
        self._progress_tail = ""
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._on_output)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)
        self.proc.started.connect(self._on_started)

        pretty = " ".join(shlex.quote(a) for a in [vbm, *args])
        self.cmd_label.setText(f"Running: {pretty}")
        self.output.appendPlainText(f"$ {pretty}")
        if stdin_data:
            self.output.appendPlainText("[password sent via stdin]")
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

    def _on_started(self):
        if self.proc and self._stdin_data:
            self.proc.write(self._stdin_data)
            self.proc.closeWriteChannel()
            self._stdin_data = None

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
            self._reset_ui()
            proc, self.proc = self.proc, None
            if proc:
                proc.deleteLater()
            self.finished.emit(126)

    def _on_finished(self, code: int, _status):
        self.output.appendPlainText(f"[exit {code}]\n")
        self._reset_ui()
        proc, self.proc = self.proc, None
        if proc:
            proc.deleteLater()
        self.finished.emit(code)

    def _reset_ui(self):
        self.cmd_label.setText("Idle.")
        self.cancel_btn.setEnabled(False)
        self.progress.setVisible(False)


class CaptureRunner(QProcess):
    """One-shot QProcess that buffers output and emits a callback."""

    def __init__(self, parent, on_done):
        super().__init__(parent)
        self._buf = bytearray()
        self._on_done = on_done
        self.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.readyReadStandardOutput.connect(self._read)
        self.finished.connect(self._done)
        self.errorOccurred.connect(self._error)

    def _read(self):
        self._buf += bytes(self.readAllStandardOutput())

    def _done(self, code, _status):
        on_done, self._on_done = self._on_done, None
        if on_done:
            on_done(code, self._buf.decode(errors="replace"))
        self.deleteLater()

    def _error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
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
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_LOCATION, QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(False)
        layout.addWidget(self.tree)

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
            parent = r.parent_uuid if r.parent_uuid not in ("", "base") else ""
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
        self.apply_filter(self._filter)

        if prev_uuid:
            self.select_uuid(prev_uuid)

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
            rec.state,
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
            rec.state,
            "\n".join(rec.in_use),
            rec.location,
            rec.uuid,
        ]
        for col, tip in enumerate(tooltips):
            if tip:
                item.setToolTip(col, tip)
        if rec.state.lower() == "inaccessible":
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


class CreateDiskDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create disk")
        self.path = ""
        self.size_mb = 0
        self.fmt = "VDI"
        self.variant = "Standard"

        layout = QFormLayout(self)
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(os.path.join(os.path.expanduser("~"), "new-disk.vdi"))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        layout.addRow("File:", path_row)

        self.fmt_combo = QComboBox()
        self.fmt_combo.addItems([f for f in DISK_FORMATS if f != "RAW"])
        self.fmt_combo.currentTextChanged.connect(self._on_format_changed)
        layout.addRow("Format:", self.fmt_combo)

        self.variant_combo = QComboBox()
        self.variant_combo.addItems(DISK_VARIANTS)
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

    def _on_format_changed(self, fmt: str):
        variants = VMDK_VARIANTS if fmt == "VMDK" else DISK_VARIANTS
        current = self.variant_combo.currentText()
        self.variant_combo.clear()
        self.variant_combo.addItems(variants)
        if current in variants:
            self.variant_combo.setCurrentText(current)
        path = self.path_edit.text()
        base, ext = os.path.splitext(path)
        if ext.lower() in DISK_EXT.values():
            self.path_edit.setText(base + DISK_EXT.get(fmt, ".img"))

    def _browse(self):
        fmt = self.fmt_combo.currentText()
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Disk file",
            self.path_edit.text(),
            f"{fmt} (*{DISK_EXT.get(fmt, '')});;All files (*)",
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
    def __init__(self, disk: MediumRecord, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Convert / Clone disk")
        self.target_format = "VDI"
        self.target_path = ""

        layout = QFormLayout(self)
        layout.addRow(QLabel(f"Source: <b>{os.path.basename(disk.location)}</b> ({disk.fmt})"))

        self.fmt = QComboBox()
        self.fmt.addItems([f for f in DISK_FORMATS if f != "RAW"])
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

    def _update_default_path(self, fmt: str):
        base, _ = os.path.splitext(self._src.location)
        self.path_edit.setText(base + "-converted" + DISK_EXT.get(fmt, ".img"))

    def _browse(self):
        fmt = self.fmt.currentText()
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Target file",
            self.path_edit.text(),
            f"{fmt} (*{DISK_EXT.get(fmt, '')});;All files (*)",
        )
        if path:
            self.path_edit.setText(path)

    def _ok(self):
        path = self.path_edit.text().strip()
        if not path:
            QMessageBox.warning(self, "Missing path", "Please choose a target file.")
            return
        if os.path.exists(path):
            ret = QMessageBox.question(
                self, "Overwrite?", f"{path} already exists. Overwrite?"
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
            try:
                os.remove(path)
            except OSError as e:
                QMessageBox.critical(self, "Error", f"Could not remove file: {e}")
                return
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


class PropertiesDialog(QDialog):
    """Edit medium type and description (modifymedium --type/--description)."""

    def __init__(self, medium: MediumRecord, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Medium properties")
        self._medium = medium
        self.new_type: str | None = None
        self.new_description: str | None = None

        layout = QFormLayout(self)
        layout.addRow(QLabel(f"<b>{os.path.basename(medium.location)}</b>"))

        self.type_combo = QComboBox()
        self.type_combo.addItems(MEDIUM_TYPES)
        current = medium_type_word(medium.medium_type)
        if current in MEDIUM_TYPES:
            self.type_combo.setCurrentText(current)
        layout.addRow("Type:", self.type_combo)

        self.desc_edit = QPlainTextEdit()
        self.desc_edit.setPlainText(medium.description)
        self.desc_edit.setMaximumHeight(90)
        layout.addRow("Description:", self.desc_edit)
        layout.addRow(
            QLabel("<i>Type changes require the disk to be detached from VMs.</i>")
        )

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _ok(self):
        chosen = self.type_combo.currentText()
        if chosen != medium_type_word(self._medium.medium_type):
            self.new_type = chosen
        desc = self.desc_edit.toPlainText()
        if desc != self._medium.description:
            self.new_description = desc
        self.accept()

    def args(self, kind: str) -> list[str] | None:
        return properties_args(kind, self._medium.uuid, self.new_type, self.new_description)


class EncryptDialog(QDialog):
    """Set or remove disk encryption (encryptmedium; needs the Extension Pack)."""

    def __init__(self, medium: MediumRecord, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Disk encryption")
        self._medium = medium
        self.command: tuple[list[str], bytes] | None = None

        encrypted = medium.encryption not in ("", "disabled")
        layout = QFormLayout(self)
        layout.addRow(QLabel(f"<b>{os.path.basename(medium.location)}</b>"))
        layout.addRow("Current state:", QLabel(medium.encryption or "unknown"))

        self.encrypt_radio = QRadioButton("Encrypt (set password)")
        self.decrypt_radio = QRadioButton("Decrypt (remove encryption)")
        self.encrypt_radio.setChecked(not encrypted)
        self.decrypt_radio.setChecked(encrypted)
        layout.addRow(self.encrypt_radio)
        layout.addRow(self.decrypt_radio)

        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addRow("Password:", self.password)
        self.confirm = QLineEdit()
        self.confirm.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addRow("Confirm:", self.confirm)

        self.pwid = QLineEdit(os.path.basename(medium.location) or medium.uuid)
        layout.addRow("Password ID:", self.pwid)
        self.cipher = QComboBox()
        self.cipher.addItems(ENCRYPTION_CIPHERS)
        layout.addRow("Cipher:", self.cipher)
        layout.addRow(QLabel("<i>Requires the VirtualBox Extension Pack.</i>"))

        self.encrypt_radio.toggled.connect(self._sync_fields)
        self._sync_fields()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _sync_fields(self):
        encrypting = self.encrypt_radio.isChecked()
        self.confirm.setEnabled(encrypting)
        self.pwid.setEnabled(encrypting)
        self.cipher.setEnabled(encrypting)

    def _ok(self):
        pw = self.password.text()
        if not pw:
            QMessageBox.warning(self, "Missing password", "Enter a password.")
            return
        if self.encrypt_radio.isChecked():
            if pw != self.confirm.text():
                QMessageBox.warning(self, "Mismatch", "Passwords do not match.")
                return
            pwid = self.pwid.text().strip()
            if not pwid:
                QMessageBox.warning(self, "Missing ID", "Enter a password identifier.")
                return
            self.command = encrypt_args(self._medium.uuid, pw, pwid, self.cipher.currentText())
        else:
            self.command = decrypt_args(self._medium.uuid, pw)
        self.accept()


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
        layout.addRow("Port:", self.port_spin)

        self.device_spin = QSpinBox()
        self.device_spin.setRange(0, 1)
        layout.addRow("Device:", self.device_spin)

        self.status = QLabel("")
        self.status.setStyleSheet("color:#888;")
        layout.addRow(self.status)

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

    def _ok(self):
        if self.vm_combo.currentData() is None:
            QMessageBox.warning(self, "No VM", "No virtual machine selected.")
            return
        if self.ctl_combo.currentData() is None:
            QMessageBox.warning(self, "No controller", "This VM has no storage controller to attach to.")
            return
        self.accept()

    def args(self) -> list[str]:
        return attach_args(
            self.vm_combo.currentData(),
            self.ctl_combo.currentData(),
            self.port_spin.value(),
            self.device_spin.value(),
            self._kind,
            self._medium.uuid,
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
        self.setWindowTitle("VBoxFront — VBoxManage GUI")
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
        self.act_attach = self._action("Attach to VM…", self.attach_selected)
        self.act_detach = self._action("Detach from VM…", self.detach_selected)
        self.act_remove = self._action("Remove…", self.remove_selected, QKeySequence.StandardKey.Delete)
        self.act_copy_uuid = self._action("Copy UUID", self.copy_uuid_selected)
        self.act_copy_path = self._action("Copy location", self.copy_path_selected)
        self.act_open_dir = self._action("Open containing folder", self.open_folder_selected)
        self.act_settings = self._action("Settings…", self.open_settings)
        act_quit = self._action("Quit", self.close, "Ctrl+Q")

        menu_file = self.menuBar().addMenu("&File")
        menu_file.addAction(self.act_settings)
        menu_file.addSeparator()
        menu_file.addAction(act_quit)

        menu_media = self.menuBar().addMenu("&Media")
        menu_media.addAction(self.act_refresh)
        menu_media.addSeparator()
        menu_media.addAction(self.act_create)
        menu_media.addAction(self.act_add)
        menu_media.addAction(self.act_fromraw)
        menu_media.addAction(self.act_compact_all)
        menu_media.addSeparator()
        for act in (self.act_info, self.act_compact, self.act_resize, self.act_convert,
                    self.act_props, self.act_move, self.act_encrypt,
                    self.act_attach, self.act_detach):
            menu_media.addAction(act)
        menu_media.addSeparator()
        menu_media.addAction(self.act_remove)

        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        for act in (self.act_refresh, self.act_create, self.act_add, self.act_fromraw):
            toolbar.addAction(act)
        toolbar.addSeparator()
        for act in (self.act_info, self.act_compact, self.act_resize, self.act_convert):
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

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.runner = CommandRunner()
        self.runner.finished.connect(self._on_runner_finished)
        splitter.addWidget(self.tabs)
        splitter.addWidget(self.runner)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        self.setCentralWidget(splitter)
        self.setStatusBar(QStatusBar())
        self._update_actions()

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
                    self.act_encrypt):
            act.setEnabled(is_disk)

    def _show_context_menu(self, pane: MediaPane, pos):
        menu = QMenu(self)
        for act in (self.act_info, self.act_props, self.act_attach, self.act_detach):
            menu.addAction(act)
        menu.addSeparator()
        for act in (self.act_compact, self.act_resize, self.act_convert,
                    self.act_move, self.act_encrypt):
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
        if not vboxmanage_path():
            QMessageBox.warning(
                self,
                "VBoxManage missing",
                "Could not find `VBoxManage` in PATH. Install VirtualBox, add "
                "it to your PATH, or set its location in File → Settings.",
            )

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
        for kind in KINDS:
            for path in load_library(kind):
                self._chain.append((["showmediuminfo", kind, path], self._library_open_done(kind, path)))
        for kind in KINDS:
            self._chain.append((list_media_args(kind), self._list_done(kind)))
        self._advance_chain()

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
        dlg = CreateDiskDialog(self)
        if dlg.exec() and self._require_idle():
            self.runner.run(dlg.args())

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
        vdis = [r for r in self.media["disk"] if r.fmt.upper() == "VDI"
                and r.state.lower() != "inaccessible"]
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

    def convert_selected(self):
        """Clone the selected disk to a different format (or same, defragmented)."""
        m = self._require_selection()
        if not m:
            return
        dlg = ConvertDialog(m, self)
        if dlg.exec() and self._require_idle():
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
        src, _ = QFileDialog.getOpenFileName(
            self,
            "Select RAW image",
            os.path.expanduser("~"),
            "RAW images (*.img *.raw *.bin);;All files (*)",
        )
        if not src:
            return
        fmt, dst = ask_target_format_and_path(self, src, default_fmt="VDI")
        if not dst or not self._require_idle():
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
        args = dlg.args(self.current_kind())
        if not args:
            self.statusBar().showMessage("Properties unchanged.")
            return
        if self._require_idle():
            self.runner.run(args)

    def move_selected(self):
        m = self._require_selection()
        if not m:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Move disk to", m.location,
            f"{m.fmt} (*{DISK_EXT.get(m.fmt.upper(), '')});;All files (*)",
        )
        if not path or path == m.location:
            return
        if not self._require_idle():
            return
        self._pending_library_move = (m.location, path)
        self.runner.run(move_args(m.uuid, path))

    def encrypt_selected(self):
        m = self._require_selection()
        if not m:
            return
        dlg = EncryptDialog(m, self)
        if dlg.exec() and dlg.command and self._require_idle():
            args, stdin_data = dlg.command
            self.runner.run(args, stdin_data)

    def attach_selected(self):
        m = self._require_selection()
        if not m:
            return

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
                dlg = AttachDialog(self.current_kind(), m, vms, running, self._capture, self)
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
        dlg = RemoveDialog(m, self)
        if not dlg.exec() or not self._require_idle():
            return
        kind = self.current_kind()
        args = ["closemedium", kind, m.uuid]
        if dlg.delete_file:
            args.append("--delete")
        self._pending_library_remove = (kind, m.location)
        self.runner.run(args)

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
            self.refresh_media()

    def _show_queue_status(self):
        if self._cmd_queue:
            self.statusBar().showMessage(f"Queue: {len(self._cmd_queue)} command(s) pending.")

    def _on_runner_finished(self, code: int):
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
    """Quick prompt for format + destination when converting from raw."""
    fmt, ok = QInputDialog.getItem(
        parent,
        "Target format",
        "Convert to:",
        [f for f in DISK_FORMATS if f != "RAW"],
        0 if default_fmt not in DISK_FORMATS else DISK_FORMATS.index(default_fmt),
        False,
    )
    if not ok:
        return None, None
    base, _ = os.path.splitext(src_path)
    dst_default = base + DISK_EXT.get(fmt, ".vdi")
    dst, _ = QFileDialog.getSaveFileName(
        parent,
        "Save converted image as",
        dst_default,
        f"{fmt} (*{DISK_EXT.get(fmt, '')});;All files (*)",
    )
    if not dst:
        return None, None
    if os.path.exists(dst):
        ret = QMessageBox.question(
            parent, "Overwrite?", f"{dst} exists. Overwrite?"
        )
        if ret != QMessageBox.StandardButton.Yes:
            return None, None
        try:
            os.remove(dst)
        except OSError as e:
            QMessageBox.critical(parent, "Error", str(e))
            return None, None
    return fmt, dst


def main():
    app = QApplication(sys.argv)
    app.setOrganizationName("vboxfront")
    app.setApplicationName("vboxfront")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
