"""Search folders for a moved image by the UUID stored inside it."""
from __future__ import annotations

import os

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from .domain import parse_capacity_mb
from .locate import MATCH_UUID, LocateCandidate, default_search_folders, find_moved_image
from .models import MediumRecord
from .persistence import image_folder
from .theme import color_css, muted_color, warning_color

_workers: set = set()  # Keep cancelled searches alive until QThread.finished.


class LocateTask(QThread):
    """`find_moved_image` off the GUI thread: a home folder can take a while."""

    progress = pyqtSignal(int)
    done = pyqtSignal(object)

    def __init__(self, uuid: str, old_location: str, folders: list[str],
                 known: list[str], expected_mb: int | None, recursive: bool):
        super().__init__()
        self._args = (uuid, old_location, folders, known, expected_mb, recursive)

    def start(self, *args):
        _workers.add(self)
        self.finished.connect(self._release)
        super().start(*args)

    def _release(self):
        _workers.discard(self)
        self.deleteLater()

    def run(self):
        uuid, old, folders, known, expected_mb, recursive = self._args
        found = find_moved_image(
            uuid, old, folders, known, expected_mb, recursive,
            cancelled=self.isInterruptionRequested,
            progress=self.progress.emit,
        )
        self.done.emit(None if self.isInterruptionRequested() else found)


class LocateMovedDialog(QDialog):
    """Find where a medium's file went, so Resolve can re-point the registry.

    Read-only: it opens candidate files for their first kilobyte (and a VHD's
    last 512 bytes) and runs no VBoxManage command. Choosing a result only
    fills in Resolve's path; the `--setlocation` itself is still Resolve's.
    """

    def __init__(self, medium: MediumRecord, known: list[str] | None = None,
                 parent=None, autostart: bool = True):
        super().__init__(parent)
        self.setWindowTitle("Find moved image")
        self.resize(720, 440)
        self._medium = medium
        self._known = list(known or [])
        self._task: LocateTask | None = None
        self.chosen = ""
        self.candidates: list[LocateCandidate] = []

        layout = QVBoxLayout(self)
        intro = QLabel(
            f"Looking for the image with UUID <tt>{medium.uuid}</tt>, last seen at "
            f"<tt>{medium.location}</tt>. Disk images carry their UUID inside "
            "the file, so a match is certain. ISOs and RAW images do not; for "
            "those only a file with the same name and size is offered."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(intro)

        self.folders = QListWidget()
        self.folders.setMaximumHeight(90)
        for folder in default_search_folders(medium.location, [image_folder()]):
            self.folders.addItem(folder)
        layout.addWidget(self.folders)

        folder_row = QHBoxLayout()
        add = QPushButton("Add folder…")
        add.clicked.connect(self._add_folder)
        drop = QPushButton("Remove folder")
        drop.clicked.connect(self._drop_folder)
        self.recursive = QCheckBox("Include subfolders")
        self.recursive.setChecked(True)
        self.search_btn = QPushButton("Search")
        self.search_btn.clicked.connect(self._search_or_cancel)
        folder_row.addWidget(add)
        folder_row.addWidget(drop)
        folder_row.addWidget(self.recursive)
        folder_row.addStretch(1)
        folder_row.addWidget(self.search_btn)
        layout.addLayout(folder_row)

        self.results = QListWidget()
        self.results.itemSelectionChanged.connect(self._sync_buttons)
        self.results.itemDoubleClicked.connect(lambda _item: self._use())
        layout.addWidget(self.results, 1)
        self.status = QLabel("Not searched yet.")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(color_css(muted_color()))
        layout.addWidget(self.status)

        row = QHBoxLayout()
        self.use_btn = QPushButton("Use this file")
        self.use_btn.setEnabled(False)
        self.use_btn.clicked.connect(self._use)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row.addStretch(1)
        row.addWidget(self.use_btn)
        row.addWidget(close)
        layout.addLayout(row)

        if autostart and self.folders.count():
            self.search()

    # -- folders ---------------------------------------------------------
    def _add_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Folder to search")
        if path and path not in self.folder_paths():
            self.folders.addItem(path)

    def _drop_folder(self):
        for item in self.folders.selectedItems():
            self.folders.takeItem(self.folders.row(item))

    def folder_paths(self) -> list[str]:
        return [self.folders.item(i).text() for i in range(self.folders.count())]

    # -- searching -------------------------------------------------------
    def searching(self) -> bool:
        return self._task is not None

    def _search_or_cancel(self):
        if self.searching():
            self.cancel()
        else:
            self.search()

    def search(self):
        folders = self.folder_paths()
        if not folders:
            self.status.setText("Add a folder to search first.")
            return
        self.results.clear()
        self.candidates = []
        self._sync_buttons()
        self.status.setText(f"Searching {len(folders)} folder(s)…")
        self.search_btn.setText("Cancel")
        task = LocateTask(
            self._medium.uuid, self._medium.location, folders, self._known,
            parse_capacity_mb(self._medium.capacity), self.recursive.isChecked(),
        )
        task.progress.connect(self._progress)
        task.done.connect(self._finished)
        self._task = task
        task.start()

    def cancel(self):
        if self._task is not None:
            self._task.requestInterruption()
            self._task = None
        self.search_btn.setText("Search")
        self.status.setText("Search cancelled.")

    def _progress(self, examined: int):
        if self.sender() is self._task:
            self.status.setText(f"Searching… {examined} image file(s) examined.")

    def _finished(self, found):
        if self.sender() is not self._task or found is None:
            return  # A cancelled search, or one replaced by a newer one.
        self._task = None
        self.search_btn.setText("Search")
        self.show_candidates(found)

    def show_candidates(self, found: list[LocateCandidate]):
        self.candidates = list(found)
        self.results.clear()
        for cand in self.candidates:
            label = "UUID match" if cand.match == MATCH_UUID else "Same name and size"
            item = QListWidgetItem(f"{label}: {cand.path}")
            item.setData(Qt.ItemDataRole.UserRole, cand.path)
            # Long paths are cut off at the list's edge; the tooltip has it all.
            item.setToolTip(cand.path + (f"\n{cand.note}" if cand.note else ""))
            if cand.note:
                item.setText(f"{item.text()}  ({cand.note})")
            if cand.match != MATCH_UUID or cand.note:
                item.setForeground(warning_color())
            self.results.addItem(item)
        if self.candidates:
            self.results.setCurrentRow(0)
        certain = sum(1 for c in self.candidates if c.match == MATCH_UUID)
        if certain:
            self.status.setText(
                f"{certain} file(s) carry this medium's UUID."
                + (" More than one is a copy: pick the one VirtualBox should use."
                   if certain > 1 else "")
            )
        elif self.candidates:
            self.status.setText("No UUID match. Files with the same name and size are listed.")
        else:
            self.status.setText(
                "Nothing found. Add the folder the file was moved to, or use "
                f"Browse… in Resolve. ({os.path.basename(self._medium.location)})"
            )
        self._sync_buttons()

    def _sync_buttons(self):
        self.use_btn.setEnabled(bool(self.results.selectedItems()))

    def _use(self):
        items = self.results.selectedItems()
        if not items:
            return
        self.chosen = items[0].data(Qt.ItemDataRole.UserRole)
        self.accept()

    def done(self, result):  # noqa: D401 - QDialog.done override
        if self._task is not None:
            self._task.requestInterruption()
            self._task = None
        super().done(result)
