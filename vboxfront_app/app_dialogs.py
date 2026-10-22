"""VBoxFront app dialogs."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)
import os

from .commands import plan_script, rerun_args
from .constants import KINDS, KIND_META, TRASH_DIR_NAME
from .domain import (
    health_findings,
    health_report,
    is_destructive_command,
    parse_uuid_collision,
    rerun_blocker,
    scan_for_images,
)
from .models import HealthFinding, MediumRecord
from .persistence import (
    clear_history,
    default_image_path,
    get_settings,
    image_folder,
    load_history,
    load_library,
    remember_folder,
    save_library,
    trash_enabled,
    trash_root,
)
from .theme import color_css, error_color, muted_color, warning_color


class CommandHistoryDialog(QDialog):
    """Every VBoxManage command this app has run, with its exit code."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Command history")
        self.resize(760, 420)
        #: argv of a command the user asked to run again, minus the VBoxManage
        #: path; None unless Re-run was confirmed.
        self.rerun: list[str] | None = None
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
                item.setForeground(error_color())
            self.list.addItem(item)
        if not self.list.count():
            self.list.addItem("No commands recorded yet.")
        layout.addWidget(self.list)

        self.list.itemDoubleClicked.connect(lambda _item: self._rerun_selected())

        row = QHBoxLayout()
        rerun = QPushButton("Re-run selected")
        rerun.setToolTip("Run the selected command again, after confirmation")
        rerun.clicked.connect(self._rerun_selected)
        row.addWidget(rerun)
        copy_one = QPushButton("Copy selected")
        copy_one.clicked.connect(self._copy_selected)
        copy_all = QPushButton("Copy all")
        copy_all.clicked.connect(self._copy_all)
        as_script = QPushButton("Save as script…")
        as_script.setToolTip(
            "Write the selected commands (or all of them) to a shell script: "
            "what this session did, reproducible without the GUI"
        )
        as_script.clicked.connect(self._save_script)
        clear = QPushButton("Clear history")
        clear.clicked.connect(self._clear)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(copy_one)
        row.addWidget(copy_all)
        row.addWidget(as_script)
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

    def _rerun_selected(self):
        """Replay one recorded command, guarded twice over.

        Re-run was left out of 1.2.0 because replaying `closemedium --delete`
        from a list is far too easy to do by accident. It is safe to offer once
        the two ways it goes wrong are closed off: a command whose secrets are
        gone is refused outright, and a destructive one has to be confirmed by
        typing, not by hitting Return on a default button.
        """
        item = self.list.currentItem()
        command = item.data(Qt.ItemDataRole.UserRole) if item else ""
        if not command:
            QMessageBox.information(self, "Re-run", "Select a command first.")
            return
        blocker = rerun_blocker(command)
        if blocker:
            QMessageBox.warning(self, "Cannot re-run", f"{blocker}.\n\n{command}")
            return
        args = rerun_args(command)
        if not args:
            QMessageBox.warning(self, "Cannot re-run", f"Could not parse:\n\n{command}")
            return
        if is_destructive_command(command):
            typed, ok = QInputDialog.getText(
                self, "Re-run a destructive command",
                "This command changes or destroys data:\n\n"
                f"{command}\n\nType RUN to confirm.",
            )
            if not ok or typed.strip() != "RUN":
                return
        else:
            ret = QMessageBox.question(self, "Re-run", f"Run this again?\n\n{command}")
            if ret != QMessageBox.StandardButton.Yes:
                return
        self.rerun = args
        self.accept()

    def _chosen_commands(self) -> list[str]:
        """The selected history lines, or all of them when none is picked."""
        selected = [
            item.data(Qt.ItemDataRole.UserRole)
            for item in self.list.selectedItems()
            if item.data(Qt.ItemDataRole.UserRole)
        ]
        if selected:
            # The list shows newest first; a script has to run oldest first.
            return list(reversed(selected))
        return [e.get("command", "") for e in load_history() if e.get("command")]

    def _save_script(self):
        """Write the chosen commands out as a runnable shell script.

        The history already holds every command verbatim, and "every action is
        a visible VBoxManage command" is the whole point of the app — this is
        the step that makes a session reproducible somewhere without a GUI, or
        pasteable into a change ticket.
        """
        commands = self._chosen_commands()
        if not commands:
            QMessageBox.information(self, "Nothing to save", "No commands recorded.")
            return
        blocked = [c for c in commands if rerun_blocker(c)]
        argvs = [rerun_args(c) for c in commands]
        argvs = [a for a in argvs if a]
        if not argvs:
            QMessageBox.information(
                self, "Nothing to save", "None of these commands could be parsed."
            )
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save history as script",
            default_image_path("export", "vboxfront-history.sh"),
            "Shell scripts (*.sh);;All files (*)",
        )
        if not path:
            return
        script = plan_script(argvs)
        if blocked:
            # A password that went over stdin is not in the command line, so
            # the script cannot carry it either. Say so in the script rather
            # than letting it fail at 3am.
            script = (
                "# WARNING: " + str(len(blocked)) + " of these commands read a "
                "password that was never stored\n"
                "# (it went over stdin, or through a temp file since removed). "
                "They will hang or fail.\n" + script
            )
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(script)
            os.chmod(path, 0o755)
        except OSError as e:
            QMessageBox.critical(self, "Save failed", f"Could not write {path}: {e}")
            return
        remember_folder("export", path)
        QMessageBox.information(
            self, "Saved",
            f"{len(argvs)} command(s) written to {path}"
            + (f"\n\n{len(blocked)} of them need a password the script cannot "
               "carry; the file says so at the top." if blocked else ""),
        )

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


