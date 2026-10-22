"""Dialogs for recovery, persistent trash, batch results and registry comparison."""
from __future__ import annotations
import os
import shlex
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QTreeWidget,
    QTreeWidgetItem,
    QLabel,
    QPushButton,
    QMessageBox,
    QInputDialog,
)
from .domain import format_bytes, rerun_blocker, is_destructive_command


def action_row(layout, actions):
    row = QHBoxLayout()
    for label, callback in actions:
        button = QPushButton(label)
        button.clicked.connect(callback)
        row.addWidget(button)
    layout.addLayout(row)


class TrashManagerDialog(QDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle('Trash manager')
        self.resize(850, 420)
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        layout.addWidget(self.summary)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['Original path', 'Trash path', 'Logical size', 'Allocated'])
        self.tree.setSelectionMode(self.tree.SelectionMode.ExtendedSelection)
        layout.addWidget(self.tree)
        action_row(layout, [('Restore selected', self.restore_selected),
                            ('Delete permanently…', self.delete_selected), ('Close', self.accept)])
        self.populate()

    def populate(self):
        self.tree.clear()
        total = 0
        allocated_total = 0
        count = 0
        for batch in self.store.load():
            for entry in batch:
                try:
                    stat = os.lstat(entry.trashed)
                    size = stat.st_size
                    allocated = getattr(stat, 'st_blocks', (size + 511) // 512) * 512
                    total += size
                    allocated_total += allocated
                    size_text = format_bytes(size)
                    allocated_text = format_bytes(allocated)
                except OSError:
                    size_text = 'missing'
                    allocated_text = 'missing'
                item = QTreeWidgetItem([entry.original, entry.trashed, size_text, allocated_text])
                item.setData(0, Qt.ItemDataRole.UserRole, entry)
                self.tree.addTopLevelItem(item)
                count += 1
        self.summary.setText(f'{count} indexed files; {format_bytes(allocated_total)} allocated ({format_bytes(total)} logical). Allocation is estimated where filesystem block counts are unavailable.')
        self.tree.resizeColumnToContents(0)

    def selected(self):
        return [i.data(0, Qt.ItemDataRole.UserRole) for i in self.tree.selectedItems()]

    def _apply(self, callback):
        errors = []
        for entry in self.selected():
            try:
                callback(entry)
            except OSError as e:
                errors.append(f'{entry.original}: {e}')
        self.populate()
        if errors:
            QMessageBox.warning(self, 'Some files could not be processed', '\n'.join(errors))

    def restore_selected(self):
        self._apply(self.store.restore)

    def delete_selected(self):
        if not self.selected():
            return
        if QMessageBox.warning(self, 'Delete permanently?',
                f'Permanently delete {len(self.selected())} selected trash file(s)? This cannot be undone.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self._apply(self.store.delete)


class RecoveryDialog(QDialog):
    def __init__(self, journal, parent=None):
        super().__init__(parent)
        self.journal = journal
        self.retry = None
        self.setWindowTitle('Operation recovery')
        self.resize(850, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Inspect partial files, restore a saved original, or retry with confirmation.'))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['State', 'Target', 'Details'])
        layout.addWidget(self.tree)
        action_row(layout, [('Inspect files', self.inspect), ('Restore original', self.restore),
            ('Remove partial…', self.remove_partial), ('Retry…', self.retry_selected),
            ('Dismiss', self.dismiss), ('Close', self.accept)])
        self.populate()

    def populate(self):
        self.tree.clear()
        for op in self.journal.unresolved():
            item = QTreeWidgetItem([op.state, op.target, op.error])
            item.setData(0, Qt.ItemDataRole.UserRole, op)
            item.setToolTip(1, f'Backup: {op.backup}\nPartial: {op.partial}\nCommand: {shlex.join(op.args)}')
            self.tree.addTopLevelItem(item)
        self.tree.resizeColumnToContents(0)

    def selected(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def inspect(self):
        op = self.selected()
        if op:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(op.partial or op.backup or op.target)))

    def restore(self):
        op = self.selected()
        if op and op.backup and os.path.lexists(op.backup):
            try:
                self.journal.restore(op)
                self.populate()
            except OSError as e:
                QMessageBox.warning(self, 'Restore failed', str(e))

    def remove_partial(self):
        op = self.selected()
        if op and op.partial and QMessageBox.question(self, 'Remove partial file?',
                f'Permanently remove {op.partial}?') == QMessageBox.StandardButton.Yes:
            try:
                self.journal.remove_partial(op)
                self.populate()
            except OSError as e:
                QMessageBox.warning(self, 'Removal failed', str(e))

    def retry_selected(self):
        op = self.selected()
        if not op or not op.args:
            return
        if op.backup and os.path.lexists(op.backup):
            QMessageBox.warning(self, 'Restore first', 'Restore the original backup before retrying.')
            return
        typed, ok = QInputDialog.getText(self, 'Retry operation',
            'This repeats the operation and may replace its target. Type RUN to confirm.\n' + shlex.join(op.args))
        if ok and typed == 'RUN':
            self.retry = op
            self.accept()

    def dismiss(self):
        op = self.selected()
        if op:
            try:
                self.journal.forget(op)
                self.populate()
            except (OSError, ValueError) as e:
                QMessageBox.warning(self, 'Files remain', str(e))


class BatchResultsDialog(QDialog):
    def __init__(self, report, parent=None):
        super().__init__(parent)
        self.report = report
        self.retry = []
        self.setWindowTitle('Batch results')
        self.resize(850, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(report.summary()))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['Result', 'Exit', 'Command'])
        self.tree.setSelectionMode(self.tree.SelectionMode.ExtendedSelection)
        layout.addWidget(self.tree)
        for result in report.items:
            item = QTreeWidgetItem([result.state, str(result.code if result.code is not None else ''), shlex.join(result.args)])
            item.setData(0, Qt.ItemDataRole.UserRole, result)
            self.tree.addTopLevelItem(item)
        action_row(layout, [('Retry selected failures…', self.retry_failed), ('Close', self.accept)])

    def retry_failed(self):
        selected = [i.data(0, Qt.ItemDataRole.UserRole) for i in self.tree.selectedItems()]
        failed = [i for i in selected if i.state == 'failed']
        if not failed:
            return
        if any(i.entry[1] is not None or rerun_blocker(shlex.join(['VBoxManage', *i.args])) for i in failed):
            QMessageBox.warning(self, 'Cannot retry', 'Commands needing stdin or expired password files must use their original dialog.')
            return
        typed, ok = QInputDialog.getText(self, 'Retry failed commands',
            f'Retry {len(failed)} failed command(s)? Type RUN to confirm.')
        if ok and typed == 'RUN':
            self.retry = [i.entry for i in failed]
            self.accept()


class SnapshotComparisonDialog(QDialog):
    def __init__(self, changes, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Registry snapshot comparison')
        self.resize(950, 450)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f'{len(changes)} media changed. Comparison does not change the registry.'))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['Change', 'Kind', 'UUID', 'Details'])
        for c in changes:
            self.tree.addTopLevelItem(QTreeWidgetItem([c.change, c.kind, c.uuid, c.details]))
        layout.addWidget(self.tree)
        action_row(layout, [('Close', self.accept)])


