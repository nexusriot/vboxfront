"""VBoxFront window."""

from __future__ import annotations

from PyQt6.QtCore import QByteArray, QDateTime, Qt, QUrl
from PyQt6.QtGui import QAction, QDesktopServices, QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QWidget,
)
import os

from .app_dialogs import (
    CommandHistoryDialog,
    HealthDialog,
    OrphanScanDialog,
    PlanDialog,
    SettingsDialog,
)
from .attach_dialog import AttachDialog
from .commands import (
    detach_args,
    export_raw_args,
    list_media_args,
    mediumio_formatfat_args,
    move_args,
    repairhd_args,
)
from .constants import (
    DISK_EXT,
    DISK_FORMATS,
    KINDS,
    KIND_META,
    LONG_OPERATION_SECONDS,
    MEDIA_EXTENSIONS,
    RAW_EXTENSIONS,
)
from .disk_dialogs import (
    BatchConvertDialog,
    ConvertDialog,
    CreateDiffDialog,
    CreateDiskDialog,
    CreateFloppyDialog,
    ImportCompressedDialog,
    ResizeDialog,
)
from .domain import (
    FILTER_FIELDS,
    FILTER_SIZES,
    child_names,
    discard_findings,
    find_attachment,
    format_duration,
    format_mbytes,
    is_differencing,
    is_inaccessible,
    parse_capacity_mb,
    parse_hddbackends,
    parse_in_use_entry,
    parse_machinereadable,
    parse_media_list,
    parse_systemproperties,
    parse_vms_list,
    reclaim_line,
    records_to_csv,
    records_to_json,
    records_to_tsv,
    registered_at,
    registry_fingerprint,
    same_file,
    snapshot_only,
    snapshot_word,
)
from .media_dialogs import (
    ContentsDialog,
    EncryptDialog,
    MediumPropertyDialog,
    PropertiesDialog,
    RemoveDialog,
    ResolveDialog,
    UuidToolsDialog,
)
from .models import HealthFinding, MediumBackend, MediumRecord, TrashedFile
from .persistence import (
    add_library_path,
    append_history,
    default_image_path,
    get_settings,
    load_library,
    move_to_trash,
    plan_mode,
    remember_folder,
    remembered_folder,
    remove_library_path,
    restore_trashed,
    set_plan_mode,
    snapshot_dir,
    trash_enabled,
    vboxmanage_path,
    vboxmanage_problem,
    write_registry_snapshot,
)
from .processes import CaptureRunner, CommandRunner
from .version import APP_VERSION
from .features import FeatureActionsMixin
from .batches import BatchReport
from .widgets import MediaPane, confirm_space