class SettingsDialog(QDialog):
    """VBoxManage location, the delete trash, and the app-side media library."""

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

        images_row = QHBoxLayout()
        self.images_edit = QLineEdit(get_settings().value("image_folder", "", str))
        self.images_edit.setPlaceholderText(
            f"VirtualBox's own default ({image_folder()})"
        )
        images_browse = QPushButton("Browse…")
        images_browse.clicked.connect(self._browse_images)
        images_row.addWidget(self.images_edit, 1)
        images_row.addWidget(images_browse)
        layout.addRow("New images in:", images_row)
        layout.addRow(QLabel(
            "<i>Where Create disk, Create floppy and Import RAW start. Left "
            "empty they follow VirtualBox's own default machine folder. Each "
            "dialog then remembers the folder you last used it in.</i>"
        ))

        self.trash_check = QCheckBox(
            "Move deleted image files to a trash folder instead of erasing them"
        )
        self.trash_check.setChecked(trash_enabled())
        layout.addRow("Deleting:", self.trash_check)

        trash_row = QHBoxLayout()
        self.trash_edit = QLineEdit(trash_root())
        self.trash_edit.setPlaceholderText(
            f"beside each image, in {TRASH_DIR_NAME}/"
        )
        trash_browse = QPushButton("Browse…")
        trash_browse.clicked.connect(self._browse_trash)
        trash_row.addWidget(self.trash_edit, 1)
        trash_row.addWidget(trash_browse)
        layout.addRow("Trash folder:", trash_row)
        layout.addRow(
            QLabel(
                "<i>Left empty the trash follows each image, so the delete is "
                "a rename inside one filesystem and costs nothing however "
                "large the disk is — a folder on another drive means a "
                "full copy. Nothing is ever emptied automatically; "
                "<b>Media → Undo delete</b> puts back the last delete of "
                "this session, older files wait in the folder.</i>"
            )
        )
        self.trash_check.toggled.connect(self._sync_trash_row)
        self._sync_trash_row()

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

    def _browse_images(self):
        path = QFileDialog.getExistingDirectory(
            self, "Default folder for new images", self.images_edit.text() or image_folder()
        )
        if path:
            self.images_edit.setText(path)

    def _browse_trash(self):
        path = QFileDialog.getExistingDirectory(self, "Trash folder")
        if path:
            self.trash_edit.setText(path)

    def _sync_trash_row(self):
        self.trash_edit.setEnabled(self.trash_check.isChecked())

    def _remove_selected(self):
        for item in self.library_list.selectedItems():
            self.library_list.takeItem(self.library_list.row(item))

    def _ok(self):
        settings = get_settings()
        settings.setValue("vboxmanage_path", self.path_edit.text().strip())
        settings.setValue("image_folder", self.images_edit.text().strip())
        settings.setValue("trash/enabled", self.trash_check.isChecked())
        settings.setValue("trash/dir", self.trash_edit.text().strip())
        keep: dict[str, list[str]] = {kind: [] for kind in KINDS}
        for i in range(self.library_list.count()):
            kind, path = self.library_list.item(i).data(Qt.ItemDataRole.UserRole)
            keep[kind].append(path)
        for kind in KINDS:
            save_library(kind, keep[kind])
        self.accept()


