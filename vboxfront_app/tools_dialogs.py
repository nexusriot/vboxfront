"""Dialogs for settings transfer, the TRIM fix, chain repair and bundles."""
from __future__ import annotations

import os

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from . import bundles, settings_io
from .chains import (
    STATUS_MISSING,
    STATUS_OK,
    STATUS_TEXT,
    ChainLink,
    chain_fixes,
    chain_is_repairable,
    relink_args,
    relink_blocker,
    trace_chain,
)
from .domain import discard_fix_plan, format_bytes, space_warning
from .locate import default_search_folders, read_image_identity
from .models import MediumRecord
from .persistence import image_folder, remember_folder, remembered_folder
from .theme import color_css, error_color, muted_color, warning_color

_workers: set = set()  # Keep cancelled tasks alive until QThread.finished.


class FileTask(QThread):
    """Run `fn(progress, cancelled)` off the GUI thread.

    `progress` takes a byte count to add; `done` carries (result, error text).
    A cancelled task reports error "cancelled".
    """

    progress = pyqtSignal(object)
    done = pyqtSignal(object, str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn
        self._bytes = 0

    def start(self, *args):
        _workers.add(self)
        self.finished.connect(self._release)
        super().start(*args)

    def _release(self):
        _workers.discard(self)
        self.deleteLater()

    def _add(self, count: int):
        self._bytes += count
        if self._bytes // (16 << 20) != (self._bytes - count) // (16 << 20):
            self.progress.emit(self._bytes)

    def run(self):
        try:
            result = self._fn(self._add, self.isInterruptionRequested)
        except bundles.Cancelled:
            self.done.emit(None, "cancelled")
        except Exception as e:  # reported in the dialog, never lost
            self.done.emit(None, str(e) or e.__class__.__name__)
        else:
            self.done.emit(result, "")


class _TaskDialogMixin:
    """Progress bar + Cancel for a dialog that runs one FileTask at a time."""

    def _init_task_ui(self, layout):
        self._task: FileTask | None = None
        self._total = 0
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

    def busy(self) -> bool:
        return self._task is not None

    def _start_task(self, fn, total: int, on_done):
        self._total = max(total, 1)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        task = FileTask(fn)
        task.progress.connect(self._task_progress)
        task.done.connect(lambda result, error: self._task_done(task, result, error, on_done))
        self._task = task
        task.start()

    def _task_progress(self, count):
        self.progress.setValue(min(1000, int(count * 1000 / self._total)))

    def _task_done(self, task, result, error, on_done):
        if task is not self._task:
            return
        self._task = None
        self.progress.setVisible(False)
        on_done(result, error)

    def _cancel_task(self):
        if self._task is not None:
            self._task.requestInterruption()

    def done(self, result):  # QDialog.done: never leave a copy running
        if self._task is not None:
            self._task.requestInterruption()
            self._task = None
        super().done(result)


# -- settings ------------------------------------------------------------

class SettingsTransferDialog(QDialog):
    """Pick the sections to export, or to import from a file already read."""

    def __init__(self, data: dict | None = None, parent=None):
        super().__init__(parent)
        self.importing = data is not None
        self.setWindowTitle("Import settings" if self.importing else "Export settings")
        layout = QVBoxLayout(self)
        if self.importing:
            layout.addWidget(QLabel(
                f"Written by VBoxFront {data.get('app_version', '?')} on "
                f"{data.get('exported', '?')}. Import:"))
            offered = settings_io.available_sections(data)
        else:
            layout.addWidget(QLabel("Export these settings to a JSON file:"))
            offered = list(settings_io.SECTIONS)
        self.boxes: dict[str, QCheckBox] = {}
        for section in offered:
            box = QCheckBox(settings_io.SECTION_TITLES[section])
            box.setChecked(section in settings_io.DEFAULT_SECTIONS)
            self.boxes[section] = box
            layout.addWidget(box)
        if not offered:
            layout.addWidget(QLabel("The file holds no settings this version knows."))
        self.replace = QCheckBox("Replace the library, filters and folders here "
                                 "instead of merging")
        self.replace.setVisible(self.importing)
        layout.addWidget(self.replace)
        note = QLabel(
            "<i>Window layout and the trash index are not included: they describe "
            "this screen and this machine's disks.</i>" if not self.importing else
            "<i>A VBoxManage path that is not executable here is skipped.</i>")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def sections(self) -> list[str]:
        return [s for s, box in self.boxes.items() if box.isChecked()]


# -- TRIM ----------------------------------------------------------------

class DiscardFixDialog(QDialog):
    """The storageattach commands that turn TRIM pass-through on."""

    def __init__(self, media: dict[str, list[MediumRecord]],
                 infos: dict[str, dict[str, str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Enable TRIM on attached disks")
        self.resize(720, 380)
        self._media, self._infos = media, infos
        self.commands: list[list[str]] = []
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Re-attach each disk to the same slot with <tt>--discard on</tt>, so "
            "blocks the guest frees reach the image and Compact can reclaim them. "
            "The medium, its UUID and its other flags stay as they are.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.ssd = QCheckBox("Also report the disk as an SSD (--nonrotational on). "
                             "Windows guests only send TRIM to an SSD.")
        self.ssd.setChecked(True)
        self.ssd.toggled.connect(self._rebuild)
        layout.addWidget(self.ssd)
        self.list = QListWidget()
        layout.addWidget(self.list, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Enable TRIM")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._rebuild()

    def _rebuild(self, *_):
        self.commands, skipped = discard_fix_plan(self._media, self._infos,
                                                  self.ssd.isChecked())
        names = {r.uuid: os.path.basename(r.location) or r.uuid
                 for r in self._media.get("disk", [])}
        vms = {uuid: info.get("name", uuid) for uuid, info in self._infos.items()}
        self.list.clear()
        for args in self.commands:
            self.list.addItem(f"{names.get(args[args.index('--medium') + 1], '?')} in "
                              f"{vms.get(args[1], args[1])}, {args[3]} port {args[5]}")
        for reason in skipped:
            item = QListWidgetItem(f"Skipped: {reason}")
            item.setForeground(warning_color())
            self.list.addItem(item)
        self.status.setText(
            f"{len(self.commands)} slot(s) to change."
            + (f" {len(skipped)} skipped because the VM is not powered off."
               if skipped else ""))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(self.commands))


# -- chain repair --------------------------------------------------------

class ChainRepairDialog(_TaskDialogMixin, QDialog):
    """Trace a chain from the image files and offer the repairs it allows."""

    def __init__(self, disks: list[MediumRecord], uuid: str = "", path: str = "",
                 known: list[str] | None = None, parent=None, autostart: bool = True):
        super().__init__(parent)
        self.setWindowTitle("Repair chain")
        self.resize(860, 460)
        self._disks = disks
        self._uuid, self._path = uuid, path
        self._known = list(known or [])
        self.links: list[ChainLink] = []
        self.commands: list[list[str]] = []
        self.register: list[str] = []
        self.relink: list[str] | None = None

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Each image file records its own UUID and its parent's. The chain is "
            "followed through the files, and a member the registry lost track of "
            "is searched for by UUID in these folders:")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.folders = QListWidget()
        self.folders.setMaximumHeight(70)
        start = path or next((d.location for d in disks if d.uuid == uuid), "")
        for folder in default_search_folders(start, [image_folder()]):
            self.folders.addItem(folder)
        layout.addWidget(self.folders)
        row = QHBoxLayout()
        add = QPushButton("Add folder…")
        add.clicked.connect(self._add_folder)
        self.trace_btn = QPushButton("Trace")
        self.trace_btn.clicked.connect(self._trace_or_cancel)
        row.addWidget(add)
        row.addStretch(1)
        row.addWidget(self.trace_btn)
        layout.addLayout(row)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Image", "Status", "File", "Note"])
        self.tree.setRootIsDecorated(False)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.tree, 1)
        self.status = QLabel("Not traced yet.")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(color_css(muted_color()))
        layout.addWidget(self.status)
        self._init_task_ui(layout)

        buttons = QHBoxLayout()
        self.relink_btn = QPushButton("Choose parent file…")
        self.relink_btn.setToolTip(
            "Point an unregistered child at a parent file whose UUID differs "
            "(internalcommands sethdparentuuid)")
        self.relink_btn.clicked.connect(self._choose_parent)
        self.repair_btn = QPushButton("Repair")
        self.repair_btn.clicked.connect(self._repair)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons.addWidget(self.relink_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.repair_btn)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self._sync()
        if autostart:
            self.trace()

    def _add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Folder to search")
        if folder:
            self.folders.addItem(folder)

    def folder_paths(self) -> list[str]:
        return [self.folders.item(i).text() for i in range(self.folders.count())]

    def _trace_or_cancel(self):
        if self.busy():
            self._cancel_task()
        else:
            self.trace()

    def trace(self):
        disks, uuid, path = self._disks, self._uuid, self._path
        folders, known = self.folder_paths(), self._known
        self.trace_btn.setText("Cancel")
        self.status.setText("Tracing…")
        self.progress.setRange(0, 0)

        def work(_progress, cancelled):
            return trace_chain(uuid, path, disks, folders, known, cancelled)

        self._start_task(work, 1, self._traced)
        self.progress.setRange(0, 0)  # busy indicator: a walk has no total

    def _traced(self, links, error):
        self.trace_btn.setText("Trace")
        if error:
            self.status.setText(f"Trace stopped: {error}")
            return
        self.show_links(links or [])

    def show_links(self, links: list[ChainLink]):
        self.links = links
        self.tree.clear()
        for link in links:
            item = QTreeWidgetItem([link.name, STATUS_TEXT[link.status],
                                    link.path, link.note])
            item.setToolTip(0, link.uuid)
            item.setToolTip(2, link.path)
            if link.status == STATUS_MISSING:
                item.setForeground(1, error_color())
            elif link.status != STATUS_OK:
                item.setForeground(1, warning_color())
            self.tree.addTopLevelItem(item)
        for col in (0, 1, 3):
            self.tree.resizeColumnToContents(col)
        commands, register = chain_fixes(links)
        if not links:
            text = "Nothing to trace."
        elif not chain_is_repairable(links):
            missing = links[0]
            text = (f"The chain cannot be completed: {missing.name} ({missing.uuid}) "
                    "was not found. Add the folder it is in, or choose the parent "
                    "file by hand.")
        elif not commands and not register:
            text = "The chain is complete and every file is where the registry says."
        else:
            text = (f"Repair will re-point {len(commands)} registered image(s) and add "
                    f"{len(register)} to the library, base first.")
        self.status.setText(text)
        self._sync()

    def _missing_child(self) -> ChainLink | None:
        """The link right above the missing one, which a relink would edit."""
        if len(self.links) >= 2 and self.links[0].status == STATUS_MISSING:
            return self.links[1]
        return None

    def _sync(self):
        commands, register = chain_fixes(self.links)
        self.repair_btn.setEnabled(chain_is_repairable(self.links)
                                   and bool(commands or register))
        self.relink_btn.setEnabled(self._missing_child() is not None)

    def _repair(self):
        self.commands, self.register = chain_fixes(self.links)
        self.accept()

    def _choose_parent(self):
        child = self._missing_child()
        if child is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, f"Parent of {child.name}", os.path.dirname(child.path),
            "Disk images (*.vdi *.vmdk *.vhd);;All files (*)")
        if not path:
            return
        problem = relink_blocker(child, path)
        if problem:
            QMessageBox.warning(self, "Cannot relink", problem)
            return
        identity = read_image_identity(path)
        ret = QMessageBox.warning(
            self, "Relink differencing image?",
            f"Write parent UUID {identity.uuid} into {child.name}?\n\n"
            f"{child.name} was created on top of {child.parent_uuid}. Only relink "
            f"it to {os.path.basename(path)} if that file holds exactly the same "
            "data — a copy whose UUID was changed, say. On any other parent the "
            "guest reads a mix of two disks, and nothing reports it.\n\n"
            "This uses VBoxManage internalcommands, which Oracle ships unsupported. "
            "Copy the child first.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        self.relink = relink_args(child.path, identity.uuid)
        self.accept()


# -- bundles -------------------------------------------------------------

class BackupDialog(_TaskDialogMixin, QDialog):
    """Copy a medium and its chain into a new bundle folder."""

    def __init__(self, members: list[bundles.BundleMember], running: list[str] = (),
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Back up to bundle")
        self.resize(640, 380)
        self._members = members
        self.bundle_path = ""
        self._size = bundles.bundle_size(members)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"{len(members)} image(s), {format_bytes(self._size)}, copied byte for "
            "byte so UUIDs and parent links survive:"))
        listing = QListWidget()
        for member in members:
            for path in member.files:
                listing.addItem(path)
        layout.addWidget(listing, 1)
        if running:
            warn = QLabel("Attached to a running VM: " + ", ".join(running)
                          + ". A copy taken now may be inconsistent. Power the VM "
                            "off first.")
            warn.setWordWrap(True)
            warn.setStyleSheet(color_css(error_color()))
            layout.addWidget(warn)
        self._running = list(running)
        form = QFormLayout()
        row = QHBoxLayout()
        self.dest = QLineEdit(remembered_folder("bundles"))
        self.dest.textChanged.connect(self._check_space)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.dest, 1)
        row.addWidget(browse)
        form.addRow("Into folder:", row)
        self.name = QLineEdit(bundles.default_bundle_name(members))
        form.addRow("Bundle name:", self.name)
        layout.addLayout(form)
        self.space = QLabel("")
        self.space.setWordWrap(True)
        self.space.setStyleSheet(color_css(warning_color()))
        layout.addWidget(self.space)
        self._init_task_ui(layout)
        buttons = QHBoxLayout()
        self.start_btn = QPushButton("Back up")
        self.start_btn.clicked.connect(self._start)
        self.cancel_btn = QPushButton("Close")
        self.cancel_btn.clicked.connect(self._cancel_or_close)
        buttons.addStretch(1)
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.cancel_btn)
        layout.addLayout(buttons)
        self._check_space()

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Bundle folder", self.dest.text())
        if folder:
            self.dest.setText(folder)

    def _check_space(self, *_):
        target = os.path.join(self.dest.text().strip() or ".", "x")
        self.space.setText(space_warning(target, self._size))

    def _cancel_or_close(self):
        if self.busy():
            self._cancel_task()
        else:
            self.reject()

    def _start(self):
        dest = self.dest.text().strip()
        if not dest or not os.path.isdir(dest):
            QMessageBox.warning(self, "No folder", "Choose an existing folder.")
            return
        if self._running and QMessageBox.question(
                self, "VM running", "Back up anyway? The copy may not be usable.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        if self.space.text() and QMessageBox.question(
                self, "Not enough space", self.space.text() + "\n\nStart anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        remember_folder("bundles", dest)
        members, name = self._members, self.name.text().strip()
        self.start_btn.setEnabled(False)
        self.cancel_btn.setText("Cancel")
        self._start_task(
            lambda progress, cancelled: bundles.write_bundle(
                members, dest, name, progress, cancelled),
            self._size, self._finished)

    def _finished(self, path, error):
        self.start_btn.setEnabled(True)
        self.cancel_btn.setText("Close")
        if error:
            QMessageBox.warning(self, "Backup not written",
                                "Cancelled; nothing was kept." if error == "cancelled"
                                else error)
            return
        self.bundle_path = path
        self.accept()


class RestoreDialog(_TaskDialogMixin, QDialog):
    """Verify a bundle, copy its files into a folder, then register them."""

    def __init__(self, bundle: str, manifest: dict, registered: dict[str, str],
                 parent=None, verify_only: bool = False):
        super().__init__(parent)
        self.verify_only = verify_only
        self.setWindowTitle("Verify bundle" if verify_only else "Restore bundle")
        self.resize(680, 400)
        self._bundle, self._registered = bundle, registered
        self._size = bundles.manifest_size(manifest)
        self.steps: list[bundles.RestoreStep] = []
        self.problems: list[str] | None = None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"{os.path.basename(bundle)} — created {manifest.get('created', '?')}, "
            f"{len(manifest['media'])} image(s), {format_bytes(self._size)}"))
        self.list = QListWidget()
        layout.addWidget(self.list, 1)
        form = QFormLayout()
        row = QHBoxLayout()
        self.into = QLineEdit(remembered_folder("restore"))
        self.into.textChanged.connect(self.replan)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.into, 1)
        row.addWidget(browse)
        if not verify_only:
            form.addRow("Restore into:", row)
        layout.addLayout(form)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self._init_task_ui(layout)
        buttons = QHBoxLayout()
        self.start_btn = QPushButton("Verify" if verify_only else "Restore")
        self.start_btn.clicked.connect(self._start)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self._cancel_or_close)
        buttons.addStretch(1)
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.close_btn)
        layout.addLayout(buttons)
        self.replan()

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Restore into", self.into.text())
        if folder:
            self.into.setText(folder)

    def replan(self, *_):
        into = self.into.text().strip()
        self.list.clear()
        try:
            self.steps, blockers = bundles.plan_restore(self._bundle, into or ".",
                                                        self._registered)
        except bundles.BundleError as e:
            self.steps, blockers = [], [str(e)]
        for step in self.steps:
            text = f"{step.name} ({step.uuid})"
            if self.verify_only:
                pass
            elif step.skip:
                text += f" — skipped: {step.skip}"
            else:
                text += f" → {step.target}"
            self.list.addItem(text)
        if not self.verify_only and not into:
            blockers = ["choose a folder to restore into"] + blockers
        self._blockers = [] if self.verify_only else blockers
        self.status.setText("\n".join(self._blockers))
        self.status.setStyleSheet(color_css(error_color()) if self._blockers else "")
        self.start_btn.setEnabled(not self._blockers)

    def _cancel_or_close(self):
        if self.busy():
            self._cancel_task()
        else:
            self.reject()

    def _start(self):
        bundle, steps = self._bundle, self.steps
        self.start_btn.setEnabled(False)
        self.close_btn.setText("Cancel")
        if self.verify_only:
            self._start_task(lambda p, c: bundles.verify_bundle(bundle, p, c),
                             self._size, self._verified)
            return
        remember_folder("restore", self.into.text().strip())
        self._start_task(lambda p, c: bundles.restore_files(bundle, steps, p, c),
                         self._size, self._restored)

    def _verified(self, problems, error):
        self.close_btn.setText("Close")
        self.start_btn.setEnabled(True)
        if error:
            self.status.setText(f"Verification stopped: {error}")
            return
        self.problems = problems
        self.status.setStyleSheet(color_css(error_color()) if problems else "")
        self.status.setText("\n".join(problems) if problems else
                            "Every file matches its checksum.")

    def _restored(self, _result, error):
        self.close_btn.setText("Close")
        if error:
            self.start_btn.setEnabled(True)
            QMessageBox.warning(self, "Restore failed",
                                ("Cancelled" if error == "cancelled" else error)
                                + "\n\nFiles written by this restore were removed.")
            return
        self.accept()

    def restored_paths(self) -> list[tuple[str, str]]:
        """(kind, backing file) for every member actually copied, base first."""
        return [(s.kind, s.target) for s in self.steps if not s.skip]
