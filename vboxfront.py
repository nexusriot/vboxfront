#!/usr/bin/env python3
"""VBoxFront - PyQt6 frontend for VBoxManage disk image operations."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass

from PyQt6.QtCore import (
    QProcess,
    Qt,
    pyqtSignal,
    QEventLoop
)

from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

DISK_FORMATS = ["VDI", "VMDK", "VHD", "RAW"]
DISK_EXT = {"VDI": ".vdi", "VMDK": ".vmdk", "VHD": ".vhd", "RAW": ".img"}


@dataclass
class DiskRecord:
    uuid: str
    location: str
    fmt: str
    capacity: str
    size_on_disk: str


def vboxmanage_path() -> str | None:
    return shutil.which("VBoxManage") or shutil.which("vboxmanage")


def parse_hdd_list(text: str) -> list[DiskRecord]:
    """Parse `VBoxManage list hdds` blocks separated by blank lines."""
    records: list[DiskRecord] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        if not block.strip():
            continue
        kv: dict[str, str] = {}
        for line in block.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                kv[k.strip()] = v.strip()
        if "UUID" not in kv or "Location" not in kv:
            continue
        # `VBoxManage list hdds` reports the format as "Storage format" and has
        # no "Size on disk" field; accept the historical "Format" key too, and
        # fall back to the long-form "Size on disk" key when present (-l output).
        records.append(
            DiskRecord(
                uuid=kv.get("UUID", ""),
                location=kv.get("Location", ""),
                fmt=kv.get("Storage format") or kv.get("Format", ""),
                capacity=kv.get("Capacity", ""),
                size_on_disk=kv.get("Size on disk", ""),
            )
        )
    return records


class CommandRunner(QWidget):
    """A panel that runs VBoxManage commands and streams output."""

    finished = pyqtSignal(int)  # exit code

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc: QProcess | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        self.cmd_label = QLabel("Idle.")
        self.cmd_label.setStyleSheet("color:#888;")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel)
        header.addWidget(self.cmd_label, 1)
        header.addWidget(self.cancel_btn)
        layout.addLayout(header)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("Command output will appear here…")
        layout.addWidget(self.output)

    def is_busy(self) -> bool:
        return self.proc is not None and self.proc.state() != QProcess.ProcessState.NotRunning

    def run(self, args: list[str]) -> bool:
        if self.is_busy():
            return False
        vbm = vboxmanage_path()
        if not vbm:
            self.output.appendPlainText("[error] VBoxManage not found in PATH.\n")
            self.finished.emit(127)
            return False
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._on_output)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)

        pretty = " ".join(shlex.quote(a) for a in [vbm, *args])
        self.cmd_label.setText(f"Running: {pretty}")
        self.output.appendPlainText(f"$ {pretty}")
        self.cancel_btn.setEnabled(True)
        self.proc.start(vbm, args)
        return True

    def cancel(self):
        if self.is_busy():
            self.output.appendPlainText("[cancelled by user]")
            self.proc.kill()

    def _on_output(self):
        if not self.proc:
            return
        data = bytes(self.proc.readAllStandardOutput()).decode(errors="replace")
        if data:
            self.output.moveCursor(self.output.textCursor().MoveOperation.End)
            self.output.insertPlainText(data)
            self.output.moveCursor(self.output.textCursor().MoveOperation.End)

    def _on_error(self, error):
        # FailedToStart fires *instead of* finished, so reset state here.
        if error == QProcess.ProcessError.FailedToStart:
            self.output.appendPlainText(
                "[error] Failed to start VBoxManage (not executable or missing).\n"
            )
            self.cmd_label.setText("Idle.")
            self.cancel_btn.setEnabled(False)
            proc, self.proc = self.proc, None
            if proc:
                proc.deleteLater()
            self.finished.emit(126)

    def _on_finished(self, code: int, _status):
        self.output.appendPlainText(f"[exit {code}]\n")
        self.cmd_label.setText("Idle.")
        self.cancel_btn.setEnabled(False)
        proc, self.proc = self.proc, None
        if proc:
            proc.deleteLater()
        self.finished.emit(code)


class CaptureRunner(QProcess):
    """One-shot QProcess that buffers output and emits a callback."""

    def __init__(self, parent, on_done):
        super().__init__(parent)
        self._buf = bytearray()
        self._on_done = on_done
        self.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.readyReadStandardOutput.connect(self._read)
        self.finished.connect(self._done)

    def _read(self):
        self._buf += bytes(self.readAllStandardOutput())

    def _done(self, code, _status):
        self._on_done(code, self._buf.decode(errors="replace"))
        self.deleteLater()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VBoxFront — VBoxManage GUI")
        self.resize(1100, 720)

        self.disks: list[DiskRecord] = []
        # Path to register with `openmedium` once the current command succeeds
        # (used after `convertfromraw`, which only writes the file).
        self._pending_register: str | None = None

        self._build_ui()
        self._refresh_check_vbm()
        self.refresh_disks()

    def _build_ui(self):
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        act_refresh = QAction("Refresh", self)
        act_refresh.setShortcut(QKeySequence("F5"))
        act_refresh.triggered.connect(self.refresh_disks)
        toolbar.addAction(act_refresh)

        act_add = QAction("Add file…", self)
        act_add.triggered.connect(self.add_disk_from_file)
        toolbar.addAction(act_add)

        toolbar.addSeparator()

        act_compact = QAction("Compact", self)
        act_compact.triggered.connect(self.compact_selected)
        toolbar.addAction(act_compact)

        act_convert = QAction("Convert / Clone…", self)
        act_convert.triggered.connect(self.convert_selected)
        toolbar.addAction(act_convert)

        act_fromraw = QAction("Import RAW…", self)
        act_fromraw.triggered.connect(self.convert_from_raw)
        toolbar.addAction(act_fromraw)

        act_resize = QAction("Resize…", self)
        act_resize.triggered.connect(self.resize_selected)
        toolbar.addAction(act_resize)

        act_info = QAction("Info", self)
        act_info.triggered.connect(self.show_info_selected)
        toolbar.addAction(act_info)

        toolbar.addSeparator()

        act_remove = QAction("Remove…", self)
        act_remove.setShortcut(QKeySequence.StandardKey.Delete)
        act_remove.triggered.connect(self.remove_selected)
        toolbar.addAction(act_remove)

        # central splitter: table on top, runner below
        splitter = QSplitter(Qt.Orientation.Vertical)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Format", "Capacity", "Size on disk", "Location", "UUID"]
        )
        self.table.setSelectionBehavior(self.table.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(self.table.SelectionMode.SingleSelection)
        self.table.setEditTriggers(self.table.EditTrigger.NoEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for col in (0, 1, 2, 4):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.doubleClicked.connect(lambda *_: self.show_info_selected())

        self.runner = CommandRunner()
        self.runner.finished.connect(self._on_runner_finished)

        splitter.addWidget(self.table)
        splitter.addWidget(self.runner)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        self.setCentralWidget(splitter)
        self.setStatusBar(QStatusBar())

    def _refresh_check_vbm(self):
        if not vboxmanage_path():
            QMessageBox.warning(
                self,
                "VBoxManage missing",
                "Could not find `VBoxManage` in PATH. Install VirtualBox or "
                "add it to your PATH before performing operations.",
            )

    def selected_disk(self) -> DiskRecord | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.disks):
            return None
        return self.disks[row]

    def _require_idle(self) -> bool:
        if self.runner.is_busy():
            QMessageBox.information(
                self, "Busy", "Another VBoxManage command is still running."
            )
            return False
        return True

    def _require_selection(self) -> DiskRecord | None:
        d = self.selected_disk()
        if not d:
            QMessageBox.information(self, "No selection", "Select a disk first.")
            return None
        return d

    def refresh_disks(self):
        vbm = vboxmanage_path()
        if not vbm:
            self.statusBar().showMessage("VBoxManage not found.")
            return
        self.statusBar().showMessage("Listing disks…")
        proc = CaptureRunner(self, self._on_list_done)
        proc.start(vbm, ["list", "hdds"])

    def _on_list_done(self, code: int, output: str):
        if code != 0:
            self.statusBar().showMessage(f"list hdds failed (exit {code})")
            self.runner.output.appendPlainText(output)
            return
        prev = self.selected_disk()
        prev_uuid = prev.uuid if prev else None

        self.disks = parse_hdd_list(output)
        self.table.setRowCount(len(self.disks))
        for i, d in enumerate(self.disks):
            cells = [d.fmt, d.capacity, d.size_on_disk, d.location, d.uuid]
            for col, val in enumerate(cells):
                item = QTableWidgetItem(val)
                item.setToolTip(val)
                self.table.setItem(i, col, item)

        # Restore the previous selection so operations can be chained.
        if prev_uuid is not None:
            for i, d in enumerate(self.disks):
                if d.uuid == prev_uuid:
                    self.table.selectRow(i)
                    break
        self.statusBar().showMessage(f"{len(self.disks)} registered disk(s).")

    def add_disk_from_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select disk image",
            os.path.expanduser("~"),
            "Disk images (*.vdi *.vmdk *.vhd *.img *.raw);;All files (*)",
        )
        if not path or not self._require_idle():
            return
        self.runner.run(["openmedium", "disk", path])

    def show_info_selected(self):
        d = self._require_selection()
        if not d or not self._require_idle():
            return
        self.runner.run(["showmediuminfo", "disk", d.uuid])

    def compact_selected(self):
        d = self._require_selection()
        if not d:
            return
        if d.fmt.upper() != "VDI":
            ret = QMessageBox.question(
                self,
                "Compact",
                f"Compacting is normally only supported for VDI; this disk is "
                f"{d.fmt}. Try anyway?",
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        if not self._require_idle():
            return
        self.runner.run(["modifymedium", "disk", d.uuid, "--compact"])

    def resize_selected(self):
        d = self._require_selection()
        if not d:
            return
        dlg = ResizeDialog(d, self)
        if dlg.exec() and self._require_idle():
            self.runner.run(["modifymedium", "disk", d.uuid, "--resize", str(dlg.size_mb)])

    def convert_selected(self):
        """Clone the selected disk to a different format (or same, defragmented)."""
        d = self._require_selection()
        if not d:
            return
        dlg = ConvertDialog(d, self)
        if dlg.exec() and self._require_idle():
            self.runner.run(
                [
                    "clonemedium",
                    "disk",
                    d.uuid,
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
        # convertfromraw only creates the file; register it once it completes.
        self._pending_register = dst
        self.runner.run(["convertfromraw", src, dst, "--format", fmt])

    def remove_selected(self):
        d = self._require_selection()
        if not d:
            return
        dlg = RemoveDialog(d, self)
        if not dlg.exec() or not self._require_idle():
            return
        args = ["closemedium", "disk", d.uuid]
        if dlg.delete_file:
            args.append("--delete")
        self.runner.run(args)

    def _on_runner_finished(self, code: int):
        pending, self._pending_register = self._pending_register, None
        if pending and code == 0 and os.path.exists(pending):
            # Register the freshly converted image so it shows in the list.
            if self.runner.run(["openmedium", "disk", pending]):
                return  # refresh happens when this follow-up command finishes
        # Most operations change the registry; refresh quietly.
        self.refresh_disks()


class _LoopDialog(QWidget):
    """Base for the app's lightweight modal dialogs.

    Provides a blocking ``exec()`` that spins a local event loop and returns
    whether the dialog was accepted, so subclasses only define their widgets
    and set ``self._accepted`` in their OK handler.
    """

    def __init__(self, parent, title: str):
        super().__init__(parent, Qt.WindowType.Dialog)
        self.setWindowTitle(title)
        self._accepted = False

    def exec(self) -> bool:
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.show()
        loop = QEventLoop()
        self._loop = loop
        orig_close = self.closeEvent

        def closeEvent(ev):
            orig_close(ev)
            loop.quit()

        self.closeEvent = closeEvent  # type: ignore[assignment]
        loop.exec()
        return self._accepted


class ResizeDialog(_LoopDialog):
    """Modal-ish resize dialog using QMessageBox-style API."""

    def __init__(self, disk: DiskRecord, parent):
        super().__init__(parent, "Resize disk")
        self.size_mb = 0

        layout = QFormLayout(self)
        layout.addRow(QLabel(f"<b>{os.path.basename(disk.location)}</b>"))
        layout.addRow("Current capacity:", QLabel(disk.capacity or "?"))

        self.spin = QSpinBox()
        self.spin.setRange(1, 4 * 1024 * 1024)  # up to 4 TB
        # VBoxManage --resize takes megabytes (MB), matching the "MBytes" that
        # `list hdds` reports for capacity, so label it MB to avoid MiB/MB drift.
        self.spin.setSuffix(" MB")
        self.spin.setValue(self._guess_current_mb(disk.capacity))
        layout.addRow("New size:", self.spin)
        layout.addRow(
            QLabel("<i>Note: VBoxManage can only grow disks, not shrink them.</i>")
        )

        btns = QHBoxLayout()
        ok = QPushButton("Resize")
        cancel = QPushButton("Cancel")
        ok.clicked.connect(self._ok)
        cancel.clicked.connect(self.close)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        layout.addRow(btns)

    @staticmethod
    def _guess_current_mb(capacity: str) -> int:
        m = re.match(r"\s*(\d+)\s*MBytes", capacity or "")
        return int(m.group(1)) if m else 10240

    def _ok(self):
        self.size_mb = self.spin.value()
        self._accepted = True
        self.close()


class ConvertDialog(_LoopDialog):
    def __init__(self, disk: DiskRecord, parent):
        super().__init__(parent, "Convert / Clone disk")
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

        btns = QHBoxLayout()
        ok = QPushButton("Convert")
        cancel = QPushButton("Cancel")
        ok.clicked.connect(self._ok)
        cancel.clicked.connect(self.close)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        layout.addRow(btns)

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
        self._accepted = True
        self.close()


class RemoveDialog(_LoopDialog):
    """Unregister a disk, optionally deleting the backing file."""

    def __init__(self, disk: DiskRecord, parent):
        super().__init__(parent, "Remove disk")
        self.delete_file = False

        layout = QFormLayout(self)
        layout.addRow(QLabel(f"<b>{os.path.basename(disk.location)}</b>"))
        layout.addRow("Location:", QLabel(disk.location or "?"))
        layout.addRow("UUID:", QLabel(disk.uuid))

        self.mode = QComboBox()
        # Safe default first: unregister but keep the file on disk.
        self.mode.addItems(
            ["Unregister only (keep file)", "Delete file from disk"]
        )
        layout.addRow("Action:", self.mode)
        layout.addRow(
            QLabel(
                "<i>Unregister removes the disk from VirtualBox's media "
                "registry. Delete also erases the file permanently.</i>"
            )
        )

        btns = QHBoxLayout()
        ok = QPushButton("Remove")
        cancel = QPushButton("Cancel")
        ok.clicked.connect(self._ok)
        cancel.clicked.connect(self.close)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        layout.addRow(btns)

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
        self._accepted = True
        self.close()


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
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
