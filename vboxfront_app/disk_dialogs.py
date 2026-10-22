"""VBoxFront disk dialogs."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
)
import os

from .commands import (
    clone_args,
    convertfromraw_stdin_args,
    create_diff_args,
    create_disk_args,
    create_floppy_args,
    resize_args,
)
from .compression import (
    compressor_for,
    measure_uncompressed,
    open_maybe_compressed,
    uncompressed_size,
)
from .constants import (
    CAP_DIFFERENCING,
    DISK_EXT,
    DISK_FORMATS,
    DISK_VARIANTS,
    KIND_META,
    VMDK_VARIANTS,
)
from .domain import (
    allocation_estimate,
    disk_backends,
    format_bytes,
    format_mbytes,
    free_bytes_for,
    parse_byte_size,
    parse_capacity_mb,
    registered_at,
    same_file,
)
from .measurement import ArchiveMeasureTask, cache as measurement_cache
from .models import MediumBackend, MediumRecord
from .persistence import default_image_path, remember_folder, remembered_folder
from .theme import color_css, muted_color, warning_color
from .widgets import ElidingLabel, confirm_space


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
        self.path_edit = QLineEdit(default_image_path("create", "new-disk.vdi"))
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

        self.space = QLabel("")
        self.space.setStyleSheet(color_css(muted_color()))
        layout.addRow("", self.space)
        self.path_edit.textChanged.connect(self._update_space)
        self.variant_combo.currentTextChanged.connect(self._update_space)
        self._update_human(self.spin.value())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _update_human(self, mb: int):
        self.human.setText(format_mbytes(mb))
        self._update_space()

    def _update_space(self, *_args):
        """Free space on the target filesystem, next to what this will take.

        A Fixed image writes its whole size immediately, so the two figures are
        worth seeing together before the command runs rather than after it has
        filled the disk.
        """
        path = self.path_edit.text().strip()
        if not path:
            self.space.setText("")
            return
        free = free_bytes_for(path)
        needed = allocation_estimate(self.spin.value(), self.variant_combo.currentText())
        if free is None:
            self.space.setText("")
            return
        text = f"{format_bytes(free)} free, about {format_bytes(needed)} needed"
        self.space.setText(text)
        self.space.setStyleSheet(color_css(
            warning_color() if needed > free else muted_color()))

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
        if not confirm_space(
            self, path,
            allocation_estimate(self.spin.value(), self.variant_combo.currentText()),
        ):
            return
        remember_folder("create", path)
        self.path = path
        self.size_mb = self.spin.value()
        self.fmt = self.fmt_combo.currentText()
        self.variant = self.variant_combo.currentText()
        self.accept()

    def args(self) -> list[str]:
        return create_disk_args(self.path, self.size_mb, self.fmt, self.variant)


class ResizeDialog(QDialog):
    """Grow a disk, in MB or GB — or to an exact byte count.

    Two ways in, because they answer different questions: a unit spin for "make
    it 40 GB", and an exact size for "make it match that other image", which
    has to go through --resizebyte since --resize only speaks whole megabytes.
    """

    def __init__(self, disk: MediumRecord, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Resize disk")
        self.size_mb = 0
        self.size_bytes: int | None = None
        self._disk = disk
        self._current_mb = parse_capacity_mb(disk.capacity)

        layout = QFormLayout(self)
        layout.addRow(QLabel(f"<b>{os.path.basename(disk.location)}</b>"))
        layout.addRow("Current capacity:", QLabel(
            f"{format_mbytes(self._current_mb) or '?'}"
            f"{f'  ({disk.capacity})' if disk.capacity else ''}"
        ))

        size_row = QHBoxLayout()
        self.spin = QSpinBox()
        self.spin.setRange(1, 4 * 1024 * 1024)  # 4 TB expressed in MB
        self.spin.setValue(self._current_mb or 10240)
        self.spin.valueChanged.connect(self._update_delta)
        # VBoxManage --resize takes megabytes, matching the "MBytes" that
        # `list hdds` reports, so MB is the base unit and GB is a multiplier on
        # top of it — never a separate notion of size.
        self.unit = QComboBox()
        self.unit.addItem("MB", 1)
        self.unit.addItem("GB", 1024)
        self.unit.currentIndexChanged.connect(self._on_unit_changed)
        size_row.addWidget(self.spin, 1)
        size_row.addWidget(self.unit)
        layout.addRow("New size:", size_row)

        self.exact_check = QCheckBox("Exact size (--resizebyte)")
        self.exact_check.toggled.connect(self._on_exact_toggled)
        layout.addRow("", self.exact_check)
        self.exact_edit = QLineEdit()
        self.exact_edit.setPlaceholderText("e.g. 21474836480, 20G, 0x500000000")
        self.exact_edit.setEnabled(False)
        self.exact_edit.textChanged.connect(self._update_delta)
        layout.addRow("Bytes:", self.exact_edit)

        self.delta = QLabel("")
        self.delta.setWordWrap(True)
        layout.addRow("", self.delta)
        layout.addRow(
            QLabel("<i>VBoxManage can only grow disks, not shrink them.</i>")
        )

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Resize")
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        self._update_delta()

    def _on_unit_changed(self, *_args):
        """Keep the size itself put when the unit changes.

        Switching MB→GB on a 20480 in the box otherwise silently asks for
        20480 GB, which the spin will happily accept.
        """
        # Read the value *before* narrowing the range: setRange clamps, so a
        # 20480 MB box would already be 4096 by the time it was divided.
        value = self.spin.value()
        if self.unit.currentData() == 1024:
            self.spin.setRange(1, 4 * 1024)
            self.spin.setValue(max(1, round(value / 1024)))
        else:
            self.spin.setRange(1, 4 * 1024 * 1024)
            self.spin.setValue(min(4 * 1024 * 1024, value * 1024))
        self._update_delta()

    def _on_exact_toggled(self, checked: bool):
        self.exact_edit.setEnabled(checked)
        self.spin.setEnabled(not checked)
        self.unit.setEnabled(not checked)
        self._update_delta()

    def target_bytes(self) -> int | None:
        if self.exact_check.isChecked():
            return parse_byte_size(self.exact_edit.text())
        return self.spin.value() * self.unit.currentData() * 1024 * 1024

    def _update_delta(self, *_args):
        target = self.target_bytes()
        if target is None:
            self.delta.setText(
                f"<span style=\"{color_css(warning_color())}\">Not a size.</span>")
            return
        current = (self._current_mb or 0) * 1024 * 1024
        if not current:
            self.delta.setText(f"New capacity {format_bytes(target)}.")
            self.delta.setStyleSheet(color_css(muted_color()))
            return
        change = target - current
        if change > 0:
            self.delta.setText(
                f"{format_bytes(current)} → {format_bytes(target)} "
                f"(+{format_bytes(change)})"
            )
            self.delta.setStyleSheet(color_css(muted_color()))
        elif change == 0:
            self.delta.setText("Same size as now — nothing to do.")
            self.delta.setStyleSheet(color_css(warning_color()))
        else:
            self.delta.setText(
                f"Smaller than the current {format_bytes(current)}. VirtualBox "
                "refuses to shrink an image; clone it into a smaller one instead."
            )
            self.delta.setStyleSheet(color_css(warning_color()))

    def _ok(self):
        target = self.target_bytes()
        if target is None or target <= 0:
            QMessageBox.warning(self, "Size", "Enter a size, e.g. 20G or 21474836480.")
            return
        current = (self._current_mb or 0) * 1024 * 1024
        if current and target < current:
            QMessageBox.warning(
                self, "Cannot shrink",
                f"{format_bytes(target)} is smaller than the current "
                f"{format_bytes(current)}. VirtualBox only grows images "
                "(VERR_NOT_SUPPORTED); clone this disk into a smaller one "
                "instead.",
            )
            return
        if current and target == current:
            QMessageBox.information(
                self, "No change", "That is the size it already is."
            )
            return
        # Only the growth has to fit: the blocks that are already allocated are
        # not written again.
        if not confirm_space(self, self._disk.location, target - current):
            return
        if self.exact_check.isChecked() and target % (1024 * 1024):
            self.size_bytes = target
        else:
            self.size_mb = target // (1024 * 1024)
        self.accept()

    def args(self, uuid: str) -> list[str]:
        return resize_args(uuid, size_mb=self.size_mb, size_bytes=self.size_bytes)


class ConvertDialog(QDialog):
    def __init__(self, disk: MediumRecord, backends: list[MediumBackend] | None = None,
                 parent=None, known: list[MediumRecord] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Convert / Clone disk")
        #: Every registered medium, so a target that is one of them can be
        #: named rather than silently deleted on the way to the clone.
        self._known = known or []
        self.target_format = "VDI"
        self.target_variant = "Standard"
        self.target_path = ""
        self.existing = False
        # The caller preserves an existing target (clonemedium refuses to
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
        self.fmt.currentTextChanged.connect(self._on_format_changed)
        layout.addRow("Target format:", self.fmt)

        # The variant is the target's, not the source's: cloning is the one
        # place a dynamic image can be turned into a fixed one (or split).
        self.variant = QComboBox()
        self.variant.addItems(self._variants_for(self.fmt.currentText()))
        layout.addRow("Target variant:", self.variant)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        layout.addRow("Target file:", path_row)

        # --existing clones *into* a file that is already there, keeping its
        # size and variant. It is the only way to clone into a pre-allocated
        # image, and the only case where an existing target is not an error.
        self.existing_check = QCheckBox(
            "Clone into an existing image (--existing)"
        )
        self.existing_check.toggled.connect(self._on_existing_toggled)
        layout.addRow("", self.existing_check)

        self.space = QLabel("")
        self.space.setStyleSheet(color_css(muted_color()))
        layout.addRow("", self.space)

        self._src = disk
        self._update_default_path(self.fmt.currentText())
        self.path_edit.textChanged.connect(self._update_space)
        self.variant.currentTextChanged.connect(self._update_space)
        self._update_space()

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

    def _variants_for(self, fmt: str) -> list[str]:
        backend = self._backends.get(fmt)
        if backend:
            return backend.variants()
        return VMDK_VARIANTS if fmt == "VMDK" else DISK_VARIANTS

    def _on_format_changed(self, fmt: str):
        variants = self._variants_for(fmt)
        current = self.variant.currentText()
        self.variant.clear()
        self.variant.addItems(variants)
        if current in variants:
            self.variant.setCurrentText(current)

    def _on_existing_toggled(self, checked: bool):
        # Both belong to the target file that already exists; VirtualBox reuses
        # its geometry rather than being told a new one.
        self.variant.setEnabled(not checked)
        self.fmt.setEnabled(not checked)
        self._update_space()

    def _update_space(self, *_args):
        path = self.path_edit.text().strip()
        if not path or self.existing_check.isChecked():
            self.space.setText("")
            return
        free = free_bytes_for(path)
        if free is None:
            self.space.setText("")
            return
        needed = self._needed_bytes()
        self.space.setText(
            f"{format_bytes(free)} free, about {format_bytes(needed)} needed"
        )
        self.space.setStyleSheet(color_css(
            warning_color() if needed > free else muted_color()))

    def _needed_bytes(self) -> int:
        """What the clone will occupy: its whole capacity when the target is
        fixed, otherwise no more than the source has actually allocated."""
        capacity = parse_capacity_mb(self._src.capacity) or 0
        allocated = parse_capacity_mb(self._src.size_on_disk)
        if "fixed" in self.variant.currentText().lower():
            return allocation_estimate(capacity, "Fixed")
        return (allocated if allocated is not None else capacity) * 1024 * 1024

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
        if self.existing_check.isChecked():
            if not exists:
                QMessageBox.warning(
                    self, "No such image",
                    "--existing clones into an image that is already there. "
                    "Create the target first, or clear the checkbox.",
                )
                return
            self.overwrite = False
        else:
            if exists:
                occupant = registered_at(path, self._known, exclude=self._src.uuid)
                if occupant and not self._confirm_registered_target(path, occupant):
                    return
                ret = QMessageBox.question(
                    self, "Overwrite?", f"{path} already exists. Overwrite?"
                )
                if ret != QMessageBox.StandardButton.Yes:
                    return
            if not confirm_space(self, path, self._needed_bytes()):
                return
            self.overwrite = exists
        remember_folder("convert", path)
        self.existing = self.existing_check.isChecked()
        self.target_format = self.fmt.currentText()
        self.target_variant = self.variant.currentText()
        self.target_path = path
        self.accept()

    def _confirm_registered_target(self, path: str, occupant: MediumRecord) -> bool:
        """Say which registered medium the target is before overwriting it."""
        ret = QMessageBox.warning(
            self, "Target is a registered medium",
            f"{path} is the backing file of a medium VirtualBox has "
            f"registered:\n\n{os.path.basename(occupant.location) or occupant.uuid}"
            f"\nUUID {occupant.uuid}\n\nOverwriting it deletes that image and "
            "leaves the registry entry pointing at a file that is no longer "
            "the medium it names. Remove it from VirtualBox first, or choose "
            "another target.\n\nOverwrite it anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return ret == QMessageBox.StandardButton.Yes

    def args(self, uuid: str) -> list[str]:
        # Neither format nor variant is sent with --existing: the target image
        # already has both, and VirtualBox keeps them.
        if self.existing:
            return clone_args("disk", uuid, self.target_path, existing=True)
        return clone_args("disk", uuid, self.target_path,
                          fmt=self.target_format, variant=self.target_variant)


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
        remember_folder("diff", path)
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
        self.path_edit = QLineEdit(default_image_path("floppy", "floppy.img"))
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
        remember_folder("floppy", path)
        self.path = path
        self.size_mb = self.spin.value()
        self.formatted = self.format_check.isChecked()
        self.accept()

    def args(self) -> list[str]:
        return create_floppy_args(self.path, self.size_mb, self.formatted)


class ImportCompressedDialog(QDialog):
    """Import a compressed raw image without unpacking it first.

    `convertfromraw stdin <target> <bytes>` reads the image off standard
    input, so a 40 GB `.img.gz` never has to exist as a 40 GB file — the app
    decompresses straight into the child. The catch is the byte count, which
    is mandatory and is the *uncompressed* length: gzip records one (below
    4 GiB), nothing else does, and the rest have to be measured by a streaming
    pass over the archive.
    """

    def __init__(self, source: str, backends: list[MediumBackend] | None = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import compressed raw image")
        self._source = source
        self.target = ""
        self.size_bytes = 0
        self.fmt = "VDI"
        self.variant = "Standard"
        self._backends = {b.id: b for b in disk_backends(backends or [])}
        self._measure_worker = None

        layout = QFormLayout(self)
        layout.addRow("Archive:", ElidingLabel(source))
        compressed = os.path.getsize(source) if os.path.isfile(source) else 0
        opener = compressor_for(source)
        layout.addRow("Compression:", QLabel(
            os.path.splitext(source)[1].lstrip(".") if opener else "none (plain image)"
        ))
        layout.addRow("On disk:", QLabel(format_bytes(compressed) or "?"))

        size_row = QHBoxLayout()
        self.size_edit = QLineEdit()
        self.size_edit.setPlaceholderText("uncompressed bytes — e.g. 40G")
        self.measure_btn = QPushButton("Measure…")
        self.measure_btn.setToolTip(
            "Stream the archive once and count the bytes. Reads the "
            "compressed file and writes nothing."
        )
        self.measure_btn.clicked.connect(self._measure)
        size_row.addWidget(self.size_edit, 1)
        size_row.addWidget(self.measure_btn)
        layout.addRow("Uncompressed size:", size_row)

        self.size_note = QLabel("")
        self.size_note.setWordWrap(True)
        layout.addRow("", self.size_note)

        self.fmt_combo = QComboBox()
        self.fmt_combo.addItems(
            list(self._backends) or [f for f in DISK_FORMATS if f != "RAW"]
        )
        if "VDI" in [self.fmt_combo.itemText(i) for i in range(self.fmt_combo.count())]:
            self.fmt_combo.setCurrentText("VDI")
        self.fmt_combo.currentTextChanged.connect(self._on_format_changed)
        layout.addRow("Target format:", self.fmt_combo)

        self.variant_combo = QComboBox()
        self.variant_combo.addItems(self._variants_for(self.fmt_combo.currentText()))
        layout.addRow("Target variant:", self.variant_combo)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(self._default_target())
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        layout.addRow("Target file:", path_row)

        layout.addRow(QLabel(
            "<i>The archive is decompressed straight into VBoxManage, so no "
            "unpacked copy is ever written. The byte count has to be exact: "
            "an incorrect size fails the import and may leave partial "
            "output for inspection in Operation recovery.</i>"
        ))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Import")
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

        self.finished.connect(self._stop_measurement)
        self._seed_size()

    def _seed_size(self):
        """Fill in the size when it can be had without reading the archive."""
        known = measurement_cache.get(self._source)
        if known is None:
            known = uncompressed_size(self._source)
        if known:
            self.size_edit.setText(str(known))
            self.size_note.setText(
                f"<i>{format_bytes(known)}, from the source file or cached measurement.</i>"
            )
            self.size_note.setStyleSheet(color_css(muted_color()))
        else:
            self.size_note.setText(
                "<i>The full uncompressed size cannot be trusted from this "
                "container's metadata. Measure it, or type it if you "
                "already know it.</i>"
            )
            self.size_note.setStyleSheet(color_css(warning_color()))

    def _measure(self):
        if self._measure_worker is not None:
            self._stop_measurement()
            return
        self.size_edit.clear()
        self.measure_btn.setText("Cancel measurement")
        self._measure_worker = ArchiveMeasureTask(self._source)
        self._measure_worker.progress.connect(self._measurement_progress)
        self._measure_worker.measured.connect(self._measurement_done)
        self._measure_worker.start()

    def _measurement_progress(self, total):
        self.size_note.setText(f"Measured {format_bytes(total)} so far…")

    def _measurement_done(self, measured):
        worker, self._measure_worker = self._measure_worker, None
        cancelled = worker is None or worker.isInterruptionRequested()
        self.measure_btn.setEnabled(True)
        self.measure_btn.setText("Measure…")
        if measured is None or cancelled:
            self.size_note.setText("Measurement cancelled, source changed, or archive could not be read.")
            return
        measurement_cache.put(worker.stamp, measured)
        self.size_edit.setText(str(measured))
        self.size_note.setText(f"{format_bytes(measured)}, measured.")

    def _stop_measurement(self, *_args):
        if self._measure_worker is not None:
            self._measure_worker.requestInterruption()
            self.measure_btn.setText("Cancelling…")
            self.measure_btn.setEnabled(False)

    def _variants_for(self, fmt: str) -> list[str]:
        backend = self._backends.get(fmt)
        if backend:
            return backend.variants()
        return VMDK_VARIANTS if fmt == "VMDK" else DISK_VARIANTS

    def _extension_for(self, fmt: str) -> str:
        backend = self._backends.get(fmt)
        if backend:
            return backend.extension_for("HardDisk") or ".vdi"
        return DISK_EXT.get(fmt, ".vdi")

    def _default_target(self) -> str:
        # Strip both the compression suffix and the image one: "disk.img.gz"
        # should suggest "disk.vdi", not "disk.img.vdi".
        base = self._source
        if compressor_for(base):
            base = os.path.splitext(base)[0]
        base = os.path.splitext(base)[0]
        name = os.path.basename(base) + self._extension_for("VDI")
        return os.path.join(remembered_folder("import"), name)

    def _on_format_changed(self, fmt: str):
        variants = self._variants_for(fmt)
        current = self.variant_combo.currentText()
        self.variant_combo.clear()
        self.variant_combo.addItems(variants)
        if current in variants:
            self.variant_combo.setCurrentText(current)
        base, ext = os.path.splitext(self.path_edit.text())
        if ext:
            self.path_edit.setText(base + self._extension_for(fmt))

    def _browse(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save imported image as", self.path_edit.text(),
            KIND_META["disk"]["filter"],
        )
        if path:
            self.path_edit.setText(path)

    def _ok(self):
        if self._measure_worker is not None:
            QMessageBox.information(self, "Measuring", "Wait for measurement or cancel it first.")
            return
        size = parse_byte_size(self.size_edit.text())
        if not size:
            QMessageBox.warning(
                self, "Size needed",
                "convertfromraw needs the uncompressed size up front. "
                "Measure it, or enter it.",
            )
            return
        path = self.path_edit.text().strip()
        if not path:
            QMessageBox.warning(self, "Missing path", "Choose a target file.")
            return
        if same_file(path, self._source):
            QMessageBox.warning(
                self, "Same file",
                "The target is the archive being read. Choose another file.",
            )
            return
        if os.path.exists(path):
            QMessageBox.warning(
                self, "File exists",
                "convertfromraw refuses to overwrite; choose a new path.",
            )
            return
        if not confirm_space(self, path, size):
            return
        remember_folder("import", path)
        self.size_bytes = size
        self.target = path
        self.fmt = self.fmt_combo.currentText()
        self.variant = self.variant_combo.currentText()
        self.accept()

    def args(self) -> list[str]:
        return convertfromraw_stdin_args(
            self.target, self.size_bytes, self.fmt, self.variant
        )

    def open_source(self):
        """The decompressing stream the runner feeds to the child."""
        return open_maybe_compressed(self._source)


class BatchConvertDialog(QDialog):
    """Clone several disks into one folder in a single queued run."""

    def __init__(self, disks: list[MediumRecord],
                 backends: list[MediumBackend] | None = None, parent=None,
                 known: list[MediumRecord] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Convert selected disks")
        self._disks = disks
        #: Every registered medium, for the same reason ConvertDialog keeps
        #: one: the caller preserves each existing target before its clone.
        self._known = known or []
        self._backends = {b.id: b for b in disk_backends(backends or [])}
        self.targets: list[tuple[MediumRecord, str]] = []
        self.overwrite: list[str] = []

        layout = QFormLayout(self)
        layout.addRow(QLabel(f"<b>{len(disks)} disk(s) selected</b>"))

        self.fmt = QComboBox()
        self.fmt.addItems(list(self._backends) or [f for f in DISK_FORMATS if f != "RAW"])
        self.fmt.currentTextChanged.connect(self._on_format_changed)
        layout.addRow("Target format:", self.fmt)

        self.variant = QComboBox()
        self.variant.addItems(self._variants_for(self.fmt.currentText()))
        layout.addRow("Target variant:", self.variant)

        dir_row = QHBoxLayout()
        self.dir_edit = QLineEdit(os.path.dirname(disks[0].location) if disks else "")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(browse)
        layout.addRow("Target folder:", dir_row)

        self.suffix = QLineEdit("-converted")
        layout.addRow("Name suffix:", self.suffix)

        self.preview = QListWidget()
        self.preview.setMaximumHeight(140)
        layout.addRow("Will write:", self.preview)
        for widget in (self.dir_edit, self.suffix):
            widget.textChanged.connect(self._update_preview)
        self.fmt.currentTextChanged.connect(self._update_preview)
        self._update_preview()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Convert")
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _variants_for(self, fmt: str) -> list[str]:
        backend = self._backends.get(fmt)
        if backend:
            return backend.variants()
        return VMDK_VARIANTS if fmt == "VMDK" else DISK_VARIANTS

    def _on_format_changed(self, fmt: str):
        variants = self._variants_for(fmt)
        current = self.variant.currentText()
        self.variant.clear()
        self.variant.addItems(variants)
        if current in variants:
            self.variant.setCurrentText(current)

    def _extension_for(self, fmt: str) -> str:
        backend = self._backends.get(fmt)
        if backend:
            return backend.extension_for("HardDisk") or ".img"
        return DISK_EXT.get(fmt, ".img")

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "Target folder", self.dir_edit.text())
        if path:
            self.dir_edit.setText(path)

    def _plan(self) -> list[tuple[MediumRecord, str]]:
        folder = self.dir_edit.text().strip()
        suffix = self.suffix.text()
        ext = self._extension_for(self.fmt.currentText())
        plan = []
        for disk in self._disks:
            base = os.path.splitext(os.path.basename(disk.location))[0] or disk.uuid
            plan.append((disk, os.path.join(folder, base + suffix + ext)))
        return plan

    def _update_preview(self, *_args):
        self.preview.clear()
        for _disk, target in self._plan():
            item = QListWidgetItem(target)
            if os.path.exists(target):
                item.setForeground(warning_color())
                item.setToolTip("Exists — original will be preserved until the clone succeeds")
            self.preview.addItem(item)

    def _ok(self):
        folder = self.dir_edit.text().strip()
        if not os.path.isdir(folder):
            QMessageBox.warning(self, "No such folder", f"{folder} is not a folder.")
            return
        plan = self._plan()
        targets = [t for _d, t in plan]
        if len(set(targets)) != len(targets):
            QMessageBox.warning(
                self, "Name collision",
                "Two sources would be written to the same target file. Give "
                "them a different suffix or convert them separately.",
            )
            return
        # Against *every* source, not just each target's own: the caller
        # replaces an existing target during its clone, so a plan
        # aimed at another source could destroy that source before it is read.
        # Re-running a convert over its own output folder can cause this.
        clashes = [
            (target, other)
            for _disk, target in plan
            for other in self._disks
            if same_file(target, other.location)
        ]
        if clashes:
            target, other = clashes[0]
            QMessageBox.warning(
                self, "Same file",
                f"{os.path.basename(target)} is the source disk "
                f"{os.path.basename(other.location) or other.uuid}, which is "
                "in this selection. It would be deleted before its own clone "
                "ran. Choose another folder or suffix.",
            )
            return
        existing = [t for t in targets if os.path.exists(t)]
        occupied = [
            (t, rec) for t in existing
            if (rec := registered_at(t, self._known)) is not None
        ]
        if occupied:
            listing = "\n".join(
                f"{os.path.basename(t)} — {os.path.basename(rec.location) or rec.uuid} "
                f"(UUID {rec.uuid})"
                for t, rec in occupied[:8]
            )
            ret = QMessageBox.warning(
                self, "Targets are registered media",
                f"{len(occupied)} target file(s) are the backing files of "
                f"media VirtualBox has registered:\n\n{listing}"
                + ("\n…" if len(occupied) > 8 else "")
                + "\n\nOverwriting them deletes those images and leaves their "
                "registry entries pointing at files that are no longer the "
                "media they name.\n\nOverwrite them anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        if existing:
            ret = QMessageBox.question(
                self, "Overwrite?",
                f"{len(existing)} target file(s) already exist and will be "
                "moved aside until the clone that replaces them succeeds:\n\n"
                + "\n".join(existing[:8])
                + ("\n…" if len(existing) > 8 else ""),
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        needed = 0
        for disk, _target in plan:
            capacity = parse_capacity_mb(disk.capacity) or 0
            allocated = parse_capacity_mb(disk.size_on_disk)
            if "fixed" in self.variant.currentText().lower():
                needed += allocation_estimate(capacity, "Fixed")
            else:
                needed += (allocated if allocated is not None else capacity) * 1024 * 1024
        if not confirm_space(self, os.path.join(folder, "x"), needed):
            return
        remember_folder("convert", folder)
        self.targets = plan
        self.overwrite = existing
        self.accept()

    def commands(self) -> list[list[str]]:
        variant = self.variant.currentText()
        fmt = self.fmt.currentText()
        return [
            clone_args("disk", disk.uuid, target, fmt=fmt, variant=variant)
            for disk, target in self.targets
        ]