class PlanDialog(QDialog):
    """The commands a batch would run, before it runs any of them.

    The app has always echoed each command as it went; this shows the whole
    list first. That makes a batch reviewable, and — via Copy — portable to a
    machine with no GUI, or into a change ticket.
    """

    #: A third exec() result: run the batch after all.
    RUN = 2

    def __init__(self, commands: list[list[str]], title: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Planned commands")
        self.resize(760, 380)
        self._commands = commands

        layout = QVBoxLayout(self)
        heading = QLabel(
            f"<b>{len(commands)} command(s)</b>"
            + (f" — {title}" if title else "")
        )
        layout.addWidget(heading)

        listing = QPlainTextEdit(plan_script(commands))
        listing.setReadOnly(True)
        layout.addWidget(listing)
        layout.addWidget(QLabel(
            "<i>Nothing has run. Copy takes the list as a shell script, with "
            "every argument quoted exactly as it would be passed.</i>"
        ))

        row = QHBoxLayout()
        copy = QPushButton("Copy as script")
        copy.clicked.connect(self._copy)
        save = QPushButton("Save as…")
        save.clicked.connect(self._save)
        run = QPushButton("Run it now")
        run.clicked.connect(lambda: self.done(self.RUN))
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row.addWidget(copy)
        row.addWidget(save)
        row.addStretch(1)
        row.addWidget(run)
        row.addWidget(close)
        layout.addLayout(row)

    def _copy(self):
        QApplication.clipboard().setText(plan_script(self._commands))

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save plan as", default_image_path("export", "vboxfront-plan.sh"),
            "Shell scripts (*.sh);;All files (*)",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(plan_script(self._commands))
            os.chmod(path, 0o755)
        except OSError as e:
            QMessageBox.critical(self, "Save failed", f"Could not write {path}: {e}")
            return
        remember_folder("export", path)
        QMessageBox.information(self, "Saved", f"Plan written to {path}")


class HealthDialog(QDialog):
    """Everything wrong with the registry that can be seen without running a
    command, plus an optional `repairhd` dry run over the VDIs."""

    def __init__(self, media: dict[str, list[MediumRecord]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Media health")
        self.resize(760, 420)
        self.dry_run_paths: list[str] = []
        self.reveal: tuple[str, str] | None = None
        #: Media whose backing file is gone, to unregister in one pass.
        self.prune: list[tuple[str, str]] = []
        #: Set when the user asks for the attached-without---discard scan,
        #: which needs a showvminfo per VM and so is the caller's to run.
        self.want_discard_scan = False
        self._media = media
        self._findings = health_findings(media)

        layout = QVBoxLayout(self)
        summary = QLabel(self._summary())
        summary.setWordWrap(True)
        layout.addWidget(summary)

        self.list = QListWidget()
        self.summary = summary
        self.repopulate()
        self.list.itemDoubleClicked.connect(self._reveal_selected)
        layout.addWidget(self.list)

        row = QHBoxLayout()
        reveal = QPushButton("Show in list")
        reveal.clicked.connect(lambda: self._reveal_selected(self.list.currentItem()))
        copy = QPushButton("Copy report")
        copy.clicked.connect(self._copy)
        self.cleanup = QPushButton("Clean up…")
        self.cleanup.setToolTip(
            "Unregister the entries whose backing file is gone. Nothing is "
            "deleted — the file already is."
        )
        self.cleanup.clicked.connect(self._start_cleanup)
        self.cleanup.setEnabled(bool(self._missing()))
        self.discard_btn = QPushButton("Check attach flags")
        self.discard_btn.setToolTip(
            "Read each VM and report disks attached without --discard, which "
            "is why compacting them reclaims nothing"
        )
        self.discard_btn.clicked.connect(self._start_discard_scan)
        self.dry_run = QPushButton("Check VDI images (dry run)")
        self.dry_run.setToolTip(
            "internalcommands repairhd -dry-run on every accessible VDI: reads "
            "the image and reports damage without writing to it"
        )
        self.dry_run.clicked.connect(self._start_dry_run)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row.addWidget(reveal)
        row.addWidget(copy)
        row.addWidget(self.cleanup)
        row.addWidget(self.discard_btn)
        row.addWidget(self.dry_run)
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)

    def _summary(self) -> str:
        errors = sum(1 for f in self._findings if f.level == "error")
        warnings = len(self._findings) - errors
        total = sum(len(v) for v in self._media.values())
        if not self._findings:
            return f"Checked {total} medium(s): no problems found."
        return (f"Checked {total} medium(s): <b>{errors} error(s)</b>, "
                f"{warnings} warning(s).")

    def _copy(self):
        QApplication.clipboard().setText(health_report(self._findings))

    def _reveal_selected(self, item):
        finding = item.data(Qt.ItemDataRole.UserRole) if item else None
        if isinstance(finding, HealthFinding):
            self.reveal = (finding.kind, finding.uuid)
            self.accept()

    def repopulate(self):
        """Redraw the list from `self._findings`.

        Separate from __init__ because the attach-flag scan arrives later: it
        needs a `showvminfo` per VM, so its findings can only be folded in
        once the dialog has already been built once.
        """
        self.list.clear()
        for finding in self._findings:
            item = QListWidgetItem(finding.line())
            item.setData(Qt.ItemDataRole.UserRole, finding)
            item.setForeground(
                error_color() if finding.level == "error" else warning_color()
            )
            item.setToolTip(finding.location)
            self.list.addItem(item)
        if not self._findings:
            self.list.addItem("Nothing to report.")
        self.summary.setText(self._summary())

    def _missing(self) -> list[HealthFinding]:
        """Findings for media whose backing file is gone.

        Both codes mean the same thing in practice: `missing-file` is the
        registry still claiming the medium is fine, `inaccessible` is
        VirtualBox having noticed. Either way there is nothing left to open.
        """
        return [
            f for f in self._findings
            if f.code in ("missing-file", "inaccessible")
            and f.location and not os.path.exists(f.location)
        ]

    def _start_cleanup(self):
        """Unregister every entry whose file is gone, in one queued pass.

        The commonest way a registry rots, and until now the dialog could only
        point at it. Plain `closemedium`, never `--delete`: there is no file
        left to delete, and asking VBoxManage to remove one would only fail.
        """
        missing = self._missing()
        if not missing:
            QMessageBox.information(
                self, "Nothing to clean up",
                "Every registered medium still has its backing file.",
            )
            return
        listing = "\n".join(
            f"{os.path.basename(f.location) or f.uuid} — {f.location}"
            for f in missing[:12]
        )
        ret = QMessageBox.question(
            self, "Clean up the registry",
            f"Unregister {len(missing)} medium(s) whose backing file is no "
            f"longer there?\n\n{listing}"
            + ("\n…" if len(missing) > 12 else "")
            + "\n\nNothing is deleted — the files are already gone. If one of "
            "them has merely moved, use Resolve… to re-point it instead.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self.prune = [(f.kind, f.uuid) for f in missing]
        self.accept()

    def _start_discard_scan(self):
        self.want_discard_scan = True
        self.accept()

    def add_findings(self, extra: list[HealthFinding]):
        """Fold in findings that needed a command, keeping the list sorted."""
        for finding in extra:
            if finding not in self._findings:
                self._findings.append(finding)

    def _start_dry_run(self):
        """Queue a read-only repairhd over the VDIs whose file is actually there.

        VDI only: repairhd understands VDI and VMDK, but `-format` has to be
        told which, and a wrong guess is a confusing error rather than a check.
        """
        paths = [
            rec.location for rec in self._media.get("disk", [])
            if rec.fmt.upper() == "VDI" and rec.location and os.path.exists(rec.location)
        ]
        if not paths:
            QMessageBox.information(
                self, "Nothing to check",
                "No VDI image with a readable file was found.",
            )
            return
        ret = QMessageBox.question(
            self, "Dry run",
            f"Read and check {len(paths)} VDI image(s)? Nothing is written — "
            "this is repairhd's dry run — but it reads every image in full, so "
            "it can take a while.",
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self.dry_run_paths = paths
        self.accept()

    @staticmethod
    def dry_run_caveat() -> str:
        """What the output panel is about to say, and why it is not alarming.

        `repairhd -dry-run` on 7.2.12 prints "Corrupted VDI image repaired
        successfully" as its closing line even when the line above it says the
        image is consistent and nothing was written. Verified on a freshly
        created VDI.
        """
        return ("[health] repairhd -dry-run writes nothing. It signs off with "
                "\"Corrupted VDI image repaired successfully\" whatever it "
                "found — the line above that one is the verdict.")


class OrphanScanDialog(QDialog):
    """Find image files on disk that neither VirtualBox nor the library knows."""

    def __init__(self, known: list[str], parent=None, capture=None):
        super().__init__(parent)
        self.setWindowTitle("Scan for unregistered images")
        self.resize(720, 460)
        self._known = known
        self._capture = capture
        self.chosen: list[tuple[str, str]] = []

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Folders to search for <i>.vdi .vmdk .vhd .iso .img …</i> files "
            "that are not registered with VirtualBox and not in the library:"
        ))

        self.folders = QListWidget()
        self.folders.setMaximumHeight(90)
        self.folders.addItem(image_folder())
        layout.addWidget(self.folders)

        folder_row = QHBoxLayout()
        add = QPushButton("Add folder…")
        add.clicked.connect(self._add_folder)
        drop = QPushButton("Remove folder")
        drop.clicked.connect(self._drop_folder)
        self.recursive = QCheckBox("Include subfolders")
        self.recursive.setChecked(True)
        scan = QPushButton("Scan")
        scan.clicked.connect(self._scan)
        self.explain_btn = QPushButton("Why unregistered?")
        self.explain_btn.setToolTip(
            "Ask VirtualBox to open each image. The usual answer is that its "
            "UUID collides with one already registered — a copied .vdi"
        )
        self.explain_btn.setEnabled(False)
        self.explain_btn.clicked.connect(self._explain)
        folder_row.addWidget(add)
        folder_row.addWidget(drop)
        folder_row.addWidget(self.recursive)
        folder_row.addStretch(1)
        folder_row.addWidget(self.explain_btn)
        folder_row.addWidget(scan)
        layout.addLayout(folder_row)

        self.results = QListWidget()
        layout.addWidget(self.results)
        self.status = QLabel("Not scanned yet.")
        self.status.setStyleSheet(color_css(muted_color()))
        layout.addWidget(self.status)

        row = QHBoxLayout()
        select_all = QPushButton("Select all")
        select_all.clicked.connect(lambda: self._set_all(True))
        select_none = QPushButton("Select none")
        select_none.clicked.connect(lambda: self._set_all(False))
        self.add_btn = QPushButton("Add checked to library")
        self.add_btn.setEnabled(False)
        self.add_btn.clicked.connect(self._ok)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row.addWidget(select_all)
        row.addWidget(select_none)
        row.addStretch(1)
        row.addWidget(self.add_btn)
        row.addWidget(close)
        layout.addLayout(row)

    def _add_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Folder to scan")
        if path and not self._folder_paths().count(path):
            self.folders.addItem(path)

    def _drop_folder(self):
        for item in self.folders.selectedItems():
            self.folders.takeItem(self.folders.row(item))

    def _folder_paths(self) -> list[str]:
        return [self.folders.item(i).text() for i in range(self.folders.count())]

    def _set_all(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.results.count()):
            item = self.results.item(i)
            if item.data(Qt.ItemDataRole.UserRole):
                item.setCheckState(state)

    def _scan(self):
        folders = self._folder_paths()
        if not folders:
            QMessageBox.information(self, "No folders", "Add a folder to scan first.")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            found = scan_for_images(folders, self._known, self.recursive.isChecked())
        finally:
            QApplication.restoreOverrideCursor()
        self.results.clear()
        for kind, path in found:
            item = QListWidgetItem(f"{KIND_META[kind]['title']}: {path}")
            item.setData(Qt.ItemDataRole.UserRole, (kind, path))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.results.addItem(item)
        self.add_btn.setEnabled(bool(found))
        self.explain_btn.setEnabled(bool(found) and self._capture is not None)
        self.status.setText(
            f"{len(found)} unregistered image(s) found in {len(folders)} folder(s)."
            if found else
            "No unregistered images found — everything here is already known."
        )

    def _explain(self):
        """Open each found image through VirtualBox and report what it says.

        One `showmediuminfo` per candidate, run one after another for the same
        reason the refresh chain is sequential. Opt-in rather than part of the
        scan: it is a process per file, and the scan itself costs none.
        """
        items = [self.results.item(i) for i in range(self.results.count())]
        pending = [i for i in items if i.data(Qt.ItemDataRole.UserRole)]
        if not pending or self._capture is None:
            return
        self.explain_btn.setEnabled(False)
        total = len(pending)
        queue = list(pending)

        def step():
            if not queue:
                self.explain_btn.setEnabled(True)
                self.status.setText(f"Checked {total} image(s).")
                return
            item = queue.pop(0)
            kind, path = item.data(Qt.ItemDataRole.UserRole)
            self.status.setText(
                f"Asking VirtualBox about {os.path.basename(path)} "
                f"({total - len(queue)}/{total})…"
            )

            def done(code: int, output: str):
                clash = parse_uuid_collision(output)
                if clash:
                    other, uuid = clash
                    item.setForeground(warning_color())
                    item.setText(
                        f"{item.text()}  — UUID {uuid} is already "
                        f"{os.path.basename(other)}"
                    )
                    item.setToolTip(
                        f"{path}\n\nIts UUID {uuid} belongs to {other}. "
                        "Adding it to the library will not register it; stamp "
                        "it with a fresh UUID in Image UUID tools first."
                    )
                    # Adopting it would only produce a failing re-open on
                    # every refresh, so do not offer it pre-checked.
                    item.setCheckState(Qt.CheckState.Unchecked)
                elif code != 0:
                    item.setForeground(warning_color())
                    item.setToolTip(f"{path}\n\nVirtualBox refused it:\n{output}")
                step()

            if not self._capture(["showmediuminfo", kind, path], done):
                self.explain_btn.setEnabled(True)
                return

        step()

    def _ok(self):
        self.chosen = [
            self.results.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.results.count())
            if self.results.item(i).checkState() == Qt.CheckState.Checked
            and self.results.item(i).data(Qt.ItemDataRole.UserRole)
        ]
        if not self.chosen:
            QMessageBox.information(self, "Nothing checked", "Check an image to add it.")
            return
        self.accept()
