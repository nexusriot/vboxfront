"""Service contracts and UI integration for the modular feature architecture."""
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMessageBox
from vboxfront_app.recovery import OperationJournal
from vboxfront_app.batches import BatchReport
from vboxfront_app.trash import TrashStore
from vboxfront_app.models import TrashedFile
from vboxfront_app.persistence import get_settings, move_to_trash, load_library
from vboxfront_app.library_views import SavedFilterStore, storage_slots, StorageSlot
from vboxfront_app.snapshots import compare_registry, load_snapshot
from vboxfront_app.measurement import ArchiveMeasureTask, MeasurementCache, source_stamp
from vboxfront_app.disk_dialogs import ImportCompressedDialog
from vboxfront_app.feature_dialogs import TrashManagerDialog, RecoveryDialog, BatchResultsDialog, VMStorageDialog
from tests.test_window import FakeVBoxManageTest, wait_until


class ArchitectureTest(unittest.TestCase):
    def test_domain_layer_imports_without_qt(self):
        result = subprocess.run([sys.executable, '-c',
            "import sys; import vboxfront_app.domain; assert 'PyQt6' not in sys.modules"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_entrypoint_reexports_the_real_classes(self):
        import vboxfront
        from vboxfront_app.window import MainWindow
        from vboxfront_app.processes import CommandRunner
        self.assertIs(vboxfront.MainWindow, MainWindow)
        self.assertIs(vboxfront.CommandRunner, CommandRunner)


class RecoveryServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = str(self.root / 'operations.json')
        self.journal = OperationJournal(self.path)
        self.addCleanup(self.journal.close)
        self.target = self.root / 'output.vdi'

    def begin(self, overwrite=True):
        return self.journal.begin(['clonemedium', 'disk', 'source', str(self.target)],
                                  str(self.target), overwrite=overwrite)

    def test_success_replaces_original_and_removes_backup(self):
        self.target.write_bytes(b'original')
        op = self.begin()
        self.assertFalse(self.target.exists())
        self.assertEqual(Path(op.backup).read_bytes(), b'original')
        self.target.write_bytes(b'new')
        self.assertEqual(self.journal.finish(op, 0), 0)
        self.assertEqual(self.target.read_bytes(), b'new')
        self.assertFalse(Path(op.backup).exists())
        self.assertFalse(OperationJournal(self.path).unresolved())

    def test_failure_restores_original_and_preserves_partial(self):
        self.target.write_bytes(b'original')
        op = self.begin()
        self.target.write_bytes(b'partial')
        self.assertEqual(self.journal.finish(op, 1), 1)
        self.assertEqual(self.target.read_bytes(), b'original')
        self.assertEqual(Path(op.partial).read_bytes(), b'partial')
        loaded = OperationJournal(self.path).unresolved()[0]
        self.assertEqual(loaded.partial, op.partial)

    def test_interrupted_replacement_can_be_restored_after_restart(self):
        self.target.write_bytes(b'original')
        op = self.begin()
        self.target.write_bytes(b'partial')
        self.journal.close()  # Simulate the old process releasing its file locks.
        restarted = OperationJournal(self.path)
        loaded = restarted.unresolved()[0]
        self.assertEqual(loaded.state, 'interrupted')
        restarted.restore(loaded)
        self.assertEqual(self.target.read_bytes(), b'original')
        self.assertEqual(Path(loaded.partial).read_bytes(), b'partial')

    def test_existing_target_requires_explicit_overwrite(self):
        self.target.write_bytes(b'original')
        with self.assertRaises(FileExistsError):
            self.begin(overwrite=False)
        self.assertEqual(self.target.read_bytes(), b'original')

    def test_success_without_output_is_failure_and_restores_backup(self):
        self.target.write_bytes(b'original')
        op = self.begin()
        self.assertEqual(self.journal.finish(op, 0), 1)
        self.assertEqual(self.target.read_bytes(), b'original')

    def test_changed_partial_cannot_delete_a_new_good_output(self):
        op = self.begin(False)
        self.target.write_bytes(b'partial')
        self.journal.finish(op, 1)
        self.target.write_bytes(b'new successful output')
        with self.assertRaises(OSError):
            self.journal.remove_partial(op)
        self.assertEqual(self.target.read_bytes(), b'new successful output')

    def test_partial_removal_and_dismiss_are_persisted(self):
        op = self.begin(False)
        self.target.write_bytes(b'partial')
        self.journal.finish(op, 1)
        with self.assertRaises(ValueError):
            self.journal.forget(op)
        self.journal.remove_partial(op)
        self.journal.forget(op)
        self.assertFalse(OperationJournal(self.path).operations)

    def test_separate_instances_do_not_lose_each_others_records(self):
        other = OperationJournal(self.path)
        a = self.begin(False)
        self.addCleanup(other.close)
        b = other.begin(['convertfromraw', '/raw', str(self.root / 'other.vdi')], str(self.root / 'other.vdi'))
        self.assertEqual({o.id for o in OperationJournal(self.path).operations}, {a.id, b.id})

    def test_interrupted_new_output_is_available_for_partial_cleanup(self):
        self.begin(False)
        self.target.write_bytes(b'partial')
        self.journal.close()  # Simulate the old process releasing its file locks.
        restarted = OperationJournal(self.path)
        op = restarted.unresolved()[0]
        self.assertEqual(op.partial, str(self.target))
        restarted.remove_partial(op)
        self.assertFalse(self.target.exists())


    def test_another_instance_cannot_replace_or_restore_an_active_target(self):
        self.target.write_bytes(b'original')
        op = self.begin()
        other = OperationJournal(self.path)
        self.addCleanup(other.close)
        self.assertEqual(other.operations[0].state, 'running')
        with self.assertRaises(OSError):
            other.begin(op.args, str(self.target), overwrite=True)
        with self.assertRaises(OSError):
            other.restore(other.operations[0])
        self.assertEqual(Path(op.backup).read_bytes(), b'original')
        self.assertEqual(self.journal.finish(op, 1), 1)

    def test_successful_retry_resolves_previous_failure_without_leftover_files(self):
        op = self.begin(False)
        self.journal.finish(op, 1)
        retry = self.begin(False)
        self.target.write_bytes(b'valid output')
        self.journal.finish(retry, 0)
        self.assertFalse(self.journal.unresolved())


class SnapshotServiceTest(unittest.TestCase):
    def test_added_missing_moved_and_other_changes(self):
        before = {'disk': [{'uuid': 'a', 'location': '/old'}, {'uuid': 'gone'}, {'uuid': 'c', 'type': 'normal'}]}
        after = {'disk': [{'uuid': 'a', 'location': '/new'}, {'uuid': 'new'}, {'uuid': 'c', 'type': 'immutable'}]}
        changes = {c.uuid: c for c in compare_registry(before, after)}
        self.assertEqual({u: c.change for u,c in changes.items()},
                         {'a': 'moved', 'gone': 'missing', 'new': 'added', 'c': 'changed'})
        self.assertIn('/old → /new', changes['a'].details)

    def test_malformed_snapshots_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'snapshot.json'
            for data in [[], {}, {'media': []}, {'media': {'bad': []}},
                         {'media': {'disk': [{}]}},
                         {'media': {'disk': [{'uuid':'a'}, {'uuid':'a'}]}}]:
                with self.subTest(data=data):
                    path.write_text(json.dumps(data))
                    with self.assertRaises(ValueError):
                        load_snapshot(path)


class FeatureIntegrationTest(FakeVBoxManageTest):
    def test_saved_filters_persist_and_apply_to_the_existing_filter(self):
        w = self.make_window()
        store = SavedFilterStore()
        get_settings().remove(store.KEY)
        store.save('VDIs', 'fmt:vdi')
        self.assertEqual(SavedFilterStore().load(), {'VDIs': 'fmt:vdi'})
        w.reload_saved_filters()
        w.apply_saved_filter(1)
        self.assertEqual(w.filter_edit.text(), 'fmt:vdi')
        self.assertTrue(all(item.isHidden() for item in w.panes['disk']._iter_items()
                            if item.text(1).lower() == 'vmdk'))
        store.delete('VDIs')
        self.assertEqual(store.load(), {})

    def test_persistent_trash_restores_after_loading_a_new_store(self):
        original = Path(self.workdir) / 'gone.vdi'
        original.write_bytes(b'disk data')
        trashed = move_to_trash(str(original))
        store = TrashStore()
        entry = TrashedFile('disk', str(original), trashed)
        store.save([[entry]])
        restarted = TrashStore()
        self.assertEqual(restarted.load(), [[entry]])
        restarted.restore(entry)
        self.assertEqual(original.read_bytes(), b'disk data')
        self.assertIn(str(original), load_library('disk'))
        self.assertEqual(store.load(), [])

    def test_trash_manager_permanent_delete_requires_confirmation(self):
        self.make_window()
        original = Path(self.workdir) / 'gone.vdi'
        original.write_bytes(b'disk data')
        entry = TrashedFile('disk', str(original), move_to_trash(str(original)))
        store = TrashStore()
        store.save([[entry]])
        dlg = TrashManagerDialog(store)
        dlg.tree.topLevelItem(0).setSelected(True)
        with mock.patch.object(QMessageBox, 'warning', return_value=QMessageBox.StandardButton.No):
            dlg.delete_selected()
        self.assertTrue(os.path.exists(entry.trashed))
        with mock.patch.object(QMessageBox, 'warning', return_value=QMessageBox.StandardButton.Yes):
            dlg.delete_selected()
        self.assertFalse(os.path.exists(entry.trashed))
        self.assertEqual(store.load(), [])

    def test_trash_restore_does_not_overwrite_a_recreated_file(self):
        self.make_window()
        original = Path(self.workdir) / 'gone.vdi'
        original.write_bytes(b'old')
        entry = TrashedFile('disk', str(original), move_to_trash(str(original)))
        store = TrashStore()
        store.save([[entry]])
        original.write_bytes(b'new')
        with self.assertRaises(FileExistsError):
            store.restore(entry)
        self.assertEqual(original.read_bytes(), b'new')
        self.assertTrue(os.path.exists(entry.trashed))

    def test_batch_results_include_success_failed_and_skipped(self):
        entries = [(['first'], None, None), (['second'], None, None), (['third'], None, None)]
        report = BatchReport(entries)
        first = report.start(); first.state, first.code = 'succeeded', 0
        second = report.start(); second.state, second.code = 'failed', 1
        report.skip_waiting()
        self.assertEqual(report.summary(), '1 succeeded, 1 failed, 1 skipped')
        self.assertEqual(report.items[1].entry, entries[1])

    def test_results_dialog_retries_only_confirmed_selected_failures(self):
        self.make_window()
        report = BatchReport([(['first'], None, None), (['second'], None, None)])
        report.items[0].state = 'failed'
        report.items[1].state = 'succeeded'
        dlg = BatchResultsDialog(report)
        dlg.tree.topLevelItem(0).setSelected(True)
        dlg.tree.topLevelItem(1).setSelected(True)
        with mock.patch('vboxfront_app.feature_dialogs.QInputDialog.getText', return_value=('RUN', True)):
            dlg.retry_failed()
        self.assertEqual(dlg.retry, [report.items[0].entry])

    def test_results_dialog_refuses_replaying_stdin(self):
        self.make_window()
        report = BatchReport([(['encryptmedium', 'u'], b'password', None)])
        report.items[0].state = 'failed'
        dlg = BatchResultsDialog(report)
        dlg.tree.topLevelItem(0).setSelected(True)
        with mock.patch.object(QMessageBox, 'warning') as warning:
            dlg.retry_failed()
        self.assertFalse(dlg.retry)
        warning.assert_called_once()

    def test_vm_storage_slot_model_uses_controller_geometry(self):
        info = {'storagecontrollername0': 'IDE', 'storagecontrollertype0': 'PIIX4',
                'storagecontrollerportcount0': '2', 'IDE-0-0': '/disk.vdi'}
        slots = storage_slots('vm', 'alpha', info)
        self.assertEqual(len(slots), 4)
        self.assertEqual(slots[0].medium, '/disk.vdi')
        self.assertEqual((slots[3].port, slots[3].device), (1,1))

    def test_vm_storage_refuses_a_slot_changed_since_the_view(self):
        w = self.make_window()
        slot = StorageSlot('vm', 'alpha', 'SATA', 0, 0, '')
        changed = 'storagecontrollername0="SATA"\nstoragecontrollerportcount0="1"\n"SATA-0-0"="/other.vdi"'
        with mock.patch.object(w, '_capture', side_effect=lambda args, done: done(0, changed)), \
             mock.patch.object(w, '_run_queue') as queue, \
             mock.patch.object(QMessageBox, 'warning') as warning:
            w._apply_storage_request(('attach', slot, 'disk', 'uuid'))
        queue.assert_not_called()
        warning.assert_called_once()

    def test_background_measurement_populates_size_and_cache(self):
        self.make_window()
        archive = Path(self.workdir) / 'disk.img.gz'
        with gzip.open(archive, 'wb') as handle:
            handle.write(b'x' * (3 << 20))
        dlg = ImportCompressedDialog(str(archive))
        dlg._measure()
        self.assertIsNotNone(dlg._measure_worker)
        self.assertTrue(wait_until(lambda: dlg._measure_worker is None))
        self.assertEqual(dlg.size_edit.text(), str(3 << 20))
        cached = ImportCompressedDialog(str(archive))
        self.assertEqual(cached.size_edit.text(), str(3 << 20))

    def test_measurement_cache_invalidates_changed_sources(self):
        path = Path(self.workdir) / 'archive.gz'
        path.write_bytes(b'first')
        cache = MeasurementCache()
        cache.put(source_stamp(path), 123)
        self.assertEqual(cache.get(path), 123)
        path.write_bytes(b'other contents')
        self.assertIsNone(cache.get(path))

    def test_cancelled_measurement_does_not_publish_a_size(self):
        self.make_window()
        archive = Path(self.workdir) / 'disk.img.gz'
        with gzip.open(archive, 'wb') as handle:
            handle.write(b'x' * (3 << 20))
        dlg = ImportCompressedDialog(str(archive))
        with mock.patch('vboxfront_app.measurement.measure_uncompressed',
                        side_effect=lambda path, report: 123 if report(123) else None):
            dlg._measure()
            dlg._stop_measurement()
            self.assertTrue(wait_until(lambda: dlg._measure_worker is None))
        self.assertEqual(dlg.size_edit.text(), '')

    def test_prelaunch_failure_restores_a_preserved_target(self):
        w = self.make_window()
        target = Path(self.workdir) / 'output.vdi'
        target.write_bytes(b'original')
        self.assertTrue(w._clear_target(str(target)))
        with mock.patch('vboxfront_app.processes.vboxmanage_path', return_value=None):
            w.runner.run(['clonemedium', 'disk', 'source', str(target)])
        self.assertEqual(target.read_bytes(), b'original')
        self.assertIsNone(w._active_operation)