class MainWindow(FeatureActionsMixin, QMainWindow):
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
                    self.act_encrypt, self.act_create_diff, self.act_export_raw):
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
                    self.act_formatfat, self.act_resolve):
            menu.addAction(act)
        menu.addSeparator()
        for act in (self.act_copy_uuid, self.act_copy_path, self.act_copy_rows,
                    self.act_open_dir):
            menu.addAction(act)
        menu.addSeparator()
        menu.addAction(self.act_remove)
        menu.addAction(self.act_undo_delete)
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

    def _snapshot_registry(self, reason: str):
        """Write the registry aside before something destructive runs.

        Never a reason to stop: a snapshot that cannot be written is noted
        and the operation goes ahead, because refusing to delete a medium
        because a log file failed would be the worse behaviour.
        """
        path = write_registry_snapshot(self.media, reason)
        if path:
            self.runner.note(f"[snapshot] registry saved to {path}")
        else:
            self.runner.note(
                f"[snapshot] could not write a registry snapshot to "
                f"{snapshot_dir()}; continuing anyway"
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
            # Asked alongside the backends, and only once: it is what makes
            # "where should this new disk go" a sensible answer instead of ~.
            self._chain.append((["list", "systemproperties"], self._sysprops_done))
        for kind in KINDS:
            for path in load_library(kind):
                # One process per library entry, every refresh, was the single
                # biggest cost of F5 on a large library — and almost all of it
                # was re-opening files that had not moved since. The stamp is
                # dropped whenever the probe fails, so a broken entry is
                # retried rather than remembered as fine.
                if self._library_unchanged(path):
                    continue
                self._chain.append((["showmediuminfo", kind, path],
                                    self._library_open_done(kind, path)))
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

    def _sysprops_done(self, code: int, output: str):
        """Cache VirtualBox's default machine folder for `image_folder()`."""
        if code != 0:
            return
        folder = parse_systemproperties(output).get("Default machine folder", "")
        if folder:
            get_settings().setValue("vbox/machinefolder", folder)

    def _library_stamp(self, path: str) -> tuple[float, int] | None:
        try:
            info = os.stat(path)
        except OSError:
            return None
        return (info.st_mtime, info.st_size)

    def _library_unchanged(self, path: str) -> bool:
        """True when this entry was opened before and the file has not moved.

        VBoxSVC forgets a standalone registration when it exits, so an
        unchanged file still has to be re-opened *once per VBoxSVC lifetime* —
        which is why the cache is cleared whenever a listing comes back
        without the medium in it (see `_finish_refresh`).
        """
        stamp = self._library_stamp(path)
        return stamp is not None and self._library_seen.get(path) == stamp

    def _library_open_done(self, kind: str, path: str):
        def done(code: int, _output: str):
            if code != 0:
                self.runner.note(f"[library] could not open {kind} {path} (exit {code})")
                self._library_seen.pop(path, None)
                return
            stamp = self._library_stamp(path)
            if stamp is not None:
                self._library_seen[path] = stamp
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
        self._report_reclaimed()
        self._check_drift()
        self._forget_vanished_library_entries()
        self._refreshing = False
        if self._refresh_pending:
            self._refresh_pending = False
            self.refresh_media()

    def _check_drift(self):
        """Say so when the registry changed without a command of ours.

        A stale row is how an action lands on the wrong medium, and this
        window is not the only thing that writes the registry: the VirtualBox
        GUI, a VM starting, or a second copy of this app all do.
        """
        fingerprint = registry_fingerprint(self.media)
        if (self._fingerprint and fingerprint != self._fingerprint
                and not self._expect_change):
            self.runner.note(
                "[registry] changed since the last refresh, and not by "
                "anything run here — the VirtualBox GUI, a VM starting, or "
                "another copy of VBoxFront. The list above is current."
            )
        self._fingerprint = fingerprint
        self._expect_change = False

    def _forget_vanished_library_entries(self):
        """Drop cached stamps for library paths the listing does not show.

        That is what a VBoxSVC restart looks like from here: the file is
        unchanged but the registration is gone, so the entry has to be
        re-opened on the next refresh rather than skipped as "seen".
        """
        present = {
            rec.location for records in self.media.values() for rec in records
        }
        for path in list(self._library_seen):
            if path not in present:
                self._library_seen.pop(path, None)

    def _report_reclaimed(self):
        """Say what a compact actually gave back.

        Measured after the fact rather than predicted before it: `list -l`
        cannot see blocks that were allocated and then freed inside the guest,
        which is exactly what compacting reclaims.
        """
        if not self._size_before:
            return
        by_uuid = {r.uuid: r for r in self.media["disk"]}
        for uuid, (name, before) in self._size_before.items():
            rec = by_uuid.get(uuid)
            after = parse_capacity_mb(rec.size_on_disk) if rec else None
            line = reclaim_line(name, before, after)
            if line:
                self.runner.note(line)
        self._size_before = {}

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
        output = next((a.partition("=")[2] for a in args if a.startswith("--output=")), "")
        occupant = registered_at(output, [r for records in self.media.values() for r in records]) if output else None
        if occupant:
            QMessageBox.warning(
                self, "Target is a registered medium",
                f"{output} is the backing file of UUID {occupant.uuid}. Choose another file.",
            )
            return
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
            self.runner.run(dlg.args(m.uuid))

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
        self.runner.run(dlg.args(m.uuid))

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

    def edit_properties_selected(self):
        media = self._require_selection_multi()
        if not media:
            return
        dlg = PropertiesDialog(media, self)
        if not dlg.exec():
            return
        commands = dlg.commands(self.current_kind())
        if not commands:
            self.statusBar().showMessage("Properties unchanged.")
            return
        self._run_queue([(c, None, None) for c in commands], "change properties")

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
        media = self._require_selection_multi()
        if not media:
            return
        m = self.selected_medium() or media[0]
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
                dlg = AttachDialog(kind, m, vms, running, self._capture, self,
                                   extra=media)
                if not dlg.exec():
                    return
                commands = dlg.commands()
                if len(commands) == 1:
                    if self._require_idle():
                        self.runner.run(commands[0])
                    return
                self._run_queue([(c, None, None) for c in commands],
                                "attach the selected media")

            self._capture(["list", "runningvms"], running_done)

        self._capture(["list", "vms"], vms_done)

    def detach_selected(self):
        media = self._require_selection_multi()
        if not media:
            return
        if len(media) > 1:
            self._detach_batch(media)
            return
        m = media[0]
        attachments = [e for e in (parse_in_use_entry(x) for x in m.in_use) if e]
        if not attachments:
            QMessageBox.information(self, "Detach", "This medium is not attached to any VM.")
            return
        if snapshot_only(m):
            # storageattach only edits the current state, so there is no slot
            # to clear. Saying that beats "could not locate the attachment
            # slot", which reads like a bug in the app.
            QMessageBox.information(
                self, "Held by a snapshot",
                f"{os.path.basename(m.location) or m.uuid} is attached only "
                f"inside the snapshot “{snapshot_word(m)}”, not in any VM's "
                "current state.\n\nVBoxManage can only detach from the "
                "current state, so nothing here can release it — deleting or "
                "restoring that snapshot in the VirtualBox GUI is what frees "
                "the medium.",
            )
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

    def _detach_batch(self, media: list[MediumRecord]):
        """Detach every attachment of every selected medium.

        One `showvminfo` per VM rather than per attachment: the slot of each
        medium is found in the same dump, and a VM holding six of them should
        not be read six times.
        """
        snapshot_held = [m for m in media if snapshot_only(m)]
        pairs = []
        for m in media:
            if m in snapshot_held:
                continue
            for entry in m.in_use:
                parsed = parse_in_use_entry(entry)
                if parsed:
                    pairs.append((m, parsed[0], parsed[1]))
        if snapshot_held:
            # Separated out before the slot lookups rather than reported as
            # "could not locate" afterwards: there is no slot to find.
            self.runner.note(
                "[detach] held only by a snapshot, which storageattach cannot "
                "reach: "
                + ", ".join(os.path.basename(m.location) or m.uuid
                            for m in snapshot_held)
            )
        if not pairs:
            QMessageBox.information(
                self, "Detach", "None of the selected media are attached to a VM."
            )
            return
        listing = "\n".join(
            f"{os.path.basename(m.location) or m.uuid} ← {vm}" for m, vm, _u in pairs[:12]
        )
        ret = QMessageBox.question(
            self, "Detach",
            f"Detach {len(pairs)} attachment(s)?\n\n{listing}"
            + ("\n…" if len(pairs) > 12 else ""),
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

        def infos_ready(infos: dict[str, dict[str, str]]):
            entries, missing = [], []
            for m, vm_name, vm_uuid in pairs:
                info = infos.get(vm_uuid)
                slot = find_attachment(info, m.uuid) if info else None
                if not slot:
                    missing.append(f"{os.path.basename(m.location) or m.uuid} in {vm_name}")
                    continue
                ctl, port, device = slot
                entries.append((detach_args(vm_uuid, ctl, port, device), None, None))
            if missing:
                self.runner.note(
                    "[detach] could not locate: " + "; ".join(missing)
                )
            if not entries:
                QMessageBox.warning(
                    self, "Detach", "No attachment slot could be located."
                )
                return
            self._run_queue(entries, "detach the selected media")

        self._collect_vminfo(sorted({u for _m, _n, u in pairs}), infos_ready)

    def remove_selected(self):
        media = self._require_selection_multi()
        if not media:
            return
        kind = self.current_kind()
        removable, blocked = [], []
        for m in media:
            if m.in_use:
                names = ", ".join(
                    e[0] for e in (parse_in_use_entry(x) for x in m.in_use) if e
                )
                blocked.append(
                    f"{os.path.basename(m.location) or m.uuid}: attached to {names}"
                )
                continue
            children = child_names(self.media[kind], m.uuid)
            if children:
                # closemedium answers VBOX_E_OBJECT_IN_USE for these; say which
                # child holds it rather than passing the COM error along.
                blocked.append(
                    f"{os.path.basename(m.location) or m.uuid}: "
                    f"{', '.join(children)} build{'s' if len(children) == 1 else ''} on it"
                )
                continue
            removable.append(m)
        if blocked and not removable:
            QMessageBox.warning(
                self, "Cannot remove",
                "Nothing here can be removed yet:\n\n" + "\n".join(blocked),
            )
            return
        if blocked:
            ret = QMessageBox.question(
                self, "Some cannot be removed",
                f"{len(blocked)} of {len(media)} media cannot be removed:\n\n"
                + "\n".join(blocked)
                + f"\n\nRemove the other {len(removable)}?",
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        dlg = RemoveDialog(removable, self)
        if not dlg.exec():
            return
        # With the trash on, closemedium is run *without* `--delete` and the
        # file is moved aside afterwards instead: VBoxManage's own delete
        # unlinks, and nothing can be undone after that.
        use_trash = dlg.delete_file and trash_enabled()
        batch: list[TrashedFile] = []
        entries = []
        for m in removable:
            args = ["closemedium", kind, m.uuid]
            if dlg.delete_file and not use_trash:
                args.append("--delete")
            # Per-command, not a shared pending slot: in a batch the shared one
            # would be claimed by whichever command finished.
            entries.append((
                args, None,
                self._removal_followup(kind, m.location, batch if use_trash else None),
            ))
        # After the queue starts, never before: a refused queue would leave an
        # undo entry for a delete that is not going to happen. The follow-ups
        # hold the same list, and none of them can run before the event loop
        # turns again.
        self._snapshot_registry("remove media")
        if self._run_queue(entries, "remove the selected media") and use_trash:
            self._trash.append(batch)
            self._trash_store.save(self._trash)

    def _removal_followup(self, kind: str, location: str,
                          batch: list[TrashedFile] | None):
        """Bookkeeping for one medium, once *its* closemedium has succeeded.

        The library entry goes either way. The backing file is only moved
        aside when the delete was asked for and the trash is on, and only
        here — before the command, VirtualBox still holds the medium, and the
        unregister could still fail with the file already gone.
        """
        def done():
            remove_library_path(kind, location)
            if batch is None or not location:
                return
            if not os.path.exists(location):
                self.runner.note(f"[trash] {location} was already gone")
                return
            try:
                dest = move_to_trash(location)
            except OSError as e:
                self.runner.note(
                    f"[trash] could not move {location}: {e} — the medium is "
                    "unregistered but the file was left where it is"
                )
                return
            batch.append(TrashedFile(kind, location, dest))
            self._trash_store.save(self._trash)
            self.runner.note(
                f"[trash] moved {location} → {dest} "
                "(Media → Undo delete puts it back)"
            )
            self._sync_undo_action()
        return done

    def undo_delete(self):
        """Put back the files the most recent delete moved aside."""
        if not self._require_idle():
            return
        batch = self._take_trash_batch()
        if not batch:
            self._sync_undo_action()
            return
        restored, failed = [], []
        for entry in batch:
            try:
                restore_trashed(entry)
            except OSError as e:
                self.runner.note(
                    f"[trash] could not restore {entry.original}: {e} — "
                    f"it is still at {entry.trashed}"
                )
                failed.append(entry)
                continue
            # The medium was unregistered by the remove, so putting the file
            # back is only half of it: the library entry is what re-opens it
            # on the next refresh and brings the row back.
            add_library_path(entry.kind, entry.original)
            self.runner.note(f"[trash] restored {entry.trashed} → {entry.original}")
            restored.append(entry)
        if failed:
            # Still in the trash, so still undoable: a second try can work
            # once whatever blocked this one is out of the way.
            self._trash.append(failed)
        self._trash_store.save(self._trash)
        self._sync_undo_action()
        if restored:
            self.statusBar().showMessage(
                f"Restored {len(restored)} file(s) from the trash."
            )
            self.refresh_media()
        if failed:
            QMessageBox.warning(
                self, "Undo delete",
                f"{len(failed)} of {len(batch)} files could not be put back; "
                "the output panel says why. They are still in the trash.",
            )

    def _take_trash_batch(self) -> list[TrashedFile]:
        """The newest batch that actually holds something, removed from the stack.

        A Remove whose files could not be moved (or that never ran) leaves an
        empty batch behind; those are skipped rather than counted as an undo
        that did nothing.
        """
        while self._trash and not self._trash[-1]:
            self._trash.pop()
        return self._trash.pop() if self._trash else []

    def _sync_undo_action(self):
        self.act_undo_delete.setEnabled(any(self._trash))

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

    def open_health(self, extra: list[HealthFinding] | None = None):
        dlg = HealthDialog(self.media, self)
        if extra:
            # Findings from the attach-flag scan, which needed a command and
            # so could not be there when the dialog first opened.
            dlg.add_findings(extra)
            dlg.repopulate()
        if not dlg.exec():
            return
        if dlg.reveal:
            kind, uuid = dlg.reveal
            self.tabs.setCurrentIndex(KINDS.index(kind))
            self.panes[kind].select_uuid(uuid)
            return
        if dlg.prune:
            # Plain closemedium: the file is already gone, so there is nothing
            # for --delete to remove and asking for it would only fail.
            self.runner.note(
                f"[health] unregistering {len(dlg.prune)} medium(s) whose "
                "backing file is gone"
            )
            self._snapshot_registry("health cleanup")
            # No trash batch: the file is already gone, so there is nothing
            # to move aside — the follow-up only prunes the library entry.
            self._run_queue(
                [(["closemedium", kind, uuid], None,
                  self._removal_followup(kind, self._location_of(kind, uuid), None))
                 for kind, uuid in dlg.prune],
                "unregister media whose file is gone",
            )
            return
        if dlg.want_discard_scan:
            self.scan_attach_flags()
            return
        if dlg.dry_run_paths:
            # -dry-run reads and reports; it never writes, so a batch of them is
            # safe to queue unattended.
            self.runner.note(dlg.dry_run_caveat())
            self._run_queue(
                [(repairhd_args(path, fmt="VDI", dry_run=True), None, None)
                 for path in dlg.dry_run_paths],
                "check the VDI images",
            )

    def _location_of(self, kind: str, uuid: str) -> str:
        for rec in self.media.get(kind, []):
            if rec.uuid == uuid:
                return rec.location
        return ""

    def scan_attach_flags(self):
        """Read every VM and report disks attached without `--discard`.

        The one health check that cannot come from the registry: whether a
        slot passes the guest's TRIM through lives in `showvminfo`, not in
        `list -l`. Reopens the health dialog with the answer folded in.
        """
        def vms_done(code: int, output: str):
            if code != 0:
                self.runner.note(output)
                return
            vms = parse_vms_list(output)
            if not vms:
                QMessageBox.information(
                    self, "Attach flags", "No virtual machines are registered."
                )
                return

            def infos_ready(infos: dict[str, dict[str, str]]):
                findings = discard_findings(self.media, infos)
                if not findings:
                    QMessageBox.information(
                        self, "Attach flags",
                        f"Checked {len(infos)} VM(s): every attached disk "
                        "passes the guest's TRIM through.",
                    )
                    return
                self.open_health(findings)

            self._collect_vminfo([uuid for _name, uuid in vms], infos_ready)

        self._capture(["list", "vms"], vms_done)

    def open_orphan_scan(self):
        known = [rec.location for records in self.media.values() for rec in records]
        for kind in KINDS:
            known += load_library(kind)
        dlg = OrphanScanDialog(known, self, capture=self._capture)
        if not dlg.exec() or not dlg.chosen:
            return
        for kind, path in dlg.chosen:
            add_library_path(kind, path)
            self.runner.note(f"[library] added {kind} {path}")
        self.refresh_media()

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
        self._save_ui_state()
        super().closeEvent(event)

    def _notify_long_operation(self, code: int):
        """Flag up a command that ran long enough for the user to look away.

        No tray icon and no desktop notification service: the window itself
        asks for attention, which works the same everywhere and needs nothing
        installed.
        """
        verdict = "finished" if code == 0 else f"failed (exit {code})"
        elapsed = format_duration(self.runner.elapsed_seconds())
        self.runner.note(f"[done] {verdict} after {elapsed}")
        self.statusBar().showMessage(f"Command {verdict} after {elapsed}.")
        QApplication.alert(self)

    def _run_queue(self, entries: list[tuple[list[str], bytes | None, object]],
                   title: str = "", before_each=None) -> bool:
        """Run commands one after another, stopping at the first failure.

        Each entry carries its own follow-up so a batch can do per-command
        bookkeeping (dropping a library entry for the medium *that* command
        removed, say) without a shared slot that the next command would claim.

        In plan mode the list is shown first and nothing runs until the user
        says so from there. before_each(args) prepares only the command about
        to run; returning False aborts the queue without launching it.
        """
        if not entries or not self._require_idle():
            return False
        if plan_mode():
            dlg = PlanDialog([args for args, _stdin, _follow in entries],
                             title, self)
            if dlg.exec() != PlanDialog.RUN:
                self.statusBar().showMessage("Plan shown; nothing was run.")
                return False
            if not self._require_idle():
                return False
        self._batch_report = BatchReport(entries, title)
        self._batch_current = None
        self._last_batch_prepare = before_each
        self._queue_before = before_each
        self._cmd_queue = list(entries)
        return self._start_queued_command()

    def _start_queued_command(self) -> bool:
        self._batch_current = self._batch_report.start() if self._batch_report else None
        args, stdin_data, followup = self._cmd_queue.pop(0)
        if self._queue_before and not self._queue_before(args):
            if self._batch_current:
                self._batch_current.state, self._batch_current.code = "failed", 1
                self._batch_report.skip_waiting()
                self._batch_current = None
            self._cmd_queue = []
            self._queue_before = None
            self._followup = None
            self.runner.note("[queue] aborted: could not prepare the next command")
            self._show_queue_status()
            self.refresh_media()
            return False
        self._followup = followup
        started = self.runner.run(args, stdin_data)
        self._show_queue_status()
        return started

    def _show_queue_status(self):
        self.runner.show_queue([a for a, _stdin, _follow in self._cmd_queue])
        if self._cmd_queue:
            self.statusBar().showMessage(f"Queue: {len(self._cmd_queue)} command(s) pending.")

    def skip_queue(self):
        """Drop everything still queued, leaving the running command alone."""
        dropped, self._cmd_queue = len(self._cmd_queue), []
        if self._batch_report:
            self._batch_report.skip_waiting()
        if dropped:
            self.runner.note(f"[queue] skipped {dropped} remaining command(s)")
            self.statusBar().showMessage(f"Skipped {dropped} queued command(s).")
        self._show_queue_status()

    def _collect_vminfo(self, vm_uuids: list[str], done):
        """Read `showvminfo` for several VMs in turn, then hand over the lot.

        Sequential rather than parallel for the same reason the refresh chain
        is: one VBoxSVC, and a burst of clients against it is how the service
        gets wedged.
        """
        infos: dict[str, dict[str, str]] = {}
        pending = list(vm_uuids)

        def step():
            if not pending:
                done(infos)
                return
            uuid = pending.pop(0)

            def got(code: int, output: str):
                if code == 0:
                    infos[uuid] = parse_machinereadable(output)
                step()

            if not self._capture(["showvminfo", uuid, "--machinereadable"], got):
                done(infos)

        step()

    def _on_runner_finished(self, code: int):
        code = self._finish_output(code)
        was_batch = self._batch_current is not None
        if self._batch_current:
            self._batch_current.code = code
            self._batch_current.state = "succeeded" if code == 0 else "failed"
            self._batch_current = None
        # Only what CommandRunner ran, which is only ever a mutating command:
        # the read-only refresh traffic goes through CaptureRunner and would
        # bury the history under `list`/`showvminfo` noise on every F5.
        if self.runner.last_command:
            append_history(
                self.runner.last_command,
                code,
                QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss"),
            )
        # Our own command just ran, so whatever the next refresh finds
        # changed is explained and should not be reported as drift.
        self._expect_change = True
        if self.runner.elapsed_seconds() >= LONG_OPERATION_SECONDS:
            self._notify_long_operation(code)
        followup, self._followup = self._followup, None
        if code == 0:
            if followup:
                followup()
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
        else:
            self._pending_library_add = None
            self._pending_library_move = None
            if self._cmd_queue:
                self.runner.note(f"[queue] aborted, {len(self._cmd_queue)} command(s) dropped")
                self._cmd_queue = []
                if self._batch_report:
                    self._batch_report.skip_waiting()
                self.runner.show_queue([])

        if self._cmd_queue:
            self._start_queued_command()
            return
        self._queue_before = None
        self.runner.show_queue([])
        if was_batch and self._batch_report:
            self.runner.note("[batch] " + self._batch_report.summary() + "; File → Batch results")
        # Most operations change the registry; refresh quietly.
        self.refresh_media()


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
