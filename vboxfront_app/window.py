"""VBoxFront window."""

from __future__ import annotations

from PyQt6.QtCore import QByteArray, Qt, QUrl
from PyQt6.QtGui import QAction, QDesktopServices, QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QToolBar,
)
import os

from .app_dialogs import CommandHistoryDialog, SettingsDialog
from .constants import KINDS, KIND_META, MEDIA_EXTENSIONS, RAW_EXTENSIONS
from .domain import (
    FILTER_FIELDS,
    FILTER_SIZES,
    records_to_csv,
    records_to_json,
    records_to_tsv,
)
from .models import MediumBackend, MediumRecord, TrashedFile
from .persistence import (
    add_library_path,
    default_image_path,
    get_settings,
    plan_mode,
    remember_folder,
    set_plan_mode,
    vboxmanage_problem,
)
from .processes import CommandRunner
from .version import APP_VERSION
from .features import FeatureActionsMixin
from .workflows import WorkflowActionsMixin
from .tools import ToolsActionsMixin
from .attach_actions import AttachActionsMixin
from .command_queue import CommandQueueMixin
from .health_actions import HealthActionsMixin
from .media_create import MediaCreateMixin
from .media_edit import MediaEditMixin
from .refresh import RefreshMixin
from .removal import RemovalActionsMixin
from .widgets import MediaPane


