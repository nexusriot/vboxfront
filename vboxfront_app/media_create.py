"""MainWindow actions. Bringing media in and writing new images: create, add, import, export.

Mixed into `window.MainWindow`; methods use its widgets, runner and helpers.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QFileDialog, QInputDialog, QMessageBox
import os

from .commands import export_raw_args
from .constants import DISK_EXT, DISK_FORMATS, KIND_META
from .disk_dialogs import (
    CreateDiffDialog,
    CreateDiskDialog,
    CreateFloppyDialog,
    ImportCompressedDialog,
)
from .domain import (
    format_mbytes,
    is_differencing,
    parse_capacity_mb,
    registered_at,
    same_file,
)
from .persistence import add_library_path, remember_folder, remembered_folder
from .widgets import confirm_space


class MediaCreateMixin:
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

    def add_media_from_file(self):
        kind = self.current_kind()
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select media image",
            remembered_folder("library"),
            KIND_META[kind]["filter"],
        )
        if not path:
            return
        # `openmedium` is gone from modern VBoxManage and showmediuminfo only
        # registers transiently, so persistence lives in the app's library.
        remember_folder("library", path)
        add_library_path(kind, path)
        self.runner.note(f"[library] added {kind} {path}")
        self.refresh_media()

    def convert_from_raw(self, src: str = ""):
        if not self._require_idle():
            return
        if not src:
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
        occupant = registered_at(dst, [m for records in self.media.values() for m in records])
        if occupant:
            QMessageBox.warning(
                self, "Target is a registered medium",
                f"{dst} is the backing file of UUID {occupant.uuid}. Choose another file.",
            )
            return
        if overwrite and not self._clear_target(dst):
            return
        # convertfromraw only writes the file; remember it in the library once
        # it completes so the refresh picks it up.
        self._pending_library_add = ("disk", dst)
        self.runner.run(["convertfromraw", src, dst, "--format", fmt])

    def import_compressed(self):
        """Import a .gz/.bz2/.xz raw image straight into a managed format."""
        if not self._require_idle():
            return
        src, _ = QFileDialog.getOpenFileName(
            self, "Select a compressed raw image", remembered_folder("import"),
            "Compressed images (*.gz *.bz2 *.xz *.lzma *.img *.raw);;All files (*)",
        )
        if not src:
            return
        remember_folder("import", src)
        dlg = ImportCompressedDialog(src, self.backends, self)
        if not dlg.exec() or not self._require_idle():
            return
        try:
            stream = dlg.open_source()
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, "Cannot read", f"Could not open {src}: {e}")
            return
        # convertfromraw only writes the file; the library entry is what keeps
        # the result visible, exactly as for the uncompressed import.
        self._pending_library_add = ("disk", dlg.target)
        self._archive_context = (src, dlg.size_bytes)
        self.runner.run(dlg.args(), stdin_stream=stream,
                        stdin_total=dlg.size_bytes)

    def export_raw(self):
        """Clone a disk out to a flat raw image (`clonemedium --format RAW`)."""
        m = self._require_selection()
        if not m or not self._require_idle():
            return
        capacity = parse_capacity_mb(m.capacity) or 0
        base = os.path.splitext(os.path.basename(m.location))[0] or m.uuid
        path, _ = QFileDialog.getSaveFileName(
            self, "Export to raw image",
            os.path.join(remembered_folder("export"), base + ".img"),
            "Raw images (*.img *.raw);;All files (*)",
        )
        if not path:
            return
        if same_file(path, m.location):
            QMessageBox.warning(
                self, "Same file",
                "That is the disk being exported. Choose another file.",
            )
            return
        occupant = registered_at(path, [r for records in self.media.values() for r in records],
                                 exclude=m.uuid)
        if occupant:
            QMessageBox.warning(
                self, "Target is a registered medium",
                f"{path} is the backing file of "
                f"{os.path.basename(occupant.location) or occupant.uuid} "
                f"(UUID {occupant.uuid}). Choose another file.",
            )
            return
        # A raw image is flat: it occupies its whole capacity whatever the
        # source has actually allocated, which is the surprise worth naming.
        written = format_mbytes(capacity) or "the whole disk"
        ret = QMessageBox.question(
            self, "Export to raw",
            f"A raw image is flat — this writes {written} whatever the source "
            "has actually allocated, with no format and no compression.\n\n"
            f"Export {os.path.basename(m.location)}?",
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        overwrite = os.path.exists(path)
        if overwrite:
            if QMessageBox.question(
                self, "Overwrite?", f"{path} already exists. Overwrite?"
            ) != QMessageBox.StandardButton.Yes:
                return
        if not confirm_space(self, path, capacity * 1024 * 1024):
            return
        if not self._require_idle():
            return
        if overwrite and not self._clear_target(path):
            return
        remember_folder("export", path)
        self.runner.run(export_raw_args(m.uuid, path))


def ask_target_format_and_path(parent, src_path: str, default_fmt: str = "VDI"):
    """Prompt for format + destination when converting from raw.

    Returns (format, destination, overwrite). The destination is *not* touched
    here: the caller preserves an overwrite target before running the command.
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
