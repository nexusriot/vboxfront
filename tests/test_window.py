"""MainWindow integration tests against a fake VBoxManage executable."""

import csv
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

import vboxfront

from PyQt6.QtCore import QByteArray
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication, QMessageBox

from vboxfront import (
    COLUMNS,
    COL_SNAPSHOT,
    OrphanScanDialog,
    RemoveDialog,
    add_library_path,
    COL_CAPACITY,
    COL_NAME,
    COL_STATE,
    ContentsDialog,
    CreateDiskDialog,
    CreateFloppyDialog,
    PropertiesDialog,
    COL_IN_USE,
    KINDS,
    MainWindow,
    append_history,
    change_password_args,
    clear_history,
    encrypt_args,
    get_settings,
    load_history,
    load_library,
    save_library,
    write_secret_file,
)

HDDS_OUTPUT = """\
UUID:           aaaaaaaa-1111-1111-1111-111111111111
Parent UUID:    base
State:          created
Type:           normal (base)
Location:       /fake/base.vdi
Storage format: VDI
Capacity:       10240 MBytes
Size on disk:   500 MBytes
In use by VMs:  vbf-smoke (UUID: 24aa0bbd-3d9c-4ba3-a41f-273c0ab57661)

UUID:           bbbbbbbb-2222-2222-2222-222222222222
Parent UUID:    aaaaaaaa-1111-1111-1111-111111111111
State:          created
Type:           normal (differencing)
Location:       /fake/child.vdi
Storage format: VDI
Capacity:       10240 MBytes
Size on disk:   12 MBytes

UUID:           cccccccc-3333-3333-3333-333333333333
Parent UUID:    base
State:          created
Type:           normal (base)
Location:       /fake/other.vmdk
Storage format: VMDK
Capacity:       2048 MBytes
Size on disk:   2048 MBytes

UUID:           dddddddd-4444-4444-4444-444444444444
Parent UUID:    base
State:          inaccessible
Access Error:   Could not open the medium '/fake/gone.vdi'.
VD: error VERR_FILE_NOT_FOUND opening image file '/fake/gone.vdi' (VERR_FILE_NOT_FOUND)
Type:           normal (base)
Location:       /fake/gone.vdi
Storage format: VDI
Capacity:       512 MBytes
Size on disk:   0 MBytes
"""


BACKENDS = """\
Supported hard disk backends:

Backend 0: id='VMDK' description='VMDK' capabilities=0x0a7f extensions='vmdk (HardDisk)' properties=()
Backend 1: id='VDI' description='VDI' capabilities=0x0e77 extensions='vdi (HardDisk)' properties=()
Backend 10: id='RAW' description='RAW' capabilities=0x0262 extensions='img (Floppy)' properties=()
"""


