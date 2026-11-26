"""MainWindow actions. Acting on the selected media: inspect, compact, resize, convert, properties,
move, encryption, resolve and UUID tools.

Mixed into `window.MainWindow`; methods use its widgets, runner and helpers.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QFileDialog, QInputDialog, QLineEdit, QMessageBox
import os

from .commands import mediumio_formatfat_args, move_args, setlocation_args
from .constants import DISK_EXT, KINDS
from .disk_dialogs import BatchConvertDialog, ConvertDialog, ResizeDialog
from .domain import (
    child_names,
    is_inaccessible,
    parse_capacity_mb,
    registered_at,
    same_file,
)
from .media_dialogs import (
    ContentsDialog,
    EncryptDialog,
    MediumPropertyDialog,
    PropertiesDialog,
    ResolveDialog,
    UuidToolsDialog,
)
from .models import MediumRecord
from .persistence import add_library_path, load_library, remove_library_path


class MediaEditMixin:
    def show_contents_selected(self):
        m = self._require_selection()
        if not m or not self._require_idle():
            return
        kind = self.current_kind()
        dlg = ContentsDialog(kind, m, self)
        if not dlg.exec() or not dlg.command or not self._require_idle():
            return
        args, stdin_data = dlg.command
        output = next((a.partition("=")[2] for a in args if a.startswith("--output=")), "")
        occupant = registered_at(output, [r for records in self.media.values() for r in records]) if output else None
        if occupant:
            QMessageBox.warning(
                self, "Target is a registered medium",
                f"{output} is the backing file of UUID {occupant.uuid}. Choose another file.",
            )
            return
        self._run_single(args, stdin_data, title="extract a byte range")

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
        self._run_single(args, stdin_data, title="format as FAT")

    def show_info_selected(self):
        m = self._require_selection()
        if not m or not self._require_idle():
            return
        self.runner.run(["showmediuminfo", self.current_kind(), m.uuid])

    def compact_selected(self):
        disks = self._require_selection_multi()
        if not disks:
            return
        others = [d for d in disks if d.fmt.upper() != "VDI"]
        if others:
            names = ", ".join(os.path.basename(d.location) or d.uuid for d in others[:5])
            ret = QMessageBox.question(
                self,
                "Compact",
                f"Compacting is normally only supported for VDI. "
                f"{len(others)} of these are not ({names}"
                f"{'…' if len(others) > 5 else ''}). Try anyway?",
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        if len(disks) > 1:
            ret = QMessageBox.question(
                self, "Compact",
                f"Compact {len(disks)} disk(s) one after another?",
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        self._compact(disks)

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
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._compact(vdis)

    def _compact(self, disks: list[MediumRecord]):
        """Queue a compact per disk, remembering the sizes to compare after.

        The whole queue runs before the refresh, so every reclaimed figure is
        reported in one go rather than one refresh per disk.
        """
        if not self._run_queue(
            [(["modifymedium", "disk", d.uuid, "--compact"], None, None) for d in disks],
            "compact the selected disks",
        ):
            return
        self._size_before = {
            d.uuid: (os.path.basename(d.location) or d.uuid,
                     parse_capacity_mb(d.size_on_disk))
            for d in disks
        }

    def resize_selected(self):
        m = self._require_selection()
        if not m:
            return
        children = child_names(self.media[self.current_kind()], m.uuid)
        if children:
            QMessageBox.warning(
                self, "Read-only",
                f"{os.path.basename(m.location) or m.uuid} is the parent of "
                f"{', '.join(children)}, which makes it read-only — "
                "VirtualBox refuses to resize it. Resize the differencing "
                "image instead, or remove the child first.",
            )
            return
        dlg = ResizeDialog(m, self)
        if dlg.exec() and self._require_idle():
            self._run_single(dlg.args(m.uuid), title="resize")

    def _clear_target(self, path: str) -> bool:
        """Move a confirmed overwrite target aside until the command succeeds."""
        if self.operation_journal is None:
            QMessageBox.critical(self, "Recovery unavailable", self._journal_error)
            return False
        try:
            if self._active_operation is not None:
                raise OSError("An output replacement is already pending")
            self._active_operation = self.operation_journal.begin([], path, overwrite=True)
            if self._active_operation.backup:
                self.runner.note(f"[recovery] preserved {path} at {self._active_operation.backup}")
        except OSError as e:
            QMessageBox.critical(self, "Error", f"Could not preserve {path}: {e}")
            return False
        return True

    def convert_selected(self):
        """Clone the selected disk(s) to another format, variant or file."""
        disks = self._require_selection_multi()
        if not disks or not self._require_idle():
            return
        if len(disks) > 1:
            self._convert_batch(disks)
            return
        m = disks[0]
        dlg = ConvertDialog(m, self.backends, self, known=self.media["disk"])
        if not dlg.exec() or not self._require_idle():
            return
        if dlg.overwrite and not self._clear_target(dlg.target_path):
            return
        self._run_clone(dlg, m)

    def _convert_batch(self, disks: list[MediumRecord]):
        dlg = BatchConvertDialog(disks, self.backends, self,
                                 known=self.media["disk"])
        if not dlg.exec() or not self._require_idle():
            return
        commands = dlg.commands()
        snapshot_taken = False

        def prepare(args):
            nonlocal snapshot_taken
            if not snapshot_taken:
                self._snapshot_registry("batch convert")
                snapshot_taken = True
            target = args[3]  # clonemedium disk <source> <target>
            return target not in dlg.overwrite or self._clear_target(target)

        self._run_queue([(c, None, None) for c in commands],
                        "convert the selected disks", before_each=prepare)

    def edit_properties_selected(self):
        media = self._require_selection_multi()
        if not media:
            return
        dlg = PropertiesDialog(media, self)
        if not dlg.exec():
            return
        kind = self.current_kind()
        entries = []
        group = self._open_undo_group(
            f"properties of {os.path.basename(media[0].location) or media[0].uuid}"
            if len(media) == 1 else f"properties of {len(media)} media")
        for medium in media:
            forward = dlg.commands_for(kind, medium)
            if not forward:
                continue
            undo = [(c, None, None) for c in dlg.undo_commands_for(kind, medium)]
            # Recorded once the medium's first call succeeds: restoring a
            # field the batch never reached just re-sets its current value.
            entries.append((forward[0], None, lambda u=undo: group.entries.extend(u)))
            entries += [(c, None, None) for c in forward[1:]]
        if not entries:
            self.statusBar().showMessage("Properties unchanged.")
            return
        if self._run_queue(entries, "change properties"):
            self._push_undo_group(group)

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
        old, name = m.location, os.path.basename(m.location) or m.uuid
        self._followup = lambda: self._moved(m.uuid, old, path, name)
        if not self._run_single(move_args(m.uuid, path), title="move"):
            self._followup = None

    def _moved(self, uuid: str, old: str, new: str, name: str, undoable: bool = True):
        """Follow a disk file to its new place: library entry, then undo."""
        for kind in KINDS:
            if remove_library_path(kind, old):
                add_library_path(kind, new)
        if undoable:
            group = self._open_undo_group(f"move of {name}")
            group.entries.append((move_args(uuid, old), None,
                                  lambda: self._moved(uuid, new, old, name, undoable=False)))
            self._push_undo_group(group)

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
        self._run_single(args, stdin_data, secrets, title="change encryption")

    def resolve_selected(self):
        m = self._require_selection()
        if not m or not self._require_idle():
            return
        kind = self.current_kind()
        known = [rec.location for records in self.media.values() for rec in records]
        for each in KINDS:
            known += load_library(each)
        dlg = ResolveDialog(kind, m, self, known=known)
        if not dlg.exec() or not dlg.command or not self._require_idle():
            return
        command = dlg.command
        if command[3:4] == ["--setlocation"] and m.location:
            name = os.path.basename(m.location) or m.uuid

            def relocated():
                group = self._open_undo_group(f"re-pointing of {name}")
                group.entries.append((setlocation_args(kind, m.uuid, m.location), None, None))
                self._push_undo_group(group)
            self._followup = relocated
        if not self._run_single(command, title="resolve"):
            self._followup = None

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
        self._run_single(dlg.command, title="stamp a UUID")

    def edit_medium_properties(self):
        m = self._require_selection()
        if not m or not self._require_idle():
            return
        dlg = MediumPropertyDialog(self.current_kind(), m, self.backends, self)
        if not dlg.exec():
            return
        commands = dlg.commands()
        if not commands:
            self.statusBar().showMessage("Properties unchanged.")
            return
        self._run_queue([(c, None, None) for c in commands])