class VMStorageDialog(QDialog):
    def __init__(self, vms, infos, media, parent=None):
        super().__init__(parent)
        from .library_views import storage_slots
        self.media = media
        self.requested = None
        self.setWindowTitle('VM storage')
        self.resize(850, 500)
        layout = QVBoxLayout(self)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['VM / controller / slot', 'Medium'])
        for name, vm in vms:
            root = QTreeWidgetItem([name, '' if vm in infos else 'Could not read storage'])
            self.tree.addTopLevelItem(root)
            controllers = {}
            for slot in storage_slots(vm, name, infos.get(vm, {})):
                if slot.controller not in controllers:
                    controllers[slot.controller] = QTreeWidgetItem(root, [slot.controller])
                item = QTreeWidgetItem(controllers[slot.controller],
                    [f'Port {slot.port}, device {slot.device}', slot.medium or 'Empty'])
                item.setData(0, Qt.ItemDataRole.UserRole, slot)
        self.tree.expandAll()
        self.tree.resizeColumnToContents(0)
        layout.addWidget(self.tree)
        action_row(layout, [('Attach image…', self.attach), ('Detach…', self.detach), ('Close', self.accept)])

    def slot(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def attach(self):
        slot = self.slot()
        if not slot:
            return
        if slot.medium:
            QMessageBox.information(self, 'Occupied slot', 'Detach the current medium before attaching another.')
            return
        choices = [(f'{kind}: {os.path.basename(r.location) or r.uuid} [{r.uuid}]', kind, r)
                   for kind, rows in self.media.items() for r in rows]
        if not choices:
            QMessageBox.information(self, 'No media', 'Add or create an image first.')
            return
        label, ok = QInputDialog.getItem(self, 'Attach image', 'Medium:', [c[0] for c in choices], 0, False)
        if ok:
            _, kind, rec = next(c for c in choices if c[0] == label)
            self.requested = ('attach', slot, kind, rec.uuid)
            self.accept()

    def detach(self):
        slot = self.slot()
        if slot and slot.medium and QMessageBox.question(self, 'Detach medium?',
                f'Detach {slot.medium} from {slot.vm_name}, {slot.controller}, port {slot.port}, device {slot.device}?') == QMessageBox.StandardButton.Yes:
            self.requested = ('detach', slot, '', '')
            self.accept()