def wait_until(cond, timeout=10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


class FakeVBoxManageTest(unittest.TestCase):
    """Fixture only: a fake VBoxManage on PATH and a window driven against it.

    Split out from the tests that use it so a second suite can inherit the
    fixture without inheriting — and re-running — every test in it.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv[:1])

    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="vboxfront-fake-")
        self.log = os.path.join(self.workdir, "calls.log")
        self.fake = os.path.join(self.workdir, "VBoxManage")
        hdds = HDDS_OUTPUT
        script = (
            "#!/bin/sh\n"
            f'echo "$*" >> "{self.log}"\n'
            'case "$*" in\n'
            f'  "list -l hdds") cat <<\'EOF\'\n{hdds}EOF\n  ;;\n'
            f'  "list hddbackends") cat <<\'EOF\'\n{BACKENDS}EOF\n  ;;\n'
            '  modifymedium*) echo "0%...50%...100%" ;;\n'
            # Echo the secret back into the log so the stdin plumbing is
            # observable: VBoxManage reads it from standard input, not argv.
            f'  encryptmedium*) read pw; echo "stdin:$pw" >> "{self.log}" ;;\n'
            # Not a VBoxManage subcommand: a handle for tests that need a
            # command which is still running when they look at it.
            '  hang) sleep 30 ;;\n'
            "esac\n"
            "exit 0\n"
        )
        with open(self.fake, "w") as f:
            f.write(script)
        os.chmod(self.fake, os.stat(self.fake).st_mode | stat.S_IXUSR)
        get_settings().setValue("vboxmanage_path", self.fake)
        for kind in KINDS:
            save_library(kind, [])
        self.window = None

    def tearDown(self):
        if self.window:
            # Close asks before killing a running command, and an unmocked
            # QMessageBox here would block the whole suite — so leave nothing
            # running, whatever the test body did or failed to do.
            if self.window.runner.is_busy():
                self.window.runner.cancel()
                wait_until(lambda: not self.window.runner.is_busy())
            self.window.close()
            self.window.deleteLater()
        get_settings().setValue("vboxmanage_path", "")
        QApplication.processEvents()

    def make_window(self) -> MainWindow:
        self.window = MainWindow()
        self.assertTrue(
            wait_until(lambda: not self.window._refreshing
                       and self.window.panes["disk"].tree.topLevelItemCount() > 0),
            "initial refresh did not populate the disk pane",
        )
        return self.window

    def top_level_names(self, pane) -> list[str]:
        return [
            pane.tree.topLevelItem(i).text(0)
            for i in range(pane.tree.topLevelItemCount())
        ]


class MainWindowTest(FakeVBoxManageTest):
    def test_populates_tree_with_parent_chains(self):
        w = self.make_window()
        pane = w.panes["disk"]
        names = self.top_level_names(pane)
        self.assertEqual(sorted(names), ["base.vdi", "gone.vdi", "other.vmdk"])
        base = next(
            pane.tree.topLevelItem(i)
            for i in range(pane.tree.topLevelItemCount())
            if pane.tree.topLevelItem(i).text(0) == "base.vdi"
        )
        self.assertEqual(base.childCount(), 1)
        self.assertEqual(base.child(0).text(0), "child.vdi")
        self.assertEqual(base.text(COL_CAPACITY), "10.0 GB")
        self.assertEqual(base.text(COL_IN_USE), "vbf-smoke")

    def test_inaccessible_media_are_highlighted(self):
        # The listing carries an "Access Error:" block for this row; it must not
        # bleed into State, or the highlight silently stops firing.
        w = self.make_window()
        pane = w.panes["disk"]
        gone = next(i for i in pane._iter_items() if i.text(0) == "gone.vdi")
        self.assertEqual(gone.foreground(0).color(), QColor(220, 80, 80))
        self.assertEqual(gone.text(COL_STATE), "inaccessible")
        self.assertIn("VERR_FILE_NOT_FOUND", gone.toolTip(COL_STATE))

    def test_remove_refuses_a_medium_with_children(self):
        w = self.make_window()
        pane = w.panes["disk"]
        pane.select_uuid("aaaaaaaa-1111-1111-1111-111111111111")  # parent of child.vdi
        # base.vdi is attached to a VM too, and that guard fires first; clear it
        # so this test exercises the child guard rather than the in-use one.
        pane.selected_record().in_use = []
        with mock.patch.object(QMessageBox, "warning") as warn:
            w.remove_selected()
        warn.assert_called_once()
        self.assertIn("child.vdi", warn.call_args[0][2])
        with open(self.log) as f:
            self.assertNotIn("closemedium", f.read())

    def test_a_long_command_stays_inside_the_header(self):
        w = self.make_window()
        w.resize(700, 500)
        QApplication.processEvents()
        runner = w.runner
        runner.set_status("Running: " + "/a/very/long/path" * 30)
        QApplication.processEvents()
        self.assertLessEqual(
            runner.cancel_btn.x() + runner.cancel_btn.width(), runner.width(),
            "a long command pushed the buttons out of the panel",
        )
        self.assertLess(len(runner.cmd_label.text()), len(runner.cmd_label.fullText()))
        self.assertIn("/a/very/long/path", runner.cmd_label.toolTip())

    def test_filter_hides_non_matching_rows(self):
        w = self.make_window()
        pane = w.panes["disk"]
        w.filter_edit.setText("vmdk")
        visible = [i.text(0) for i in pane._iter_items() if not i.isHidden()]
        self.assertEqual(visible, ["other.vmdk"])
        w.filter_edit.setText("")
        hidden = [i.text(0) for i in pane._iter_items() if i.isHidden()]
        self.assertEqual(hidden, [])

    def test_compact_all_vdis_queues_accessible_vdis_only(self):
        w = self.make_window()
        with mock.patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
        ):
            w.compact_all_vdis()
        self.assertTrue(
            wait_until(lambda: not w.runner.is_busy() and not w._cmd_queue
                       and not w._refreshing),
            "compact queue did not drain",
        )
        with open(self.log) as f:
            compacts = [l for l in f.read().splitlines() if "--compact" in l]
        # base + child are accessible VDIs; gone.vdi (inaccessible) and the
        # VMDK must be skipped.
        self.assertEqual(len(compacts), 2)
        self.assertIn("modifymedium disk aaaaaaaa-1111-1111-1111-111111111111 --compact", compacts)
        self.assertIn("modifymedium disk bbbbbbbb-2222-2222-2222-222222222222 --compact", compacts)

    def test_library_paths_are_reopened_on_refresh(self):
        save_library("disk", ["/fake/lib.vdi"])
        w = self.make_window()
        with open(self.log) as f:
            calls = f.read().splitlines()
        self.assertIn("showmediuminfo disk /fake/lib.vdi", calls)

    def test_encryption_password_reaches_the_process_on_stdin(self):
        w = self.make_window()
        args, stdin_data = encrypt_args(
            "aaaaaaaa-1111-1111-1111-111111111111", "s3cret", "pid",
            "AES-XTS256-PLAIN64",
        )
        w.runner.run(args, stdin_data)
        self.assertTrue(
            wait_until(lambda: not w.runner.is_busy() and not w._refreshing),
            "encryptmedium did not finish",
        )
        with open(self.log) as f:
            log = f.read()
        self.assertIn(
            "encryptmedium aaaaaaaa-1111-1111-1111-111111111111 "
            "--newpassword stdin --newpasswordid pid",
            log,
        )
        self.assertIn("stdin:s3cret", log)
        # And it stays out of the echoed command line the user sees.
        self.assertNotIn("s3cret", w.runner.output.toPlainText())

    def test_totals_footer_reports_the_listing(self):
        w = self.make_window()
        text = w.panes["disk"].totals.text()
        self.assertIn("4 media", text)
        self.assertIn("provisioned", text)
        w.filter_edit.setText("vmdk")
        self.assertIn("(1 shown)", w.panes["disk"].totals.text())

    def test_history_records_commands_with_exit_codes(self):
        clear_history()
        w = self.make_window()
        w.runner.run(["modifymedium", "disk", "aaaaaaaa-1111-1111-1111-111111111111",
                      "--compact"])
        self.assertTrue(
            wait_until(lambda: not w.runner.is_busy() and not w._refreshing),
            "compact did not finish",
        )
        entries = load_history()
        self.assertTrue(entries)
        last = entries[-1]
        self.assertIn("--compact", last["command"])
        self.assertEqual(last["exit"], 0)
        self.assertTrue(last["when"], "entries carry a timestamp")
        # The copy button offers exactly what was run.
        self.assertEqual(w.runner.last_command, last["command"])
        clear_history()

    def test_history_survives_a_command_line_containing_commas(self):
        # QSettings' INI backend splits string lists on commas; the JSON blob
        # this uses instead must round-trip one untouched.
        clear_history()
        append_history("VBoxManage modifymedium d --description 'a,b,c'", 1, "now")
        (entry,) = load_history()
        self.assertEqual(
            entry["command"], "VBoxManage modifymedium d --description 'a,b,c'"
        )
        self.assertEqual(entry["exit"], 1)
        clear_history()

    def test_secret_files_are_shredded_when_the_command_exits(self):
        w = self.make_window()
        secrets = [write_secret_file("old"), write_secret_file("new")]
        args = change_password_args(
            "aaaaaaaa-1111-1111-1111-111111111111", secrets[0], secrets[1],
            "id", "AES-XTS256-PLAIN64",
        )
        w.runner.run(args, None, secrets)
        self.assertTrue(
            wait_until(lambda: not w.runner.is_busy() and not w._refreshing),
            "encryptmedium did not finish",
        )
        for path in secrets:
            self.assertFalse(os.path.exists(path), f"{path} outlived the command")

    def test_secret_files_are_shredded_when_the_binary_cannot_start(self):
        w = self.make_window()
        get_settings().setValue("vboxmanage_path", os.path.join(self.workdir, "nope"))
        secrets = [write_secret_file("old")]
        try:
            w.runner.run(["encryptmedium", "u"], None, secrets)
            self.assertTrue(
                wait_until(lambda: not os.path.exists(secrets[0])),
                "secret survived a failed start",
            )
        finally:
            get_settings().setValue("vboxmanage_path", self.fake)
            for path in secrets:
                if os.path.exists(path):
                    os.remove(path)

    def test_secret_files_are_shredded_when_vboxmanage_is_missing(self):
        # An empty setting is not enough here: the fallback finds a real
        # VBoxManage on any machine that has one installed.
        w = self.make_window()
        secrets = [write_secret_file("old")]
        try:
            with mock.patch.object(vboxfront, "vboxmanage_path", return_value=None):
                w.runner.run(["encryptmedium", "u"], None, secrets)
            self.assertFalse(os.path.exists(secrets[0]))
        finally:
            for path in secrets:
                if os.path.exists(path):
                    os.remove(path)

    def test_backends_are_read_once_and_shape_the_dialogs(self):
        w = self.make_window()
        self.assertEqual([b.id for b in w.backends], ["VMDK", "VDI", "RAW"])
        with open(self.log) as f:
            calls = f.read().splitlines()
        self.assertEqual(calls.count("list hddbackends"), 1)
        # Cached: a second refresh must not ask again.
        w.refresh_media()
        self.assertTrue(wait_until(lambda: not w._refreshing))
        with open(self.log) as f:
            calls = f.read().splitlines()
        self.assertEqual(calls.count("list hddbackends"), 1)
        # RAW has no HardDisk extension, so it is not a creation target.
        dlg = CreateDiskDialog(w.backends)
        formats = [dlg.fmt_combo.itemText(i) for i in range(dlg.fmt_combo.count())]
        self.assertEqual(formats, ["VMDK", "VDI"])

    def test_contents_reads_through_the_runner(self):
        w = self.make_window()
        w.panes["disk"].select_uuid("aaaaaaaa-1111-1111-1111-111111111111")
        dlg = ContentsDialog("disk", w.panes["disk"].selected_record(), w)
        dlg.size_edit.setText("64")
        dlg._ok()
        args, stdin_data = dlg.command
        w.runner.run(args, stdin_data)
        self.assertTrue(
            wait_until(lambda: not w.runner.is_busy() and not w._refreshing),
            "mediumio did not finish",
        )
        with open(self.log) as f:
            log = f.read()
        self.assertIn(
            "mediumio --disk=aaaaaaaa-1111-1111-1111-111111111111 cat --hex --size=64",
            log,
        )

    def test_properties_queues_the_autoreset_call(self):
        w = self.make_window()
        w.panes["disk"].select_uuid("aaaaaaaa-1111-1111-1111-111111111111")
        m = w.panes["disk"].selected_record()
        dlg = PropertiesDialog(m, w)
        dlg.type_combo.setCurrentText("immutable")
        dlg.autoreset.setChecked(True)
        dlg._ok()
        commands = [c for c in (dlg.args("disk"), dlg.autoreset_command()) if c]
        self.assertEqual(len(commands), 2, "type and autoreset are separate calls")
        w._run_queue([(c, None, None) for c in commands])
        self.assertTrue(
            wait_until(lambda: not w.runner.is_busy() and not w._cmd_queue
                       and not w._refreshing),
            "properties queue did not drain",
        )
        with open(self.log) as f:
            log = f.read()
        self.assertIn("--type immutable", log)
        self.assertIn("--autoreset on", log)

    def test_capture_timeout_frees_a_stranded_refresh(self):
        # A wedged VBoxManage used to leave _refreshing set forever, silencing
        # every later refresh. Verified with a fake that never exits.
        hang = os.path.join(self.workdir, "hang")
        with open(hang, "w") as f:
            f.write("#!/bin/sh\nsleep 300\n")
        os.chmod(hang, os.stat(hang).st_mode | stat.S_IXUSR)
        get_settings().setValue("vboxmanage_path", hang)
        original = vboxfront.CaptureRunner.TIMEOUT_MS
        vboxfront.CaptureRunner.TIMEOUT_MS = 300
        try:
            w = MainWindow()
            self.window = w
            self.assertTrue(
                wait_until(lambda: not w._refreshing, timeout=20.0),
                "refresh stayed stranded after the deadline",
            )
            self.assertIn("did not answer", w.runner.output.toPlainText())
        finally:
            vboxfront.CaptureRunner.TIMEOUT_MS = original
            get_settings().setValue("vboxmanage_path", self.fake)

    def test_history_blames_the_command_that_actually_failed(self):
        # `finished` fires straight from run() when the binary is missing; a
        # stale last_command filed that failure against the previous command.
        w = self.make_window()
        clear_history()
        w.runner.run(["list", "hdds"])
        self.assertTrue(wait_until(lambda: not w.runner.is_busy() and not w._refreshing))
        with mock.patch.object(vboxfront, "vboxmanage_path", return_value=None):
            w.runner.run(["encryptmedium", "other-disk", "--newpassword", "stdin"])
        QApplication.processEvents()
        entries = load_history()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["exit"], 0)
        self.assertIn("list hdds", entries[0]["command"])
        self.assertEqual(entries[1]["exit"], 127)
        self.assertIn("encryptmedium other-disk", entries[1]["command"])
        clear_history()

    def test_quitting_mid_command_asks_first(self):
        w = self.make_window()
        w.runner.run(["hang"])
        self.assertTrue(wait_until(lambda: w.runner.is_busy()), "command never started")
        with mock.patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.No
        ) as ask:
            w.close()
        ask.assert_called_once()
        self.assertTrue(w.runner.is_busy(), "the command was killed despite the veto")

        w.runner.cancel()
        self.assertTrue(wait_until(lambda: not w.runner.is_busy() and not w._refreshing))
        with mock.patch.object(QMessageBox, "question") as ask:
            w.close()
        ask.assert_not_called()

    def test_quitting_mid_command_can_be_confirmed(self):
        w = self.make_window()
        w.runner.run(["hang"])
        self.assertTrue(wait_until(lambda: w.runner.is_busy()))
        with mock.patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
        ) as ask:
            w.close()
        ask.assert_called_once()
        self.assertFalse(w.isVisible(), "confirming should let the window close")

    def test_ui_state_round_trips_between_sessions(self):
        # Sized against the actual screen: restoreGeometry deliberately clamps a
        # window to fit the available area (that is what keeps a restored window
        # on-screen), so asking for more than the screen has proves nothing.
        available = QApplication.primaryScreen().availableGeometry()
        width, height = min(880, available.width() - 60), min(620, available.height() - 60)
        w = self.make_window()
        w.resize(width, height)
        w.splitter.setSizes([420, 160])
        w.panes["disk"].tree.header().resizeSection(COL_NAME, 317)
        w.panes["disk"].tree.header().moveSection(0, 1)
        w.tabs.setCurrentIndex(2)
        QApplication.processEvents()
        w.close()
        QApplication.processEvents()

        again = MainWindow()
        self.window = again
        self.assertTrue(wait_until(lambda: not again._refreshing))
        self.assertEqual(again.size().width(), width)
        self.assertEqual(again.size().height(), height)
        self.assertEqual(again.tabs.currentIndex(), 2)
        header = again.panes["disk"].tree.header()
        self.assertEqual(header.sectionSize(COL_NAME), 317)
        self.assertEqual(header.visualIndex(COL_NAME), 1, "column order not restored")
        self.assertGreater(again.splitter.sizes()[0], again.splitter.sizes()[1])

    def test_a_restored_column_layout_is_not_autosized_away(self):
        w = self.make_window()
        w.panes["disk"].tree.header().resizeSection(COL_NAME, 299)
        w.close()
        QApplication.processEvents()
        again = MainWindow()
        self.window = again
        self.assertTrue(
            wait_until(lambda: not again._refreshing
                       and again.panes["disk"].tree.topLevelItemCount() > 0)
        )
        self.assertTrue(again.panes["disk"]._columns_sized)
        self.assertEqual(again.panes["disk"].tree.header().sectionSize(COL_NAME), 299)

    def test_a_stale_or_corrupt_column_layout_is_ignored(self):
        # A layout saved before the Encryption column existed has the wrong
        # section count, and QHeaderView refuses it — the app must fall back to
        # autosizing rather than start up with a broken header.
        for kind in KINDS:
            get_settings().setValue(f"ui/columns/{kind}", QByteArray(b"not a header"))
        w = self.make_window()
        pane = w.panes["disk"]
        self.assertEqual(pane.tree.header().count(), len(COLUMNS))
        self.assertTrue(pane._columns_sized, "should have autosized instead")
        self.assertGreater(pane.tree.header().sectionSize(COL_NAME), 20)
        for kind in KINDS:
            get_settings().remove(f"ui/columns/{kind}")

    def test_columns_are_autosized_on_a_first_run(self):
        for kind in KINDS:
            get_settings().remove(f"ui/columns/{kind}")
        w = self.make_window()
        pane = w.panes["disk"]
        self.assertTrue(pane._columns_sized)
        # Wide enough for "base.vdi" rather than a default stub width.
        self.assertGreater(pane.tree.header().sectionSize(COL_NAME), 20)

    def test_backend_failure_is_reported_once(self):
        w = self.make_window()
        w.backends = []
        w._backends_done(1, "")
        w._backends_done(1, "")
        w._backends_done(1, "")
        self.assertEqual(w.runner.output.toPlainText().count("[formats]"), 1)

    def test_creating_a_floppy_adds_no_library_entry(self):
        # createmedium registers what it creates, so a library entry would only
        # cost an extra showmediuminfo on every refresh, for ever.
        w = self.make_window()
        for kind in KINDS:
            save_library(kind, [])
        dlg = CreateFloppyDialog(w)
        dlg.path_edit.setText(os.path.join(self.workdir, "new.img"))
        dlg._ok()
        w.runner.run(dlg.args())
        self.assertTrue(
            wait_until(lambda: not w.runner.is_busy() and not w._refreshing)
        )
        self.assertIsNone(w._pending_library_add)
        self.assertEqual(load_library("floppy"), [])

    def test_selection_survives_refresh(self):
        w = self.make_window()
        pane = w.panes["disk"]
        pane.select_uuid("cccccccc-3333-3333-3333-333333333333")
        w.refresh_media()
        self.assertTrue(wait_until(lambda: not w._refreshing))
        selected = pane.selected_record()
        self.assertIsNotNone(selected)
        self.assertEqual(selected.uuid, "cccccccc-3333-3333-3333-333333333333")


class BatchOperationTest(FakeVBoxManageTest):
    """Multi-selection: every batch action takes what is selected."""

    def select(self, w, *uuids):
        pane = w.panes["disk"]
        pane.tree.clearSelection()
        for uuid in uuids:
            for item in pane._iter_items():
                if item.data(COL_NAME, vboxfront.Qt.ItemDataRole.UserRole) == uuid:
                    item.setSelected(True)
        return pane

    def test_the_tree_allows_more_than_one_row(self):
        w = self.make_window()
        pane = self.select(w, "aaaaaaaa-1111-1111-1111-111111111111",
                           "cccccccc-3333-3333-3333-333333333333")
        self.assertEqual(len(pane.selected_records()), 2)

    def test_compact_queues_one_command_per_selected_disk(self):
        w = self.make_window()
        self.select(w, "aaaaaaaa-1111-1111-1111-111111111111",
                    "bbbbbbbb-2222-2222-2222-222222222222")
        with mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.Yes):
            w.compact_selected()
        self.assertTrue(
            wait_until(lambda: not w.runner.is_busy() and not w._cmd_queue
                       and not w._refreshing),
            "compact queue did not drain",
        )
        with open(self.log) as f:
            compacts = [l for l in f if "--compact" in l]
        self.assertEqual(len(compacts), 2)
        self.assertIn("aaaaaaaa-1111-1111-1111-111111111111", compacts[0])
        self.assertIn("bbbbbbbb-2222-2222-2222-222222222222", compacts[1])

    def test_compact_reports_what_it_actually_reclaimed(self):
        # Measured across the refresh that follows, not predicted: `list -l`
        # cannot see blocks freed inside the guest, which is what compacting
        # gives back.
        w = self.make_window()
        self.select(w, "aaaaaaaa-1111-1111-1111-111111111111")
        w.compact_selected()
        self.assertTrue(
            wait_until(lambda: not w.runner.is_busy() and not w._cmd_queue
                       and not w._refreshing and not w._size_before),
            "compact did not finish",
        )
        # The fake listing never changes, so the honest report is "nothing".
        self.assertIn("nothing to reclaim", w.runner.output.toPlainText())

    def test_a_failed_batch_drops_the_rest_of_the_queue(self):
        w = self.make_window()
        w._run_queue([
            (["hang"], None, None),
            (["modifymedium", "disk", "never", "--compact"], None, None),
        ])
        self.assertTrue(wait_until(lambda: w.runner.is_busy()))
        w.runner.cancel()
        self.assertTrue(wait_until(lambda: not w.runner.is_busy()))
        self.assertEqual(w._cmd_queue, [], "the queue survived a failure")
        with open(self.log) as f:
            self.assertNotIn("never", f.read())

    def test_a_follow_up_belongs_to_its_own_command(self):
        # A batch remove prunes one library entry per medium; a single shared
        # slot would fire against whichever command happened to finish.
        w = self.make_window()
        save_library("disk", ["/fake/base.vdi", "/fake/other.vmdk"])
        done = []
        w._run_queue([
            (["modifymedium", "disk", "a", "--compact"], None, lambda: done.append("a")),
            (["modifymedium", "disk", "b", "--compact"], None, lambda: done.append("b")),
        ])
        self.assertTrue(
            wait_until(lambda: not w.runner.is_busy() and not w._cmd_queue
                       and not w._refreshing),
            "queue did not drain",
        )
        self.assertEqual(done, ["a", "b"])

    def test_remove_skips_the_media_it_cannot_remove(self):
        w = self.make_window()
        # base.vdi is attached to a VM *and* has a child; other.vmdk is free.
        self.select(w, "aaaaaaaa-1111-1111-1111-111111111111",
                    "cccccccc-3333-3333-3333-333333333333")
        with mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.Yes), \
             mock.patch.object(RemoveDialog, "exec", return_value=1):
            w.remove_selected()
        self.assertTrue(
            wait_until(lambda: not w.runner.is_busy() and not w._cmd_queue
                       and not w._refreshing),
            "remove queue did not drain",
        )
        with open(self.log) as f:
            closes = [l for l in f if "closemedium" in l]
        self.assertEqual(len(closes), 1, "a blocked medium was removed anyway")
        self.assertIn("cccccccc-3333-3333-3333-333333333333", closes[0])

    def test_remove_refuses_when_nothing_is_removable(self):
        w = self.make_window()
        self.select(w, "aaaaaaaa-1111-1111-1111-111111111111")
        with mock.patch.object(QMessageBox, "warning") as warn:
            w.remove_selected()
        warn.assert_called_once()
        with open(self.log) as f:
            self.assertNotIn("closemedium", f.read())

    def test_a_batch_remove_prunes_each_library_entry(self):
        w = self.make_window()
        save_library("disk", ["/fake/other.vmdk", "/fake/keep.vdi"])
        self.select(w, "cccccccc-3333-3333-3333-333333333333")
        with mock.patch.object(RemoveDialog, "exec", return_value=1):
            w.remove_selected()
        self.assertTrue(
            wait_until(lambda: not w.runner.is_busy() and not w._cmd_queue
                       and not w._refreshing),
            "remove did not finish",
        )
        self.assertEqual(load_library("disk"), ["/fake/keep.vdi"])

    def test_the_selection_survives_the_refresh_between_batch_steps(self):
        w = self.make_window()
        pane = self.select(w, "aaaaaaaa-1111-1111-1111-111111111111",
                           "cccccccc-3333-3333-3333-333333333333")
        w.refresh_media()
        self.assertTrue(wait_until(lambda: not w._refreshing))
        self.assertEqual(
            sorted(r.uuid for r in pane.selected_records()),
            ["aaaaaaaa-1111-1111-1111-111111111111",
             "cccccccc-3333-3333-3333-333333333333"],
        )


class ListingExportTest(FakeVBoxManageTest):
    def test_export_writes_every_row_of_the_tab(self):
        w = self.make_window()
        w.filter_edit.setText("base")  # a filter must not silently shrink the export
        target = os.path.join(self.workdir, "out.csv")
        with mock.patch("vboxfront.QFileDialog.getSaveFileName",
                        return_value=(target, "CSV (*.csv)")):
            w.export_listing()
        with open(target, newline="") as f:
            # Read it as CSV, not as lines: a quoted Access Error legitimately
            # contains newlines, and counting them is counting the wrong thing.
            rows = list(csv.reader(f))
        self.assertEqual(len(rows), 1 + len(w.panes["disk"].records))
        self.assertEqual(rows[0][0], "name")
        self.assertIn("base.vdi", [r[0] for r in rows[1:]])

    def test_json_export_is_chosen_by_extension(self):
        w = self.make_window()
        target = os.path.join(self.workdir, "out.json")
        with mock.patch("vboxfront.QFileDialog.getSaveFileName",
                        return_value=(target, "")):
            w.export_listing()
        with open(target) as f:
            rows = json.load(f)
        self.assertEqual(len(rows), len(w.panes["disk"].records))

    def test_an_extensionless_name_gets_one(self):
        w = self.make_window()
        target = os.path.join(self.workdir, "listing")
        with mock.patch("vboxfront.QFileDialog.getSaveFileName",
                        return_value=(target, "JSON (*.json)")):
            w.export_listing()
        self.assertTrue(os.path.exists(target + ".json"))

    def test_copy_rows_uses_the_selection_when_there_is_one(self):
        w = self.make_window()
        pane = w.panes["disk"]
        pane.select_uuid("cccccccc-3333-3333-3333-333333333333")
        for item in pane._iter_items():
            item.setSelected(item.text(COL_NAME) == "other.vmdk")
        w.copy_rows_selected()
        text = QApplication.clipboard().text()
        self.assertEqual(len(text.strip().splitlines()), 2, "header plus one row")
        self.assertIn("other.vmdk", text)


class DropTest(FakeVBoxManageTest):
    def _event(self, paths):
        """A drop event carrying these paths.

        The QMimeData is kept on the test: QDropEvent does not take ownership,
        and a collected one leaves the event pointing at freed memory.
        """
        from PyQt6.QtCore import QMimeData, QPointF, QUrl
        from PyQt6.QtCore import Qt as QtCore_Qt
        from PyQt6.QtGui import QDropEvent
        self._mime = QMimeData()
        self._mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
        return QDropEvent(QPointF(1, 1), QtCore_Qt.DropAction.CopyAction, self._mime,
                          QtCore_Qt.MouseButton.LeftButton,
                          QtCore_Qt.KeyboardModifier.NoModifier)

    def _drop(self, w, paths):
        w.dropEvent(self._event(paths))

    def test_a_dropped_image_joins_the_library(self):
        w = self.make_window()
        path = os.path.join(self.workdir, "dropped.vdi")
        with open(path, "w") as f:
            f.write("x")
        self._drop(w, [path])
        self.assertIn(path, load_library("disk"))
        self.assertTrue(wait_until(lambda: not w._refreshing))

    def test_an_iso_lands_on_the_dvd_shelf(self):
        w = self.make_window()
        path = os.path.join(self.workdir, "boot.iso")
        with open(path, "w") as f:
            f.write("x")
        self._drop(w, [path])
        self.assertIn(path, load_library("dvd"))
        self.assertEqual(load_library("disk"), [])

    def test_a_dropped_raw_image_offers_the_importer(self):
        # A .raw is not registerable; it has to go through convertfromraw.
        w = self.make_window()
        path = os.path.join(self.workdir, "sd.raw")
        with open(path, "w") as f:
            f.write("x")
        with mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.Yes), \
             mock.patch.object(MainWindow, "convert_from_raw") as importer:
            self._drop(w, [path])
        importer.assert_called_once_with(path)
        self.assertEqual(load_library("disk"), [])

    def test_anything_else_is_ignored(self):
        w = self.make_window()
        path = os.path.join(self.workdir, "notes.txt")
        with open(path, "w") as f:
            f.write("x")
        self.assertEqual(w._dropped_paths(self._event([path])), [])


class HealthAndScanTest(FakeVBoxManageTest):
    @staticmethod
    def _accepted_dialog(name: str, **attrs):
        """Swap a dialog class for a stub that accepts with these answers.

        Patching the class's own `exec` does not work: the attributes the
        window reads are assigned per instance in __init__, so a patched class
        attribute is shadowed — and `autospec` cannot bind `self` to a Qt slot.
        """
        class Stub:
            def __init__(self, *args, **kwargs):
                for key, value in attrs.items():
                    setattr(self, key, value)

            def exec(self):
                return 1

            def dry_run_caveat(self):
                return "[health] stub"

        return mock.patch(f"vboxfront.{name}", Stub)

    def test_health_reveals_a_medium_in_its_own_tab(self):
        w = self.make_window()
        w.tabs.setCurrentIndex(KINDS.index("floppy"))
        with self._accepted_dialog(
            "HealthDialog",
            reveal=("disk", "dddddddd-4444-4444-4444-444444444444"),
            dry_run_paths=[],
        ):
            w.open_health()
        self.assertEqual(w.current_kind(), "disk")
        self.assertEqual(w.selected_medium().uuid,
                         "dddddddd-4444-4444-4444-444444444444")

    def test_the_dry_run_queues_one_read_only_check_per_vdi(self):
        w = self.make_window()
        paths = ["/fake/base.vdi", "/fake/child.vdi"]
        with self._accepted_dialog("HealthDialog", reveal=None, dry_run_paths=paths):
            w.open_health()
        self.assertTrue(
            wait_until(lambda: not w.runner.is_busy() and not w._cmd_queue
                       and not w._refreshing),
            "dry run queue did not drain",
        )
        with open(self.log) as f:
            checks = [l for l in f if "repairhd" in l]
        self.assertEqual(len(checks), 2)
        self.assertTrue(all("-dry-run" in l for l in checks), "a check could write")

    def test_the_orphan_scan_adds_what_was_chosen(self):
        w = self.make_window()
        found = [("disk", "/fake/found.vdi"), ("dvd", "/fake/found.iso")]
        with self._accepted_dialog("OrphanScanDialog", chosen=found):
            w.open_orphan_scan()
        self.assertTrue(wait_until(lambda: not w._refreshing))
        self.assertIn("/fake/found.vdi", load_library("disk"))
        self.assertIn("/fake/found.iso", load_library("dvd"))

    def test_the_scan_is_told_what_is_already_known(self):
        w = self.make_window()
        add_library_path("floppy", "/fake/boot.img")
        captured = {}

        def fake_init(dlg_self, known, parent=None):
            captured["known"] = known
            OrphanScanDialog.__mro__[1].__init__(dlg_self, parent)
            dlg_self.chosen = []

        with mock.patch.object(OrphanScanDialog, "__init__", fake_init), \
             mock.patch.object(OrphanScanDialog, "exec", return_value=0):
            w.open_orphan_scan()
        self.assertIn("/fake/base.vdi", captured["known"], "registered media")
        self.assertIn("/fake/boot.img", captured["known"], "library entries")


class SnapshotColumnTest(FakeVBoxManageTest):
    def test_the_snapshot_name_is_shown(self):
        w = self.make_window()
        pane = w.panes["disk"]
        base = next(i for i in pane._iter_items() if i.text(COL_NAME) == "base.vdi")
        # The fixture attaches base.vdi to a VM with no snapshot.
        self.assertEqual(base.text(COL_SNAPSHOT), "")
        rec = pane._by_uuid["aaaaaaaa-1111-1111-1111-111111111111"]
        rec.in_use = ["vbf-smoke (UUID: 24aa0bbd-3d9c-4ba3-a41f-273c0ab57661) "
                      "[Before update (UUID: 24aa0bbd-3d9c-4ba3-a41f-273c0ab57662)]"]
        pane.populate(pane.records)
        base = next(i for i in pane._iter_items() if i.text(COL_NAME) == "base.vdi")
        self.assertEqual(base.text(COL_SNAPSHOT), "Before update")
        self.assertEqual(base.text(COL_IN_USE), "vbf-smoke")


if __name__ == "__main__":
    unittest.main()
