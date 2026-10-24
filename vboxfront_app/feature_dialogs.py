"""Dialogs for recovery, persistent trash, batch results and registry comparison."""
from __future__ import annotations
import os
import shlex
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QTreeWidget,
    QTreeWidgetItem,
    QLabel,
    QProgressBar,
    QPushButton,
    QMessageBox,
    QInputDialog,
)
from .domain import format_bytes, format_mbytes, rerun_blocker, is_destructive_command
from .reports import space_report_text
from .verify import VerifyTask, verdict
from .theme import color_css, error_color


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


class SpaceReportDialog(QDialog):
    """Where the disk space goes, from the listing already on screen."""

    def __init__(self, sections, parent=None):
        super().__init__(parent)
        self.sections = sections
        self.compact_uuids: list[str] = []
        self.setWindowTitle('Disk space report')
        self.resize(820, 520)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Sizes are allocated space on disk as VirtualBox reports it. '
                                'Nothing was run to produce this report.'))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['Item', 'On disk', 'Images', 'Note'])
        self.tree.setSelectionMode(self.tree.SelectionMode.ExtendedSelection)
        for section in sections:
            top = QTreeWidgetItem([section.title, format_mbytes(section.total_mb), '', section.note])
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)
            for row in section.rows:
                child = QTreeWidgetItem([row.label, format_mbytes(row.mb),
                                         str(row.count) if row.count else '', row.note])
                child.setData(0, Qt.ItemDataRole.UserRole, (section.title, row.uuids))
                child.setToolTip(0, row.label)
                top.addChild(child)
            self.tree.addTopLevelItem(top)
            top.setExpanded(section.title != 'By folder' or len(section.rows) <= 8)
        self.tree.resizeColumnToContents(0)
        self.tree.itemSelectionChanged.connect(self._sync)
        layout.addWidget(self.tree)
        row = QHBoxLayout()
        copy = QPushButton('Copy as text')
        copy.clicked.connect(self.copy_text)
        self.compact_btn = QPushButton('Compact selected candidates…')
        self.compact_btn.clicked.connect(self.compact_selected)
        close = QPushButton('Close')
        close.clicked.connect(self.reject)
        for button in (copy, self.compact_btn, close):
            row.addWidget(button)
        layout.addLayout(row)
        self._sync()

    def _selected_candidates(self) -> list[str]:
        uuids = []
        for item in self.tree.selectedItems():
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if data and data[0] == 'Compact candidates':
                uuids += data[1]
        return uuids

    def _sync(self):
        self.compact_btn.setEnabled(bool(self._selected_candidates()))

    def copy_text(self):
        QApplication.clipboard().setText(space_report_text(self.sections))

    def compact_selected(self):
        self.compact_uuids = self._selected_candidates()
        if self.compact_uuids:
            self.accept()


class VerifyDialog(QDialog):
    """Hash two images' guest-visible contents and say whether they match."""

    def __init__(self, vbm: str, sources, parent=None, autostart: bool = True):
        super().__init__(parent)
        self.setWindowTitle('Compare contents')
        self.resize(720, 260)
        self.sources = sources
        self.results = None
        self.identical = None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Reads each image end to end as the guest sees it and '
                                'compares SHA-256 hashes. Large disks take a while.'))
        self.bars = []
        for source in sources:
            label = QLabel(f'<b>{source.label}</b><br><tt>{source.describe()}</tt>')
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            layout.addWidget(label)
            layout.addWidget(bar)
            self.bars.append(bar)
        self.verdict = QLabel('')
        self.verdict.setWordWrap(True)
        self.verdict.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.verdict)
        row = QHBoxLayout()
        self.cancel_btn = QPushButton('Cancel')
        self.cancel_btn.clicked.connect(self.cancel)
        self.close_btn = QPushButton('Close')
        self.close_btn.clicked.connect(self.accept)
        self.close_btn.setEnabled(False)
        row.addStretch(1)
        row.addWidget(self.cancel_btn)
        row.addWidget(self.close_btn)
        layout.addLayout(row)
        self.task = VerifyTask(vbm, sources, self)
        self.task.progress.connect(self._progress)
        self.task.done.connect(self._done)
        if autostart:
            self.task.start()

    def _progress(self, index: int, pct: int):
        bar = self.bars[index]
        if pct < 0:
            bar.setRange(0, 0)
        else:
            bar.setRange(0, 100)
            bar.setValue(pct)

    def _done(self, results):
        self.results = results
        self.identical, text = verdict(results)
        for bar, result in zip(self.bars, results):
            bar.setRange(0, 100)
            bar.setValue(100 if not result.error else 0)
        self.verdict.setText(text)
        self.verdict.setStyleSheet('' if self.identical else color_css(error_color()))
        self.cancel_btn.setEnabled(False)
        self.close_btn.setEnabled(True)

    def cancel(self):
        self.task.cancel()

    def done(self, result):
        if self.task.isRunning():
            self.task.cancel()
            self.task.wait()
        super().done(result)


class GoldenImageDialog(QDialog):
    """Pick the VMs that should each boot from their own child of one base."""

    def __init__(self, medium_name: str, needs_type: bool, rows, parent=None):
        """rows: (vm name, vm uuid, (controller, port, device) | None, running)."""
        super().__init__(parent)
        self.setWindowTitle('Golden image')
        self.resize(720, 420)
        self.chosen = []
        layout = QVBoxLayout(self)
        steps = ('<b>{0}</b> becomes <i>multiattach</i>, then is attached to each VM '
                 'ticked below.' if needs_type else '<b>{0}</b> is already multiattach; it is '
                 'attached to each VM ticked below.').format(medium_name)
        text = QLabel(steps + ' VirtualBox gives every VM its own differencing image on '
                      'top of it, so the VMs share the base and never write to it.')
        text.setWordWrap(True)
        layout.addWidget(text)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['VM', 'Attach at', 'Note'])
        for name, uuid, slot, running in rows:
            where = f'{slot[0]} port {slot[1]} device {slot[2]}' if slot else ''
            note = ('running — power it off first' if running
                    else '' if slot else 'no free slot on a disk controller')
            item = QTreeWidgetItem([name, where, note])
            item.setData(0, Qt.ItemDataRole.UserRole, (uuid, name, slot))
            if running or not slot:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                item.setCheckState(0, Qt.CheckState.Unchecked)
            else:
                item.setCheckState(0, Qt.CheckState.Unchecked)
            self.tree.addTopLevelItem(item)
        self.tree.resizeColumnToContents(0)
        layout.addWidget(self.tree)
        action_row(layout, [('Attach', self._ok), ('Cancel', self.reject)])

    def _ok(self):
        self.chosen = [
            self.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
            for i in range(self.tree.topLevelItemCount())
            if self.tree.topLevelItem(i).checkState(0) == Qt.CheckState.Checked
        ]
        if not self.chosen:
            QMessageBox.information(self, 'Golden image', 'Tick at least one VM.')
            return
        self.accept()
