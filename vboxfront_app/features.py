"""Optional library views and recovery actions hosted by MainWindow.

Services own persistence and filesystem changes; this mixin connects them to
menus and dialogs without putting more feature flows in the window shell.
"""
from __future__ import annotations
import os
from PyQt6.QtWidgets import QComboBox, QInputDialog, QMessageBox, QFileDialog, QPushButton
from .persistence import user_trash_dir, snapshot_dir, load_library, add_library_path
from .recovery import OperationJournal
from .trash import TrashStore
from .library_views import SavedFilterStore
from .batches import BatchReport
from .feature_dialogs import (
    TrashManagerDialog,
    RecoveryDialog,
    BatchResultsDialog,
    SnapshotComparisonDialog,
    VMStorageDialog,
)
from .snapshots import load_snapshot, compare_registry, current_rows
from .commands import attach_args, detach_args
from .compression import open_maybe_compressed
from .domain import parse_vms_list, parse_machinereadable, slot_occupant, same_file


class FeatureActionsMixin:
    def _init_features(self):
        self._trash_store = TrashStore()
        self._trash = self._trash_store.load()
        self._sync_undo_action()
        self._journal_error = ''
        try:
            self.operation_journal = OperationJournal(os.path.join(os.path.dirname(user_trash_dir()), 'operations.json'))
        except (OSError, ValueError, TypeError, KeyError) as e:
            self.operation_journal = None
            self._journal_error = str(e)
            self.runner.note(f'[recovery] cannot read the operation journal: {e}')
        self._active_operation = None
        self._archive_context = None
        self.runner.before_run = self._before_command
        self._batch_report = None
        self._batch_current = None
        self._last_batch_prepare = None
        file_menu = self.menuBar().actions()[0].menu()
        media_menu = self.menuBar().actions()[1].menu()
        file_menu.addSeparator()
        for label, action in [('Operation recovery…', self.open_recovery),
                              ('Batch results…', self.open_batch_results),
                              ('Compare registry snapshot…', self.compare_snapshot)]:
            file_menu.addAction(self._action(label, action))
        media_menu.addAction(self._action('Trash manager…', self.open_trash))
        media_menu.addAction(self._action('VM storage…', self.open_vm_storage))
        self._filter_store = SavedFilterStore()
        toolbar = self.filter_edit.parentWidget()
        # QToolBar owns the existing filter widget.
        self.saved_filters = QComboBox()
        self.saved_filters.setMaximumWidth(170)
        self.saved_filters.activated.connect(self.apply_saved_filter)
        toolbar.addWidget(self.saved_filters)
        save = QPushButton('Save filter…')
        save.clicked.connect(self.save_filter)
        toolbar.addWidget(save)
        delete = QPushButton('Delete filter')
        delete.clicked.connect(self.delete_filter)
        toolbar.addWidget(delete)
        self.reload_saved_filters()
        if self.operation_journal and self.operation_journal.unresolved():
            self.runner.note('[recovery] unfinished operations are available in File → Operation recovery')

    def reload_saved_filters(self):
        self.saved_filters.clear()
        self.saved_filters.addItem('Saved filters…', None)
        for name, expression in sorted(self._filter_store.load().items()):
            self.saved_filters.addItem(name, expression)

    def apply_saved_filter(self, index):
        expression = self.saved_filters.itemData(index)
        if expression is not None:
            self.filter_edit.setText(expression)

    def save_filter(self):
        name, ok = QInputDialog.getText(self, 'Save filter', 'Name for this filter:')
        if not ok or not name.strip():
            return
        if name.strip() in self._filter_store.load() and QMessageBox.question(self,
                'Replace saved filter?', f'Replace {name.strip()}?') != QMessageBox.StandardButton.Yes:
            return
        self._filter_store.save(name, self.filter_edit.text())
        self.reload_saved_filters()

    def delete_filter(self):
        name = self.saved_filters.currentText()
        if self.saved_filters.currentIndex() > 0 and QMessageBox.question(self,
                'Delete saved filter?', f'Delete {name}?') == QMessageBox.StandardButton.Yes:
            self._filter_store.delete(name)
            self.reload_saved_filters()

    def open_trash(self):
        if not self._require_idle():
            return
        TrashManagerDialog(self._trash_store, self).exec()
        self._trash = self._trash_store.load()
        self._sync_undo_action()
        self.refresh_media()

    def open_recovery(self):
        if not self._require_idle():
            return
        if self.operation_journal is None:
            QMessageBox.warning(self, 'Recovery unavailable', self._journal_error)
            return
        dlg = RecoveryDialog(self.operation_journal, self)
        dlg.exec()
        if dlg.retry:
            self._retry_operation(dlg.retry)
        self.refresh_media()

    def _retry_operation(self, op):
        if not self._require_idle():
            return
        # Backing-file safety is checked again against the current registry.
        registered = [r for rows in self.media.values() for r in rows if same_file(r.location, op.target)]
        if registered:
            QMessageBox.warning(self, 'Registered target', 'Choose a new target from the original operation dialog.')
            return
        if op.partial == op.target and os.path.lexists(op.target):
            QMessageBox.warning(self, 'Partial output exists', 'Inspect and remove the partial output before retrying.')
            return
        if op.source_archive:
            try:
                stream = open_maybe_compressed(op.source_archive)
            except (OSError, ValueError) as e:
                QMessageBox.warning(self, 'Cannot read archive', str(e))
                return
            if os.path.lexists(op.target) and not self._clear_target(op.target):
                stream.close()
                return
            self._archive_context = (op.source_archive, op.size)
            self._pending_library_add = ('disk', op.target)
            self.runner.run(op.args, stdin_stream=stream, stdin_total=op.size)
        else:
            if os.path.lexists(op.target) and not self._clear_target(op.target):
                return
            if op.args and op.args[0] == 'convertfromraw':
                self._pending_library_add = ('disk', op.target)
            self.runner.run(op.args)

    def _before_command(self, args):
        target = ''
        if args and args[0] == 'clonemedium' and '--existing' not in args and len(args) > 3:
            target = args[3]
        elif args and args[0] == 'convertfromraw' and len(args) > 2:
            target = args[2]
        if not target:
            return
        if self.operation_journal is None:
            raise OSError('Recovery journal unavailable: ' + self._journal_error)
        archive, size = self._archive_context or ('', 0)
        self._archive_context = None
        if self._active_operation is None:
            self._active_operation = self.operation_journal.begin(args, target,
                source_archive=archive, size=size)
        else:
            op = self._active_operation
            if not same_file(op.target, target):
                raise OSError('Another output replacement is still pending')
            op.args = list(args)
            op.source_archive, op.size = archive, size
            self.operation_journal.save(op)

    def _finish_output(self, code):
        op, self._active_operation = self._active_operation, None
        if op is None:
            return code
        try:
            result = self.operation_journal.finish(op, code)
        except OSError as e:
            self.runner.note(f'[recovery] {e}; backup/partial paths remain in Operation recovery')
            return code or 1
        if result:
            self.runner.note(f'[recovery] {op.error}; partial: {op.partial or "none"}; original restored when a backup existed')
        return result

    def open_batch_results(self):
        if not self._batch_report:
            QMessageBox.information(self, 'Batch results', 'No batch has run yet.')
            return
        if not self._require_idle():
            return
        dlg = BatchResultsDialog(self._batch_report, self)
        dlg.exec()
        if dlg.retry:
            self._run_queue(dlg.retry, 'retry failed commands', before_each=self._last_batch_prepare)

    def compare_snapshot(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Compare registry snapshot', snapshot_dir(), 'JSON snapshots (*.json)')
        if not path:
            return
        try:
            changes = compare_registry(load_snapshot(path), current_rows(self.media))
        except (OSError, ValueError, TypeError, KeyError) as e:
            QMessageBox.warning(self, 'Cannot compare snapshot', str(e))
            return
        SnapshotComparisonDialog(changes, self).exec()

    def open_vm_storage(self):
        if not self._require_idle():
            return
        def got_vms(code, output):
            if code:
                self.runner.note(output)
                return
            vms = parse_vms_list(output)
            def got_infos(infos):
                dlg = VMStorageDialog(vms, infos, self.media, self)
                dlg.exec()
                if dlg.requested:
                    self._apply_storage_request(dlg.requested)
            self._collect_vminfo([ident for _, ident in vms], got_infos)
        self._capture(['list', 'vms'], got_vms)

    def _apply_storage_request(self, request):
        operation, slot, kind, medium = request
        def checked(code, output):
            if code:
                self.runner.note(output)
                return
            info = parse_machinereadable(output)
            current = slot_occupant(info, slot.controller, slot.port, slot.device)
            if current != slot.medium:
                QMessageBox.warning(self, 'Storage changed', 'This slot changed while the view was open. Reopen VM storage.')
                return
            args = (attach_args(slot.vm, slot.controller, slot.port, slot.device, kind, medium)
                    if operation == 'attach' else detach_args(slot.vm, slot.controller, slot.port, slot.device))
            self._run_queue([(args, None, None)], f'{operation} VM storage')
        self._capture(['showvminfo', slot.vm, '--machinereadable'], checked)
