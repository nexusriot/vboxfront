"""MainWindow integration tests against a fake VBoxManage executable."""

import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication, QMessageBox

from vboxfront import KINDS, MainWindow, get_settings, save_library

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
Type:           normal (base)
Location:       /fake/gone.vdi
Storage format: VDI
Capacity:       512 MBytes
Size on disk:   0 MBytes
"""


def wait_until(cond, timeout=10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


class MainWindowTest(unittest.TestCase):
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
            '  modifymedium*) echo "0%...50%...100%" ;;\n'
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
        self.assertEqual(base.text(2), "10.0 GB")
        self.assertEqual(base.text(6), "vbf-smoke")

    def test_inaccessible_media_are_highlighted(self):
        w = self.make_window()
        pane = w.panes["disk"]
        gone = next(i for i in pane._iter_items() if i.text(0) == "gone.vdi")
        self.assertEqual(gone.foreground(0).color(), QColor(220, 80, 80))

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

    def test_selection_survives_refresh(self):
        w = self.make_window()
        pane = w.panes["disk"]
        pane.select_uuid("cccccccc-3333-3333-3333-333333333333")
        w.refresh_media()
        self.assertTrue(wait_until(lambda: not w._refreshing))
        selected = pane.selected_record()
        self.assertIsNotNone(selected)
        self.assertEqual(selected.uuid, "cccccccc-3333-3333-3333-333333333333")


if __name__ == "__main__":
    unittest.main()