class MainWindow(
    RefreshMixin,
    CommandQueueMixin,
    MediaCreateMixin,
    MediaEditMixin,
    AttachActionsMixin,
    RemovalActionsMixin,
    HealthActionsMixin,
    ToolsActionsMixin,
    WorkflowActionsMixin,
    FeatureActionsMixin,
    QMainWindow,
):
    """The main window: layout, selection and UI state.

    The actions live in mixins by area, listed before the older feature,
    workflow and tools mixins so that a method moved out of this class keeps
    precedence over a same-named one there, as it had when it was defined
    here.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"VBoxFront {APP_VERSION} — VBoxManage GUI")
        self.resize(1200, 760)

        # Media parsed per kind on the last refresh.
        self.media: dict[str, list[MediumRecord]] = {k: [] for k in KINDS}
        # Path to add to the library once the current command succeeds
        # (used after `convertfromraw`, which only writes the file).
        self._pending_library_add: tuple[str, str] | None = None
        # Commands queued behind the currently running one (batch operations),
        # each with its own follow-up to run when *that* command succeeds. A
        # single shared pending-action slot cannot work for a batch: the
        # follow-up would fire against whichever command happened to finish.
        self._cmd_queue: list[tuple[list[str], bytes | None, object]] = []
        self._queue_before = None
        self._followup = None
        # uuid -> (name, size on disk before a compact), so the reclaimed space
        # can be reported as measured rather than predicted.
        self._size_before: dict[str, tuple[str, int | None]] = {}
        # Files a delete moved aside, newest batch last, one list per Remove:
        # a Remove of five disks is one mistake, so undoing it has to put all
        # five back. TrashStore reloads the persisted batches after UI setup.
        self._trash: list[list[TrashedFile]] = []
        self._refreshing = False
        self._refresh_pending = False
        #: Registry-file events before this monotonic time are our own writes.
        self._watch_quiet_until = 0.0
        #: Fingerprint of the registry as this window last saw it, and whether
        #: a command of ours is what changed it. A difference with no command
        #: in between means something else edited the registry.
        self._fingerprint = ""
        self._expect_change = True
        #: mtime+size of each library path at its last successful re-open, so
        #: an unchanged file is not re-opened on every refresh.
        self._library_seen: dict[str, tuple[float, int]] = {}
        self._chain: list[tuple[list[str], object]] = []
        # Formats/variants VirtualBox actually supports, read once and cached;
        # empty means the dialogs fall back to their hardcoded lists.
        self.backends: list[MediumBackend] = []
        self._backends_warned = False

        self.setAcceptDrops(True)
        self._build_ui()
        self._init_features()
        self._init_workflows()
        self._init_tools()
        self._update_actions()
        self._refresh_check_vbm()
        self.refresh_media()

    def dragEnterEvent(self, event):
        if self._dropped_paths(event):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if self._dropped_paths(event):
            event.acceptProposedAction()

    def _dropped_paths(self, event) -> list[str]:
        """Local image files in a drag, or [] when there is nothing to take."""
        data = event.mimeData()
        if not data.hasUrls():
            return []
        paths = []
        for url in data.urls():
            path = url.toLocalFile()
            if not path or not os.path.isfile(path):
                continue
            ext = os.path.splitext(path)[1].lower()
            if ext in MEDIA_EXTENSIONS or ext in RAW_EXTENSIONS:
                paths.append(path)
        return paths

    def dropEvent(self, event):
        """Dropped images join the library; a dropped raw image offers Import.

        The library is the only way an unattached image stays visible (modern
        VirtualBox forgets a standalone registration when VBoxSVC exits), so
        adding is exactly what *Media → Add file…* does — this is the same
        thing without the file dialog.
        """
        paths = self._dropped_paths(event)
        if not paths:
            return
        event.acceptProposedAction()
        raw = [p for p in paths if os.path.splitext(p)[1].lower() in RAW_EXTENSIONS
               and os.path.splitext(p)[1].lower() not in MEDIA_EXTENSIONS]
        images = [p for p in paths if p not in raw]
        added = 0
        for path in images:
            kind = MEDIA_EXTENSIONS[os.path.splitext(path)[1].lower()]
            add_library_path(kind, path)
            self.runner.note(f"[library] added {kind} {path}")
            added += 1
        if added:
            self.refresh_media()
        if len(raw) == 1 and not added:
            ret = QMessageBox.question(
                self, "Import RAW",
                f"{os.path.basename(raw[0])} is a raw image. Convert it into a "
                "managed disk format?",
            )
            if ret == QMessageBox.StandardButton.Yes:
                self.convert_from_raw(raw[0])
        elif raw:
            self.runner.note(
                f"[drop] ignored {len(raw)} raw image(s); use Import RAW… for them"
            )

    def _build_ui(self):
        self.act_refresh = self._action("Refresh", self.refresh_media, "F5")
        self.act_create = self._action("Create disk…", self.create_disk, "Ctrl+N")
        self.act_add = self._action("Add file…", self.add_media_from_file)
        self.act_fromraw = self._action("Import RAW…", lambda: self.convert_from_raw())
        self.act_import_compressed = self._action(
            "Import compressed RAW…", self.import_compressed)
        self.act_export_raw = self._action("Export to raw image…", self.export_raw)
        self.act_info = self._action("Info", self.show_info_selected, "Ctrl+I")
        self.act_compact = self._action("Compact", self.compact_selected, "Ctrl+K")
        self.act_compact_all = self._action("Compact all VDIs", self.compact_all_vdis)
        self.act_resize = self._action("Resize…", self.resize_selected, "Ctrl+R")
        self.act_convert = self._action("Convert / Clone…", self.convert_selected, "Ctrl+L")
        self.act_props = self._action("Properties…", self.edit_properties_selected,
                                     "Alt+Return")
        self.act_move = self._action("Move…", self.move_selected)
        self.act_encrypt = self._action("Encryption…", self.encrypt_selected)
        self.act_resolve = self._action("Resolve…", self.resolve_selected)
        self.act_uuid_tools = self._action("Image UUID tools…", self.open_uuid_tools)
        self.act_create_diff = self._action("Create differencing image…", self.create_diff)
        self.act_create_floppy = self._action("Create floppy image…", self.create_floppy)
        self.act_contents = self._action("Contents…", self.show_contents_selected)
        self.act_formatfat = self._action("Format as FAT…", self.formatfat_selected)
        self.act_medium_props = self._action("Format properties…", self.edit_medium_properties)
        self.act_health = self._action("Media health…", self.open_health, "Ctrl+H")
        self.act_orphans = self._action("Scan for unregistered images…", self.open_orphan_scan)
        self.act_export = self._action("Export listing…", self.export_listing, "Ctrl+E")
        self.act_copy_rows = self._action("Copy rows (TSV)", self.copy_rows_selected)
        self.act_history = self._action("Command history…", self.open_history, "Ctrl+Y")
        self.act_attach = self._action("Attach to VM…", self.attach_selected, "Ctrl+T")
        self.act_detach = self._action("Detach from VM…", self.detach_selected, "Ctrl+D")
        self.act_remove = self._action("Remove…", self.remove_selected, QKeySequence.StandardKey.Delete)
        self.act_undo_delete = self._action("Undo delete", self.undo_delete, "Ctrl+Z")
        self.act_undo_delete.setEnabled(False)
        self.act_copy_uuid = self._action("Copy UUID", self.copy_uuid_selected)
        self.act_copy_path = self._action("Copy location", self.copy_path_selected)
        self.act_open_dir = self._action("Open containing folder", self.open_folder_selected)
        self.act_plan_mode = QAction("Plan mode (show batches, do not run)", self)
        self.act_plan_mode.setCheckable(True)
        self.act_plan_mode.setChecked(plan_mode())
        self.act_plan_mode.setToolTip(
            "Every batch shows its commands first, with Copy and Save, and "
            "runs only if you say so there"
        )
        self.act_plan_mode.toggled.connect(self._toggle_plan_mode)
        self.act_details = QAction("Details pane", self)
        self.act_details.setCheckable(True)
        self.act_details.setShortcut(QKeySequence("Ctrl+J"))
        self.act_details.setChecked(
            get_settings().value("ui/details", False, bool))
        self.act_details.toggled.connect(self._toggle_details)
        self.act_settings = self._action("Settings…", self.open_settings)
        act_quit = self._action("Quit", self.close, "Ctrl+Q")

        menu_file = self.menuBar().addMenu("&File")
        menu_file.addAction(self.act_export)
        menu_file.addAction(self.act_history)
        menu_file.addSeparator()
        menu_file.addAction(self.act_plan_mode)
        menu_file.addAction(self.act_settings)
        menu_file.addSeparator()
        menu_file.addAction(act_quit)

        menu_media = self.menuBar().addMenu("&Media")
        menu_media.addAction(self.act_refresh)
        menu_media.addAction(self.act_details)
        menu_media.addSeparator()
        menu_media.addAction(self.act_create)
        menu_media.addAction(self.act_create_diff)
        menu_media.addAction(self.act_create_floppy)
        menu_media.addAction(self.act_add)
        menu_media.addAction(self.act_fromraw)
        menu_media.addAction(self.act_import_compressed)
        menu_media.addAction(self.act_compact_all)
        menu_media.addAction(self.act_uuid_tools)
        menu_media.addAction(self.act_orphans)
        menu_media.addAction(self.act_health)
        menu_media.addSeparator()
        for act in (self.act_info, self.act_contents, self.act_compact, self.act_resize,
                    self.act_convert, self.act_props, self.act_medium_props,
                    self.act_move, self.act_encrypt, self.act_export_raw,
                    self.act_formatfat, self.act_resolve,
                    self.act_attach, self.act_detach):
            menu_media.addAction(act)
        menu_media.addSeparator()
        menu_media.addAction(self.act_remove)
        menu_media.addAction(self.act_undo_delete)

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

        self.addToolBarBreak()
        toolbar = QToolBar("Filters and views")
        toolbar.setObjectName("FilterAndViewsToolBar")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        toolbar.addWidget(QLabel("Filter: "))
        self.filter_edit = QLineEdit()
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setMinimumWidth(280)
        self.filter_edit.setMaximumWidth(420)
        self.filter_edit.setPlaceholderText("text, or fmt:vdi size:>10G vm:alpha")
        self.filter_edit.setToolTip(
            "Plain words match any column. Add terms to narrow: "
            + ", ".join(sorted(set(FILTER_FIELDS) | set(FILTER_SIZES)))
            + ". Sizes take <, <=, > and >= with K/M/G suffixes."
        )
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
        self.runner.skip_requested.connect(self.skip_queue)
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
        for pane in self.panes.values():
            pane.set_details_visible(self.act_details.isChecked())

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
        for act in (self.act_create, self.act_fromraw, self.act_import_compressed,
                    self.act_compact, self.act_compact_all,
                    self.act_resize, self.act_convert, self.act_props, self.act_move,
                    self.act_encrypt, self.act_create_diff, self.act_export_raw,
                    *(getattr(self, name) for name in ("act_flatten", "act_golden")
                      if hasattr(self, name))):
            act.setEnabled(is_disk)
        # formatfat writes a FAT filesystem; DVDs are read-only images.
        self.act_formatfat.setEnabled(self.current_kind() in ("disk", "floppy"))

    def _show_context_menu(self, pane: MediaPane, pos):
        menu = QMenu(self)
        for act in (self.act_info, self.act_contents, self.act_props,
                    self.act_medium_props, self.act_attach, self.act_detach):
            menu.addAction(act)
        menu.addSeparator()
        for act in (self.act_compact, self.act_resize, self.act_convert,
                    self.act_move, self.act_encrypt, self.act_create_diff,
                    self.act_formatfat, self.act_resolve, self.act_flatten,
                    self.act_verify, self.act_browse,
                    *self._tools_context_actions()):
            menu.addAction(act)
        menu.addSeparator()
        for act in (self.act_copy_uuid, self.act_copy_path, self.act_copy_rows,
                    self.act_open_dir):
            menu.addAction(act)
        menu.addSeparator()
        menu.addAction(self.act_remove)
        menu.addAction(self.act_undo_delete)
        menu.addAction(self.act_undo_change)
        menu.exec(pane.tree.viewport().mapToGlobal(pos))

    def _toggle_details(self, on: bool):
        get_settings().setValue("ui/details", on)
        for pane in self.panes.values():
            pane.set_details_visible(on)

    def _toggle_plan_mode(self, on: bool):
        set_plan_mode(on)
        self.statusBar().showMessage(
            "Plan mode on: batches show their commands instead of running."
            if on else "Plan mode off."
        )

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

    def selected_media(self) -> list[MediumRecord]:
        return self.current_pane().selected_records()

    def _require_selection_multi(self) -> list[MediumRecord]:
        """Every selected medium, or the focused one when nothing is selected."""
        media = self.selected_media()
        if not media:
            one = self.selected_medium()
            media = [one] if one else []
        if not media:
            QMessageBox.information(self, "No selection", "Select a medium first.")
        return media

    def _require_selection(self) -> MediumRecord | None:
        m = self.selected_medium()
        if not m:
            QMessageBox.information(self, "No selection", "Select a medium first.")
            return None
        return m

    def export_listing(self):
        """Write the current tab's listing to CSV or JSON.

        The whole tab, not the filter or the selection: an export that silently
        dropped rows because a filter was set would be worse than no export.
        """
        kind = self.current_kind()
        records = self.panes[kind].records
        if not records:
            QMessageBox.information(self, "Export", "This tab has no media to export.")
            return
        default = default_image_path(
            "export", f"vboxfront-{KIND_META[kind]['list']}.csv"
        )
        path, chosen = QFileDialog.getSaveFileName(
            self, "Export listing", default, "CSV (*.csv);;JSON (*.json)"
        )
        if not path:
            return
        # A typed extension wins over the filter combo: picking the JSON
        # filter and then typing "report.csv" asked for a CSV, and writing
        # JSON into it was a surprise nothing on screen explained.
        suffix = os.path.splitext(path)[1].lower()
        if suffix == ".json":
            as_json = True
        elif suffix == ".csv":
            as_json = False
        else:
            as_json = "json" in (chosen or "").lower()
            if not suffix:
                path += ".json" if as_json else ".csv"
        text = records_to_json(records) if as_json else records_to_csv(records)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as e:
            QMessageBox.critical(self, "Export failed", f"Could not write {path}: {e}")
            return
        remember_folder("export", path)
        self.runner.note(f"[export] {len(records)} row(s) → {path}")
        self.statusBar().showMessage(f"Exported {len(records)} row(s) to {path}")

    def copy_rows_selected(self):
        records = self.selected_media() or self.panes[self.current_kind()].records
        if not records:
            return
        QApplication.clipboard().setText(records_to_tsv(records))
        self.statusBar().showMessage(f"Copied {len(records)} row(s) as TSV")

    def open_history(self):
        dlg = CommandHistoryDialog(self)
        if dlg.exec() and dlg.rerun and self._require_idle():
            self.runner.run(dlg.rerun)

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
            self._auto_purge_trash()
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
            # Killing the process on the way out skips QProcess.finished, and
            # with it the cleanup that normally runs there — a password file
            # would outlive the app that wrote it.
            self.skip_queue()
            self.runner.cancel()
            self.runner.discard_secrets()
        if self.image_mounts.mounts:
            # A FUSE mount outlives the app that made it; leaving one behind
            # keeps the image open in VBoxSVC.
            problems = self.unmount_browsed()
            if problems and QMessageBox.question(
                self, "Still mounted",
                "Could not unmount:\n\n" + "\n".join(problems) + "\n\nQuit anyway?",
            ) != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._save_ui_state()
        super().closeEvent(event)
