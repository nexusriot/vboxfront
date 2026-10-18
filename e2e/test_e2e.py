"""End-to-end workflows against a stateful fake VBoxManage.

The unit suite checks that each command is *built* correctly; this one checks
that the workflows built out of them *work*. The app is unmodified and real:
a real QApplication, a real MainWindow, real QProcess launches, real files on
a real filesystem. Only VBoxManage is replaced — by e2e/fake_vboxmanage.py,
which keeps a registry and writes actual image files, so every assertion here
can look at what changed rather than at what was printed.

Hermetic by construction: no VirtualBox, no display, no network, no state
outside the per-test temporary directory. Runs in Docker via `make e2e`, and
directly on any host that has PyQt6.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

# The app and the fake live next to each other; importing the app by path
# keeps this suite runnable from anywhere, container included.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QProcess, Qt  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QFileDialog,
    QInputDialog,
    QMessageBox,
)

import vboxfront  # noqa: E402
from e2e import fake_vboxmanage  # noqa: E402
from vboxfront import (  # noqa: E402
    COL_CAPACITY,
    COL_IN_USE,
    COL_NAME,
    COL_STATE,
    KINDS,
    MainWindow,
    get_settings,
    load_history,
    load_library,
    save_library,
)

VM_UUID = "11111111-1111-1111-1111-111111111111"
SECOND_VM_UUID = "22222222-2222-2222-2222-222222222222"

YES = QMessageBox.StandardButton.Yes
NO = QMessageBox.StandardButton.No


def wait_until(cond, timeout=30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return False


class E2ECase(unittest.TestCase):
    """One window, one registry, one temp directory, torn down completely."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv[:1])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vboxfront-e2e-")
        self.images = os.path.join(self.dir, "images")
        os.makedirs(self.images)
        self.state_path = os.path.join(self.dir, "state.json")
        self.log = os.path.join(self.dir, "calls.log")
        self.write_state(media=[], vms=[self.vm("alpha", VM_UUID)])

        # A tiny shim on PATH rather than the .py itself: the app launches
        # whatever `vboxmanage_path` points at, and this is what a real
        # installation looks like from its side.
        self.fake = os.path.join(self.dir, "VBoxManage")
        real = os.path.join(ROOT, "e2e", "fake_vboxmanage.py")
        with open(self.fake, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/bin/sh\n"
                f'VBOXFRONT_E2E_STATE="{self.state_path}" '
                f'VBOXFRONT_E2E_LOG="{self.log}" '
                f'exec "{sys.executable}" -I "{real}" "$@"\n'
            )
        os.chmod(self.fake, os.stat(self.fake).st_mode | stat.S_IXUSR)

        get_settings().setValue("vboxmanage_path", self.fake)
        get_settings().remove("history")
        get_settings().remove("trash/enabled")
        get_settings().remove("trash/dir")
        for kind in KINDS:
            save_library(kind, [])
        self.window: MainWindow | None = None

    def tearDown(self):
        if self.window is not None:
            if self.window.runner.is_busy():
                self.window.runner.cancel()
                wait_until(lambda: not self.window.runner.is_busy())
            wait_until(lambda: not self.window._refreshing)
            with mock.patch.object(QMessageBox, "question", return_value=YES):
                self.window.close()
            self.window.deleteLater()
            self.window = None
        QApplication.processEvents()
        get_settings().setValue("vboxmanage_path", "")
        get_settings().remove("trash/enabled")
        get_settings().remove("trash/dir")

    # ---- registry fixture -------------------------------------------------

    @staticmethod
    def vm(name: str, uuid: str, controller: str = "SATA",
           ctl_type: str = "IntelAhci", ports: int = 4) -> dict:
        return {
            "name": name, "uuid": uuid, "attachments": [],
            "controllers": [{"name": controller, "type": ctl_type, "ports": ports}],
        }

    def write_state(self, **state) -> None:
        with open(self.state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)

    def read_state(self) -> dict:
        with open(self.state_path, encoding="utf-8") as handle:
            return json.load(handle)

    def seed_disk(self, name: str, capacity_mb: int = 64, **extra) -> dict:
        """Put an image on disk and in the registry before the window opens."""
        path = os.path.join(self.images, name)
        with open(path, "wb") as handle:
            handle.truncate(capacity_mb * 1024 * 1024)
        state = self.read_state()
        medium = {
            "uuid": extra.pop("uuid", None) or f"{len(state['media']) + 1:08d}"
                    "-0000-0000-0000-000000000000",
            "location": path, "kind": extra.pop("kind", "disk"),
            "format": extra.pop("format", "VDI"), "capacity_mb": capacity_mb,
            "variant": "dynamic default", "type": "normal", "state": "created",
            "parent": "", "encryption": "disabled", "properties": {},
        }
        medium.update(extra)
        # The fake keeps a medium's UUID inside its image, as VirtualBox does;
        # a seeded file has to carry one too, or a copy of it would not be
        # recognised as a copy.
        fake_vboxmanage.stamp_uuid(path, medium["uuid"])
        state["media"].append(medium)
        self.write_state(**state)
        return medium

    def calls(self) -> list[str]:
        try:
            with open(self.log, encoding="utf-8") as handle:
                return [line.rstrip("\n") for line in handle]
        except OSError:
            return []

    # ---- driving the window ----------------------------------------------

    def open_window(self) -> MainWindow:
        self.window = MainWindow()
        self.window.tabs.setCurrentIndex(KINDS.index("disk"))
        self.assertTrue(self.settled(), "the first refresh never finished")
        return self.window

    def settled(self, timeout=30.0) -> bool:
        """Wait until nothing is in flight — captures included.

        Half the work happens through CaptureRunner rather than the visible
        runner: the refresh chain, the attach dialog's controller read, and
        the `showvminfo` a detach needs to locate its slot. Waiting only on
        the runner would look settled while a detach had not started yet.
        """
        return wait_until(
            lambda: not self.window._refreshing
            and not self.window.runner.is_busy()
            and not self.window._cmd_queue
            and not self.captures_running(),
            timeout,
        )

    def captures_running(self) -> bool:
        return any(
            capture.state() != QProcess.ProcessState.NotRunning
            for capture in self.window.findChildren(vboxfront.CaptureRunner)
        )

    def rows(self, kind: str = "disk") -> dict[str, object]:
        pane = self.window.panes[kind]
        return {item.text(COL_NAME): item for item in pane._iter_items()}

    def select(self, name: str, kind: str = "disk"):
        pane = self.window.panes[kind]
        self.window.tabs.setCurrentIndex(KINDS.index(kind))
        item = self.rows(kind)[name]
        pane.tree.setCurrentItem(item)
        pane.tree.clearSelection()
        item.setSelected(True)
        return pane.selected_record()

    def select_many(self, names: list[str], kind: str = "disk"):
        pane = self.window.panes[kind]
        self.window.tabs.setCurrentIndex(KINDS.index(kind))
        rows = self.rows(kind)
        # setCurrentItem clears the selection, so it goes first or it undoes
        # everything selected after it.
        pane.tree.setCurrentItem(rows[names[0]])
        pane.tree.clearSelection()
        for name in names:
            rows[name].setSelected(True)
        self.assertEqual(
            {record.location for record in pane.selected_records()},
            {self.rows(kind)[name].text(9) for name in names},
        )

    def run_dialog(self, action, prepare, accept=True, timeout=30.0):
        """Invoke a window action, filling in the dialog it opens.

        `prepare(dlg)` runs on the live dialog instead of exec() blocking on a
        human; whatever it leaves in the widgets is what the app then reads.

        The stand-in has to stay in place while the event loop turns: Attach
        reads `list vms` first and only builds its dialog in the callback, so
        a patch that ended with `action()` would let the real modal exec()
        through and hang the suite.
        """
        captured = {}

        def fake_exec(dlg):
            captured["dlg"] = dlg
            prepare(dlg)
            return QDialog.DialogCode.Accepted if accept else QDialog.DialogCode.Rejected

        with mock.patch.object(QDialog, "exec", fake_exec):
            action()
            self.assertTrue(
                wait_until(lambda: "dlg" in captured, timeout),
                "the action never opened a dialog",
            )
        return captured["dlg"]

    def delete_the_file(self, w):
        """Remove the selected medium, choosing the delete-the-file action."""
        def prepare(dlg):
            dlg.mode.setCurrentIndex(1)  # the delete action, trash or not
            with mock.patch.object(QMessageBox, "warning", return_value=YES):
                dlg._ok()

        self.run_dialog(w.remove_selected, prepare)
        self.assertTrue(self.settled())

    def trash_dir(self) -> str:
        return os.path.join(self.images, vboxfront.TRASH_DIR_NAME)


class CreateAndRemoveTest(E2ECase):
    def test_a_created_disk_reaches_the_registry_the_file_and_the_tree(self):
        w = self.open_window()
        self.assertEqual(self.rows(), {})
        target = os.path.join(self.images, "fresh.vdi")

        def prepare(dlg):
            dlg.path_edit.setText(target)
            dlg.spin.setValue(96)
            dlg.fmt_combo.setCurrentText("VDI")
            dlg._ok()

        self.run_dialog(w.create_disk, prepare)
        self.assertTrue(self.settled())

        self.assertTrue(os.path.isfile(target), "no image file was written")
        registry = [m["location"] for m in self.read_state()["media"]]
        self.assertIn(target, registry)
        row = self.rows()["fresh.vdi"]
        self.assertEqual(row.text(COL_CAPACITY), "96 MB")
        self.assertEqual(row.text(COL_STATE), "created")

    def test_removing_with_delete_moves_the_file_to_the_trash_and_drops_the_row(self):
        self.seed_disk("doomed.vdi")
        w = self.open_window()
        path = self.select("doomed.vdi").location
        self.assertTrue(os.path.exists(path))

        self.delete_the_file(w)

        trashed = os.path.join(self.trash_dir(), "doomed.vdi")
        self.assertFalse(os.path.exists(path), "the file was left where it was")
        self.assertTrue(os.path.exists(trashed), "nothing reached the trash")
        # The point of the trash: VBoxManage must not have been asked to
        # unlink anything, because nothing survives that.
        self.assertNotIn("--delete", " ".join(self.calls()))
        self.assertEqual(self.read_state()["media"], [])
        self.assertNotIn("doomed.vdi", self.rows())

    def test_undo_delete_brings_back_the_file_the_registration_and_the_row(self):
        self.seed_disk("regret.vdi")
        w = self.open_window()
        path = self.select("regret.vdi").location
        before = os.path.getsize(path)
        save_library("disk", [path])

        self.delete_the_file(w)
        self.assertNotIn("regret.vdi", self.rows())
        self.assertEqual(load_library("disk"), [])
        self.assertTrue(w.act_undo_delete.isEnabled())

        w.undo_delete()
        self.assertTrue(self.settled())

        self.assertTrue(os.path.exists(path), "the file never came back")
        self.assertEqual(os.path.getsize(path), before, "a different file came back")
        self.assertEqual(os.listdir(self.trash_dir()), [])
        self.assertIn(path, load_library("disk"))
        # The library entry is re-opened by the refresh, so the medium is
        # registered again and the row is back — under a new UUID, since the
        # old registration really was closed.
        registry = [m["location"] for m in self.read_state()["media"]]
        self.assertEqual(registry, [path])
        self.assertIn("regret.vdi", self.rows())
        self.assertFalse(w.act_undo_delete.isEnabled())

    def test_a_batch_delete_is_undone_as_one(self):
        for name in ("one.vdi", "two.vdi"):
            self.seed_disk(name)
        w = self.open_window()
        self.select_many(["one.vdi", "two.vdi"])
        paths = [os.path.join(self.images, name) for name in ("one.vdi", "two.vdi")]

        self.delete_the_file(w)
        self.assertEqual([p for p in paths if os.path.exists(p)], [])

        # One mistake, one undo: a Remove of two disks puts both back.
        w.undo_delete()
        self.assertTrue(self.settled())

        self.assertEqual([p for p in paths if os.path.exists(p)], paths)
        self.assertEqual(sorted(self.rows()), ["one.vdi", "two.vdi"])
        self.assertFalse(w.act_undo_delete.isEnabled())

    def test_with_the_trash_off_the_file_is_really_unlinked(self):
        get_settings().setValue("trash/enabled", False)
        self.seed_disk("doomed.vdi")
        w = self.open_window()
        path = self.select("doomed.vdi").location

        self.delete_the_file(w)

        self.assertFalse(os.path.exists(path), "the backing file survived --delete")
        self.assertFalse(os.path.isdir(self.trash_dir()))
        self.assertIn("--delete", " ".join(self.calls()))
        self.assertEqual(self.read_state()["media"], [])
        self.assertFalse(w.act_undo_delete.isEnabled())

    def test_unregistering_keeps_the_file(self):
        self.seed_disk("kept.vdi")
        w = self.open_window()
        path = self.select("kept.vdi").location

        self.run_dialog(w.remove_selected, lambda dlg: dlg._ok())
        self.assertTrue(self.settled())

        self.assertTrue(os.path.exists(path), "unregister must not delete")
        self.assertEqual(self.read_state()["media"], [])

    def test_creating_over_an_existing_file_is_refused_before_launching(self):
        taken = os.path.join(self.images, "taken.vdi")
        open(taken, "wb").close()
        w = self.open_window()

        def prepare(dlg):
            dlg.path_edit.setText(taken)
            with mock.patch.object(QMessageBox, "warning") as warn:
                dlg._ok()
            warn.assert_called_once()

        self.run_dialog(w.create_disk, prepare, accept=False)
        self.assertTrue(self.settled())
        self.assertNotIn("createmedium", " ".join(self.calls()))


    def test_a_failed_unregister_never_moves_the_file(self):
        # The move is the follow-up to a command that exited 0, and this is
        # why: a medium the registry no longer has is one closemedium cannot
        # close, and a file moved out from under it would be a second fault
        # on top of the first.
        self.seed_disk("contested.vdi")
        w = self.open_window()
        path = self.select("contested.vdi").location
        # The registry loses it behind the app's back — another client, or a
        # VBoxSVC that restarted between the refresh and the click.
        self.write_state(media=[], vms=[self.vm("alpha", VM_UUID)])

        self.delete_the_file(w)

        self.assertIn("closemedium", " ".join(self.calls()))
        self.assertTrue(os.path.exists(path), "a failed unregister moved the file")
        self.assertFalse(os.path.isdir(self.trash_dir()))
        self.assertFalse(w.act_undo_delete.isEnabled(),
                         "an undo was offered for a delete that never happened")

    def test_the_newest_delete_is_the_one_undone(self):
        for name in ("older.vdi", "newer.vdi"):
            self.seed_disk(name)
        w = self.open_window()
        older = os.path.join(self.images, "older.vdi")
        newer = os.path.join(self.images, "newer.vdi")

        self.select("older.vdi")
        self.delete_the_file(w)
        self.select("newer.vdi")
        self.delete_the_file(w)
        self.assertEqual([p for p in (older, newer) if os.path.exists(p)], [])

        w.undo_delete()
        self.assertTrue(self.settled())
        self.assertTrue(os.path.exists(newer), "the newest delete was not the one undone")
        self.assertFalse(os.path.exists(older), "an older delete came back with it")
        self.assertTrue(w.act_undo_delete.isEnabled(),
                        "the older delete stopped being undoable")

        w.undo_delete()
        self.assertTrue(self.settled())
        self.assertTrue(os.path.exists(older))
        self.assertEqual(sorted(self.rows()), ["newer.vdi", "older.vdi"])
        self.assertFalse(w.act_undo_delete.isEnabled())

    def test_a_medium_whose_file_is_already_gone_is_only_unregistered(self):
        self.seed_disk("ghost.vdi")
        w = self.open_window()
        path = self.select("ghost.vdi").location
        os.remove(path)  # something outside the app took it; the registry has not noticed

        self.delete_the_file(w)

        self.assertEqual(self.read_state()["media"], [])
        self.assertFalse(os.path.isdir(self.trash_dir()),
                         "a trash folder was made for a file that was not there")
        self.assertIn("already gone", w.runner.output.toPlainText())
        self.assertFalse(w.act_undo_delete.isEnabled(),
                         "an undo was offered for a file that never moved")

    def test_a_dvd_goes_to_the_trash_and_comes_back_to_its_own_shelf(self):
        self.seed_disk("install.iso", kind="dvd", format="ISO")
        w = self.open_window()
        path = self.select("install.iso", kind="dvd").location

        self.delete_the_file(w)
        self.assertNotIn("install.iso", self.rows("dvd"))
        self.assertTrue(os.path.exists(os.path.join(self.trash_dir(), "install.iso")))

        w.undo_delete()
        self.assertTrue(self.settled())

        self.assertTrue(os.path.exists(path))
        # The kind travels with the file: put back into the disk library, a
        # DVD would come back on the wrong shelf — or not at all.
        self.assertEqual(load_library("dvd"), [path])
        self.assertEqual(load_library("disk"), [])
        self.assertIn("install.iso", self.rows("dvd"))


class TrashSettingsTest(E2ECase):
    """What Settings says about deleting is what deleting then does.

    The other trash scenarios set the keys directly; these go through the
    dialog the user actually has, because a setting nothing reads is the
    easiest kind of lie to ship.
    """

    def change_settings(self, w, fill):
        self.run_dialog(w.open_settings, lambda dlg: (fill(dlg), dlg._ok()))
        self.assertTrue(self.settled())

    def test_turning_the_trash_off_brings_back_the_real_delete(self):
        self.seed_disk("doomed.vdi")
        w = self.open_window()
        path = self.select("doomed.vdi").location

        self.change_settings(w, lambda dlg: dlg.trash_check.setChecked(False))

        self.select("doomed.vdi")
        self.delete_the_file(w)
        self.assertIn("--delete", " ".join(self.calls()))
        self.assertFalse(os.path.exists(path), "the file was supposed to be erased")
        self.assertFalse(os.path.isdir(self.trash_dir()))
        self.assertFalse(w.act_undo_delete.isEnabled(),
                         "an unlinked file cannot be undone")

    def test_a_trash_folder_set_in_settings_is_where_the_file_lands(self):
        self.seed_disk("doomed.vdi")
        attic = os.path.join(self.dir, "attic")
        w = self.open_window()
        path = self.select("doomed.vdi").location

        self.change_settings(w, lambda dlg: dlg.trash_edit.setText(attic))

        self.select("doomed.vdi")
        self.delete_the_file(w)
        self.assertEqual(os.listdir(attic), ["doomed.vdi"])
        self.assertFalse(os.path.isdir(self.trash_dir()),
                         "the default trash was used although one was configured")

        w.undo_delete()
        self.assertTrue(self.settled())
        self.assertTrue(os.path.exists(path))
        self.assertEqual(os.listdir(attic), [])
        self.assertIn("doomed.vdi", self.rows())

    def test_the_vboxmanage_path_survives_a_trash_change(self):
        # The dialog writes every setting it owns on OK, so a trash edit must
        # not take the fake VBoxManage down with it.
        w = self.open_window()
        self.change_settings(w, lambda dlg: dlg.trash_check.setChecked(False))
        self.assertEqual(get_settings().value("vboxmanage_path", "", str), self.fake)


class ResizeCompactTest(E2ECase):
    def test_growing_a_disk_moves_the_capacity_the_listing_reports(self):
        self.seed_disk("grow.vdi", capacity_mb=64)
        w = self.open_window()
        self.select("grow.vdi")
        self.assertEqual(self.rows()["grow.vdi"].text(COL_CAPACITY), "64 MB")

        def prepare(dlg):
            dlg.spin.setValue(256)
            with mock.patch.object(QMessageBox, "warning", return_value=YES):
                dlg._ok()

        self.run_dialog(w.resize_selected, prepare)
        self.assertTrue(self.settled())

        self.assertEqual(self.rows()["grow.vdi"].text(COL_CAPACITY), "256 MB")
        self.assertEqual(self.read_state()["media"][0]["capacity_mb"], 256)

    def test_compacting_reports_what_it_actually_gave_back(self):
        # Seeded fully allocated; the fake's compact shrinks the file, and the
        # reclaim line is measured across the refresh rather than predicted.
        medium = self.seed_disk("fat.vdi", capacity_mb=64)
        with open(medium["location"], "wb") as handle:
            handle.write(b"\0" * 64 * 1024 * 1024)
        w = self.open_window()
        before = os.stat(medium["location"]).st_blocks
        self.select("fat.vdi")

        with mock.patch.object(QMessageBox, "question", return_value=YES):
            w.compact_selected()
        self.assertTrue(self.settled())

        self.assertLess(os.stat(medium["location"]).st_blocks, before)
        self.assertIn("[compact] fat.vdi: reclaimed",
                      w.runner.output.toPlainText())

    def test_compact_all_vdis_queues_every_one_of_them(self):
        for name in ("one.vdi", "two.vdi", "three.vdi"):
            medium = self.seed_disk(name, capacity_mb=32)
            with open(medium["location"], "wb") as handle:
                handle.write(b"\0" * 32 * 1024 * 1024)
        w = self.open_window()

        with mock.patch.object(QMessageBox, "question", return_value=YES):
            w.compact_all_vdis()
        self.assertTrue(self.settled())

        compacts = [c for c in self.calls() if "--compact" in c]
        self.assertEqual(len(compacts), 3)
        self.assertEqual(w.runner.output.toPlainText().count("reclaimed"), 3)


class CloneTest(E2ECase):
    def test_a_clone_produces_a_second_registered_image(self):
        self.seed_disk("src.vdi", capacity_mb=48)
        w = self.open_window()
        self.select("src.vdi")
        target = os.path.join(self.images, "copy.vmdk")

        def prepare(dlg):
            dlg.fmt.setCurrentText("VMDK")
            dlg.path_edit.setText(target)
            with mock.patch.object(QMessageBox, "warning", return_value=YES):
                dlg._ok()

        self.run_dialog(w.convert_selected, prepare)
        self.assertTrue(self.settled())

        self.assertTrue(os.path.isfile(target))
        rows = self.rows()
        self.assertIn("copy.vmdk", rows)
        self.assertEqual(rows["copy.vmdk"].text(1), "vmdk")
        self.assertTrue(os.path.isfile(os.path.join(self.images, "src.vdi")),
                        "the source must be left alone")

    def test_a_batch_convert_clones_every_selected_disk(self):
        self.seed_disk("a.vdi", capacity_mb=16)
        self.seed_disk("b.vdi", capacity_mb=16)
        out = os.path.join(self.dir, "out")
        os.makedirs(out)
        w = self.open_window()
        self.select_many(["a.vdi", "b.vdi"])

        def prepare(dlg):
            dlg.dir_edit.setText(out)
            dlg.fmt.setCurrentText("VDI")
            with mock.patch.object(QMessageBox, "question", return_value=YES):
                dlg._ok()

        self.run_dialog(w.convert_selected, prepare)
        self.assertTrue(self.settled())

        self.assertEqual(sorted(os.listdir(out)),
                         ["a-converted.vdi", "b-converted.vdi"])

    def test_a_batch_target_that_is_another_source_never_runs(self):
        """Regression: the caller deletes every target before the queue starts.

        A plan that aims one disk's clone at another's backing file therefore
        destroyed that disk before its own turn came round — which is exactly
        what re-running a convert over its own output folder produced.
        """
        self.seed_disk("a.vdi", capacity_mb=16)
        previous = self.seed_disk("a-converted.vdi", capacity_mb=16)
        w = self.open_window()
        self.select_many(["a.vdi", "a-converted.vdi"])

        def prepare(dlg):
            dlg.dir_edit.setText(self.images)
            dlg.fmt.setCurrentText("VDI")
            with mock.patch.object(QMessageBox, "warning") as warn, \
                 mock.patch.object(QMessageBox, "question", return_value=YES):
                dlg._ok()
            self.assertIn("source disk", warn.call_args[0][2])

        self.run_dialog(w.convert_selected, prepare, accept=False)
        self.assertTrue(self.settled())

        self.assertTrue(os.path.exists(previous["location"]),
                        "a selected disk's backing file was destroyed")
        self.assertNotIn("clonemedium", " ".join(self.calls()))


class AttachDetachTest(E2ECase):
    def test_attach_then_detach_round_trips_through_the_vm(self):
        self.seed_disk("guest.vdi")
        w = self.open_window()
        self.select("guest.vdi")
        self.assertEqual(self.rows()["guest.vdi"].text(COL_IN_USE), "")

        def prepare(dlg):
            self.assertTrue(wait_until(lambda: dlg.ctl_combo.count() > 0),
                            "the attach dialog never read the controllers")
            dlg.port_spin.setValue(1)
            dlg._ok()

        self.run_dialog(w.attach_selected, prepare)
        self.assertTrue(self.settled())

        attachments = self.read_state()["vms"][0]["attachments"]
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["port"], 1)
        self.assertEqual(self.rows()["guest.vdi"].text(COL_IN_USE), "alpha")

        self.select("guest.vdi")
        with mock.patch.object(QMessageBox, "question", return_value=YES):
            w.detach_selected()
        self.assertTrue(self.settled())

        self.assertEqual(self.read_state()["vms"][0]["attachments"], [])
        self.assertEqual(self.rows()["guest.vdi"].text(COL_IN_USE), "")

    def test_the_dialog_pre_selects_the_first_free_port(self):
        medium = self.seed_disk("second.vdi")
        state = self.read_state()
        state["vms"][0]["attachments"] = [
            {"controller": "SATA", "port": 0, "device": 0,
             "uuid": "ffffffff-0000-0000-0000-000000000000",
             "location": "/elsewhere/other.vdi"}
        ]
        self.write_state(**state)
        w = self.open_window()
        self.select("second.vdi")

        seen = {}

        def prepare(dlg):
            self.assertTrue(wait_until(lambda: dlg.ctl_combo.count() > 0))
            seen["port"] = dlg.port_spin.value()
            seen["device_max"] = dlg.device_spin.maximum()
            dlg._ok()

        self.run_dialog(w.attach_selected, prepare)
        self.assertTrue(self.settled())

        self.assertEqual(seen["port"], 1, "port 0 was already occupied")
        self.assertEqual(seen["device_max"], 0, "SATA has no device 1")
        self.assertEqual(
            {a["port"] for a in self.read_state()["vms"][0]["attachments"]},
            {0, 1},
        )
        self.assertEqual(medium["capacity_mb"], 64)

    def test_an_attached_medium_cannot_be_removed(self):
        self.seed_disk("busy.vdi")
        w = self.open_window()
        self.select("busy.vdi")

        def prepare(dlg):
            self.assertTrue(wait_until(lambda: dlg.ctl_combo.count() > 0))
            dlg._ok()

        self.run_dialog(w.attach_selected, prepare)
        self.assertTrue(self.settled())

        self.select("busy.vdi")
        with mock.patch.object(QMessageBox, "warning") as warn:
            w.remove_selected()
        warn.assert_called_once()
        self.assertIn("attached to alpha", warn.call_args[0][2])
        self.assertNotIn("closemedium", " ".join(self.calls()))

    def test_detaching_a_selection_reads_each_vm_once(self):
        self.seed_disk("d1.vdi")
        self.seed_disk("d2.vdi")
        state = self.read_state()
        state["vms"][0]["attachments"] = [
            {"controller": "SATA", "port": 0, "device": 0,
             "uuid": state["media"][0]["uuid"],
             "location": state["media"][0]["location"]},
            {"controller": "SATA", "port": 1, "device": 0,
             "uuid": state["media"][1]["uuid"],
             "location": state["media"][1]["location"]},
        ]
        self.write_state(**state)
        w = self.open_window()
        self.select_many(["d1.vdi", "d2.vdi"])

        before = len([c for c in self.calls() if c.startswith("showvminfo")])
        with mock.patch.object(QMessageBox, "question", return_value=YES):
            w.detach_selected()
        self.assertTrue(self.settled())

        after = [c for c in self.calls() if c.startswith("showvminfo")]
        self.assertEqual(len(after) - before, 1,
                         "one dump per VM, not one per attachment")
        self.assertEqual(self.read_state()["vms"][0]["attachments"], [])


class DifferencingChainTest(E2ECase):
    def test_a_child_nests_under_its_parent_and_locks_it(self):
        self.seed_disk("base.vdi", capacity_mb=64)
        w = self.open_window()
        self.select("base.vdi")
        child = os.path.join(self.images, "base-diff.vdi")

        def prepare(dlg):
            dlg.path_edit.setText(child)
            dlg._ok()

        self.run_dialog(w.create_diff, prepare)
        self.assertTrue(self.settled())

        pane = w.panes["disk"]
        self.assertEqual(pane.tree.topLevelItemCount(), 1)
        top = pane.tree.topLevelItem(0)
        self.assertEqual(top.text(COL_NAME), "base.vdi")
        self.assertEqual(top.child(0).text(COL_NAME), "base-diff.vdi")

        # The parent is read-only now; the app says so instead of letting
        # VBoxManage answer with VERR_VD_IMAGE_READ_ONLY.
        self.select("base.vdi")
        with mock.patch.object(QMessageBox, "warning") as warn:
            w.resize_selected()
        warn.assert_called_once()
        self.assertIn("base-diff.vdi", warn.call_args[0][2])

        self.select("base.vdi")
        with mock.patch.object(QMessageBox, "warning") as warn:
            w.remove_selected()
        self.assertIn("base-diff.vdi", warn.call_args[0][2])

    def test_a_cycle_in_the_registry_never_hides_a_medium(self):
        """Regression: looping Parent UUIDs left every medium in the loop with
        no root to hang from, so the rows simply vanished while the totals
        line went on counting them. `sethdparentuuid` can stamp exactly that.
        """
        first = self.seed_disk("left.vdi")
        second = self.seed_disk("right.vdi")
        state = self.read_state()
        state["media"][0]["parent"] = second["uuid"]
        state["media"][1]["parent"] = first["uuid"]
        self.write_state(**state)

        w = self.open_window()
        self.assertEqual(set(self.rows()), {"left.vdi", "right.vdi"})
        self.assertNotIn("shown", w.panes["disk"].totals.text())


class EncryptionTest(E2ECase):
    def test_encrypt_check_and_decrypt_round_trip_over_stdin(self):
        self.seed_disk("secret.vdi")
        w = self.open_window()
        self.select("secret.vdi")

        def encrypt(dlg):
            dlg.encrypt_radio.setChecked(True)
            dlg.password.setText("correct horse")
            dlg.confirm.setText("correct horse")
            dlg.pwid.setText("e2e-key")
            dlg._ok()

        self.run_dialog(w.encrypt_selected, encrypt)
        self.assertTrue(self.settled())

        medium = self.read_state()["media"][0]
        self.assertEqual(medium["encryption"], "enabled")
        self.assertEqual(medium["password"], "correct horse")
        self.assertEqual(medium["password_id"], "e2e-key")
        self.assertIn("AES-XTS256-PLAIN64",
                      self.rows()["secret.vdi"].text(6))
        self.assertNotIn("correct horse", " ".join(self.calls()),
                         "the password must never reach the command line")

        # A wrong password is rejected by the command, not by the dialog.
        self.select("secret.vdi")

        def check_wrong(dlg):
            dlg.old_password.setText("wrong")
            dlg._check_password()

        self.run_dialog(w.encrypt_selected, check_wrong)
        self.assertTrue(self.settled())
        self.assertIn("[exit 1]", w.runner.output.toPlainText())

        self.select("secret.vdi")

        def decrypt(dlg):
            dlg.decrypt_radio.setChecked(True)
            dlg.old_password.setText("correct horse")
            dlg._ok()

        self.run_dialog(w.encrypt_selected, decrypt)
        self.assertTrue(self.settled())
        self.assertEqual(self.read_state()["media"][0]["encryption"], "disabled")

    def test_changing_a_password_shreds_both_temp_files(self):
        # Two secrets cannot share one stdin stream, so this path spills them
        # to 0600 files; nothing of them may outlive the command.
        self.seed_disk("rekey.vdi", encryption="enabled", password="old one",
                       cipher="AES-XTS256-PLAIN64", password_id="before")
        w = self.open_window()
        self.select("rekey.vdi")

        written: list[str] = []
        real_write = vboxfront.write_secret_file

        def spy(secret):
            path = real_write(secret)
            written.append(path)
            return path

        def change(dlg):
            dlg.change_radio.setChecked(True)
            dlg.old_password.setText("old one")
            dlg.password.setText("new one")
            dlg.confirm.setText("new one")
            dlg.pwid.setText("after")
            dlg._ok()

        with mock.patch.object(vboxfront, "write_secret_file", spy):
            self.run_dialog(w.encrypt_selected, change)
            self.assertTrue(self.settled())

        self.assertEqual(len(written), 2)
        for path in written:
            self.assertFalse(os.path.exists(path), f"{path} outlived the command")
        medium = self.read_state()["media"][0]
        self.assertEqual(medium["password"], "new one")
        self.assertEqual(medium["password_id"], "after")

    def test_a_replay_of_a_stdin_password_command_is_refused(self):
        self.seed_disk("secret.vdi")
        w = self.open_window()
        self.select("secret.vdi")

        def encrypt(dlg):
            dlg.encrypt_radio.setChecked(True)
            dlg.password.setText("p")
            dlg.confirm.setText("p")
            dlg._ok()

        self.run_dialog(w.encrypt_selected, encrypt)
        self.assertTrue(self.settled())

        recorded = [e["command"] for e in load_history()]
        self.assertTrue(any("encryptmedium" in c for c in recorded))
        blocked = next(c for c in recorded if "encryptmedium" in c)
        self.assertIn("never stored", vboxfront.rerun_blocker(blocked))


class LibraryTest(E2ECase):
    def test_an_added_image_survives_a_window_restart(self):
        # Modern VirtualBox forgets a standalone registration when VBoxSVC
        # exits; the app's own library is what keeps the row on screen.
        path = os.path.join(self.images, "standalone.vdi")
        with open(path, "wb") as handle:
            handle.truncate(32 * 1024 * 1024)

        w = self.open_window()
        with mock.patch.object(QFileDialog, "getOpenFileName",
                               return_value=(path, "")):
            w.add_media_from_file()
        self.assertTrue(self.settled())
        self.assertIn("standalone.vdi", self.rows())
        self.assertIn(path, load_library("disk"))

        # A fresh registry is exactly what a VBoxSVC restart looks like.
        self.write_state(media=[], vms=[self.vm("alpha", VM_UUID)])
        with mock.patch.object(QMessageBox, "question", return_value=YES):
            self.window.close()
        self.window.deleteLater()
        self.window = None
        QApplication.processEvents()

        self.open_window()
        self.assertIn("standalone.vdi", self.rows(),
                      "the library did not re-open the image")

    def test_removing_a_medium_also_prunes_its_library_entry(self):
        path = os.path.join(self.images, "temp.vdi")
        with open(path, "wb") as handle:
            handle.truncate(16 * 1024 * 1024)
        w = self.open_window()
        with mock.patch.object(QFileDialog, "getOpenFileName",
                               return_value=(path, "")):
            w.add_media_from_file()
        self.assertTrue(self.settled())
        self.assertIn(path, load_library("disk"))

        self.select("temp.vdi")
        self.run_dialog(w.remove_selected, lambda dlg: dlg._ok())
        self.assertTrue(self.settled())
        self.assertNotIn(path, load_library("disk"))

    def test_an_orphan_scan_finds_and_adopts_an_unregistered_image(self):
        stray = os.path.join(self.images, "stray.vdi")
        with open(stray, "wb") as handle:
            handle.truncate(8 * 1024 * 1024)
        w = self.open_window()
        self.assertEqual(self.rows(), {})

        def prepare(dlg):
            dlg.folders.clear()
            dlg.folders.addItem(self.images)
            dlg._scan()
            self.assertEqual(dlg.results.count(), 1)
            dlg._ok()

        self.run_dialog(w.open_orphan_scan, prepare)
        self.assertTrue(self.settled())

        self.assertIn(stray, load_library("disk"))
        self.assertIn("stray.vdi", self.rows())

    def test_the_scan_does_not_offer_back_what_was_just_deleted(self):
        self.seed_disk("deleted.vdi")
        w = self.open_window()
        self.select("deleted.vdi")
        self.delete_the_file(w)
        self.assertTrue(os.path.exists(os.path.join(self.trash_dir(), "deleted.vdi")))

        def prepare(dlg):
            dlg.folders.clear()
            dlg.folders.addItem(self.images)
            dlg._scan()
            # The trash is under the folder being scanned; offering to adopt
            # an image the user has just deleted is the opposite of the point.
            self.assertEqual(dlg.results.count(), 0)

        # Closed rather than accepted: with nothing found there is nothing to
        # check, and OK with nothing checked is a modal that says so.
        self.run_dialog(w.open_orphan_scan, prepare, accept=False)
        self.assertTrue(self.settled())
        self.assertEqual(load_library("disk"), [])


class ImportRawTest(E2ECase):
    def test_a_raw_image_is_converted_and_lands_in_the_library(self):
        raw = os.path.join(self.dir, "disk.raw")
        with open(raw, "wb") as handle:
            handle.write(b"\0" * 8 * 1024 * 1024)
        target = os.path.join(self.images, "imported.vdi")
        w = self.open_window()

        with mock.patch.object(QInputDialog, "getItem", return_value=("VDI", True)), \
             mock.patch.object(QFileDialog, "getSaveFileName",
                               return_value=(target, "")):
            w.convert_from_raw(raw)
        self.assertTrue(self.settled())

        self.assertTrue(os.path.isfile(target))
        self.assertIn(target, load_library("disk"))
        self.assertIn("imported.vdi", self.rows())


class HealthAndResolveTest(E2ECase):
    def test_a_file_that_vanished_is_reported_and_can_be_re_pointed(self):
        medium = self.seed_disk("wander.vdi")
        w = self.open_window()
        self.assertIn("wander.vdi", self.rows())

        # Move it behind the app's back: the registry still says it is fine
        # because VBoxSVC caches State until something opens the medium.
        moved = os.path.join(self.dir, "elsewhere.vdi")
        os.replace(medium["location"], moved)

        findings = vboxfront.health_findings({"disk": w.panes["disk"].records,
                                              "dvd": [], "floppy": []})
        self.assertTrue(any("backing file is missing" in f.problem for f in findings))

        self.select("wander.vdi")

        def prepare(dlg):
            dlg.relocate_radio.setChecked(True)
            dlg.path_edit.setText(moved)
            dlg._ok()

        self.run_dialog(w.resolve_selected, prepare)
        self.assertTrue(self.settled())

        self.assertEqual(self.read_state()["media"][0]["location"], moved)
        self.assertIn("elsewhere.vdi", self.rows())

    def test_a_dry_run_queues_one_read_only_check_per_vdi(self):
        self.seed_disk("check1.vdi")
        self.seed_disk("check2.vdi")
        w = self.open_window()

        def prepare(dlg):
            with mock.patch.object(QMessageBox, "question", return_value=YES):
                dlg._start_dry_run()

        self.run_dialog(w.open_health, prepare)
        self.assertTrue(self.settled())

        repairs = [c for c in self.calls() if "repairhd" in c]
        self.assertEqual(len(repairs), 2)
        self.assertTrue(all("-dry-run" in c for c in repairs),
                        "a health check must never write")
        self.assertIn("writes nothing", w.runner.output.toPlainText())

    def test_stamping_a_registered_image_is_visible_as_damage_afterwards(self):
        medium = self.seed_disk("copy.vdi")
        w = self.open_window()

        def prepare(dlg):
            dlg.path_edit.setText(medium["location"])
            dlg.new_radio.setChecked(True)
            with mock.patch.object(QMessageBox, "warning", return_value=YES):
                dlg._ok()

        self.run_dialog(w.open_uuid_tools, prepare)
        self.assertTrue(self.settled())

        row = self.rows()["copy.vdi"]
        self.assertEqual(row.text(COL_STATE), "inaccessible")
        findings = vboxfront.health_findings({"disk": w.panes["disk"].records,
                                              "dvd": [], "floppy": []})
        self.assertTrue(any(f.level == "error" for f in findings))


class ContentsTest(E2ECase):
    def test_a_byte_range_is_read_straight_out_of_the_image(self):
        medium = self.seed_disk("payload.vdi", capacity_mb=4)
        with open(medium["location"], "r+b") as handle:
            handle.seek(2048)
            handle.write(b"VBOXFRONT-E2E")
        out = os.path.join(self.dir, "range.bin")
        w = self.open_window()
        self.select("payload.vdi")

        def prepare(dlg):
            dlg.offset_edit.setText("0x800")
            dlg.size_edit.setText("16")
            dlg.save_radio.setChecked(True)
            dlg.path_edit.setText(out)
            dlg._ok()

        self.run_dialog(w.show_contents_selected, prepare)
        self.assertTrue(self.settled())

        with open(out, "rb") as handle:
            self.assertTrue(handle.read().startswith(b"VBOXFRONT-E2E"))

    def test_a_hex_dump_lands_in_the_output_panel(self):
        self.seed_disk("hex.vdi", capacity_mb=4)
        w = self.open_window()
        self.select("hex.vdi")

        def prepare(dlg):
            dlg.size_edit.setText("32")
            dlg.hex_radio.setChecked(True)
            dlg._ok()

        self.run_dialog(w.show_contents_selected, prepare)
        self.assertTrue(self.settled())
        self.assertIn("00000000:", w.runner.output.toPlainText())


class PropertiesTest(E2ECase):
    def test_a_type_and_description_change_reaches_the_registry(self):
        self.seed_disk("props.vdi")
        w = self.open_window()
        self.select("props.vdi")

        def prepare(dlg):
            dlg.type_combo.setCurrentText("immutable")
            dlg.desc_edit.setPlainText("built by the e2e suite")
            dlg._ok()

        self.run_dialog(w.edit_properties_selected, prepare)
        self.assertTrue(self.settled())

        medium = self.read_state()["media"][0]
        self.assertEqual(medium["type"], "immutable")
        self.assertEqual(medium["description"], "built by the e2e suite")

    def test_a_multiline_description_still_parses_back_into_one_medium(self):
        """Regression: continuations print flush left, blank lines included.

        A description with a blank line followed by a "UUID:" line used to
        drop the medium from the listing and put a phantom in its place.
        """
        self.seed_disk("notes.vdi")
        w = self.open_window()
        self.select("notes.vdi")
        text = "cloned from\n\nUUID:           not-a-real-record\nand resized"

        def prepare(dlg):
            dlg.desc_edit.setPlainText(text)
            dlg._ok()

        self.run_dialog(w.edit_properties_selected, prepare)
        self.assertTrue(self.settled())

        records = w.panes["disk"].records
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].uuid, self.read_state()["media"][0]["uuid"])
        self.assertNotIn("\n", records[0].uuid)
        self.assertIn("notes.vdi", self.rows())

    def test_a_format_property_is_set_and_deleted(self):
        self.seed_disk("fmt.vdi")
        w = self.open_window()
        self.select("fmt.vdi")

        def set_it(dlg):
            dlg._edits["AllocationBlockSize"].setText("2097152")

        self.run_dialog(w.edit_medium_properties, set_it)
        self.assertTrue(self.settled())
        self.assertEqual(
            self.read_state()["media"][0]["properties"]["AllocationBlockSize"],
            "2097152",
        )

        self.select("fmt.vdi")
        self.run_dialog(w.edit_medium_properties,
                        lambda dlg: dlg._edits["AllocationBlockSize"].setText(""))
        self.assertTrue(self.settled())
        self.assertNotIn("AllocationBlockSize",
                         self.read_state()["media"][0]["properties"])


class MoveTest(E2ECase):
    def test_moving_a_disk_relocates_the_file_and_the_library_entry(self):
        path = os.path.join(self.images, "mover.vdi")
        with open(path, "wb") as handle:
            handle.truncate(16 * 1024 * 1024)
        w = self.open_window()
        with mock.patch.object(QFileDialog, "getOpenFileName",
                               return_value=(path, "")):
            w.add_media_from_file()
        self.assertTrue(self.settled())

        destination = os.path.join(self.dir, "moved.vdi")
        self.select("mover.vdi")
        with mock.patch.object(QFileDialog, "getSaveFileName",
                               return_value=(destination, "")):
            w.move_selected()
        self.assertTrue(self.settled())

        self.assertTrue(os.path.isfile(destination))
        self.assertFalse(os.path.exists(path))
        self.assertIn(destination, load_library("disk"))
        self.assertNotIn(path, load_library("disk"))


class QueueAndHistoryTest(E2ECase):
    def test_a_queue_stops_at_the_first_failure(self):
        self.seed_disk("ok.vdi", capacity_mb=16)
        # A medium whose file is gone: compacting it fails in the fake the way
        # it does for real, and everything queued behind it must be dropped.
        broken = self.seed_disk("broken.vdi", capacity_mb=16)
        os.remove(broken["location"])
        self.seed_disk("never.vdi", capacity_mb=16)
        w = self.open_window()
        self.select_many(["broken.vdi", "never.vdi"])

        with mock.patch.object(QMessageBox, "question", return_value=YES):
            w.compact_selected()
        self.assertTrue(self.settled())

        output = w.runner.output.toPlainText()
        self.assertIn("[queue] aborted", output)
        self.assertNotIn("never.vdi", [c.split()[-2] for c in self.calls()
                                       if "--compact" in c])

    def test_every_mutating_command_is_recorded_with_its_exit_code(self):
        self.seed_disk("hist.vdi")
        w = self.open_window()
        self.select("hist.vdi")

        def prepare(dlg):
            dlg.spin.setValue(256)
            with mock.patch.object(QMessageBox, "warning", return_value=YES):
                dlg._ok()

        self.run_dialog(w.resize_selected, prepare)
        self.assertTrue(self.settled())

        entries = load_history()
        self.assertTrue(entries)
        last = entries[-1]
        self.assertIn("--resize", last["command"])
        self.assertEqual(last["exit"], 0)
        # Refresh traffic must not bury the history under `list` noise.
        self.assertFalse(any("list -l hdds" in e["command"] for e in entries))


class ExportTest(E2ECase):
    def test_a_csv_export_carries_the_real_registry(self):
        self.seed_disk("export1.vdi", capacity_mb=64)
        self.seed_disk("export2.vdi", capacity_mb=128)
        w = self.open_window()
        out = os.path.join(self.dir, "listing.csv")

        with mock.patch.object(QFileDialog, "getSaveFileName",
                               return_value=(out, "CSV (*.csv)")):
            w.export_listing()

        import csv
        with open(out, encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(sorted(r["name"] for r in rows),
                         ["export1.vdi", "export2.vdi"])
        self.assertEqual(
            {r["name"]: r["capacity_mb"] for r in rows},
            {"export1.vdi": "64", "export2.vdi": "128"},
        )

    def test_a_json_name_under_the_csv_filter_still_writes_json(self):
        self.seed_disk("one.vdi")
        w = self.open_window()
        out = os.path.join(self.dir, "listing.json")
        with mock.patch.object(QFileDialog, "getSaveFileName",
                               return_value=(out, "CSV (*.csv)")):
            w.export_listing()
        with open(out, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)[0]["name"], "one.vdi")


class FloppyAndFatTest(E2ECase):
    def test_a_formatted_floppy_is_created_on_its_own_tab(self):
        w = self.open_window()
        target = os.path.join(self.images, "boot.img")

        def prepare(dlg):
            dlg.path_edit.setText(target)
            dlg.spin.setValue(2)
            dlg.format_check.setChecked(True)
            dlg._ok()

        self.run_dialog(w.create_floppy, prepare)
        self.assertTrue(self.settled())

        self.assertTrue(os.path.isfile(target))
        self.assertIn("boot.img", self.rows("floppy"))
        self.assertEqual(self.read_state()["media"][0]["kind"], "floppy")

    def test_formatting_a_disk_as_fat_goes_through_mediumio(self):
        self.seed_disk("fat.vdi")
        w = self.open_window()
        self.select("fat.vdi")
        with mock.patch.object(QMessageBox, "warning", return_value=YES):
            w.formatfat_selected()
        self.assertTrue(self.settled())
        self.assertEqual(self.read_state()["media"][0]["properties"]["Formatted"],
                         "FAT")


class StartupTest(E2ECase):
    def test_a_misconfigured_vboxmanage_is_reported_once_at_startup(self):
        get_settings().setValue("vboxmanage_path", os.path.join(self.dir, "nope"))
        with mock.patch.object(QMessageBox, "warning") as warn:
            self.window = MainWindow()
            QApplication.processEvents()
        warn.assert_called_once()
        self.assertIn("not an executable file", warn.call_args[0][2])
        get_settings().setValue("vboxmanage_path", self.fake)

    def test_the_version_flag_prints_and_exits(self):
        import subprocess
        out = subprocess.run(
            [sys.executable, os.path.join(ROOT, "vboxfront.py"), "--version"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(),
                         f"vboxfront {vboxfront.APP_VERSION}")


class CompressedImportTest(E2ECase):
    """`convertfromraw stdin` — a compressed image, never unpacked to disk."""

    def _archive(self, name="disk.img.gz", payload=b"\xa5" * (3 << 20)):
        import gzip
        raw = os.path.join(self.dir, name)
        with gzip.open(raw, "wb") as handle:
            handle.write(payload)
        return raw, len(payload)

    def test_an_archive_is_streamed_straight_into_a_managed_image(self):
        archive, size = self._archive()
        target = os.path.join(self.images, "imported.vdi")
        w = self.open_window()

        def prepare(dlg):
            # The gzip trailer carries the size, so nothing has to be measured.
            self.assertEqual(dlg.size_edit.text(), str(size))
            dlg.path_edit.setText(target)
            dlg._ok()

        with mock.patch.object(QFileDialog, "getOpenFileName",
                               return_value=(archive, "")):
            self.run_dialog(w.import_compressed, prepare)
        self.assertTrue(self.settled())

        self.assertTrue(os.path.isfile(target), "no image was written")
        self.assertEqual(os.path.getsize(target), size,
                         "the whole stream did not arrive")
        with open(target, "rb") as handle:
            # Past the header the fake stamps its UUID into, as a real VDI
            # header occupies the start of the file too.
            handle.seek(1 << 20)
            self.assertEqual(handle.read(16), b"\xa5" * 16)
        # No unpacked copy was ever written beside the archive.
        self.assertEqual(
            sorted(f for f in os.listdir(self.dir) if f.startswith("disk.img")),
            ["disk.img.gz"],
        )
        self.assertIn(target, load_library("disk"))
        self.assertIn("imported.vdi", self.rows())

    def test_a_bzip2_archive_is_measured_then_imported(self):
        import bz2
        payload = b"\x5a" * (2 << 20)
        archive = os.path.join(self.dir, "disk.img.bz2")
        with bz2.open(archive, "wb") as handle:
            handle.write(payload)
        target = os.path.join(self.images, "measured.vdi")
        w = self.open_window()

        def prepare(dlg):
            # bzip2 records no length at all, so the field starts empty.
            self.assertEqual(dlg.size_edit.text(), "")
            dlg._measure()
            self.assertEqual(dlg.size_edit.text(), str(len(payload)))
            dlg.path_edit.setText(target)
            dlg._ok()

        with mock.patch.object(QFileDialog, "getOpenFileName",
                               return_value=(archive, "")):
            self.run_dialog(w.import_compressed, prepare)
        self.assertTrue(self.settled())
        self.assertEqual(os.path.getsize(target), len(payload))

    def test_a_wrong_byte_count_fails_loudly_and_leaves_nothing(self):
        archive, size = self._archive()
        target = os.path.join(self.images, "short.vdi")
        w = self.open_window()

        def prepare(dlg):
            dlg.size_edit.setText(str(size * 2))  # more than the stream holds
            dlg.path_edit.setText(target)
            dlg._ok()

        with mock.patch.object(QFileDialog, "getOpenFileName",
                               return_value=(archive, "")):
            self.run_dialog(w.import_compressed, prepare)
        self.assertTrue(self.settled())
        self.assertIn("[exit 1]", w.runner.output.toPlainText())
        self.assertFalse(os.path.exists(target), "a truncated image was kept")


class ExportRawTest(E2ECase):
    def test_a_disk_exports_to_a_flat_image_that_stays_unregistered(self):
        medium = self.seed_disk("source.vdi", capacity_mb=8)
        target = os.path.join(self.dir, "flat.img")
        w = self.open_window()
        self.select("source.vdi")

        with mock.patch.object(QFileDialog, "getSaveFileName",
                               return_value=(target, "")), \
             mock.patch.object(QMessageBox, "question", return_value=YES), \
             mock.patch.object(QMessageBox, "warning", return_value=YES):
            w.export_raw()
        self.assertTrue(self.settled())

        self.assertTrue(os.path.isfile(target))
        self.assertEqual(os.path.getsize(target), 8 * 1024 * 1024,
                         "a raw image is flat — its whole capacity")
        registered = [m["location"] for m in self.read_state()["media"]]
        self.assertNotIn(target, registered,
                         "RAW cannot be a managed medium and must not register")
        self.assertIn(medium["location"], registered)


class HealthCleanupTest(E2ECase):
    def test_entries_whose_file_is_gone_are_unregistered_in_one_pass(self):
        alive = self.seed_disk("alive.vdi")
        gone_one = self.seed_disk("gone1.vdi")
        gone_two = self.seed_disk("gone2.vdi")
        w = self.open_window()
        for medium in (gone_one, gone_two):
            os.remove(medium["location"])
        w.refresh_media()
        self.assertTrue(self.settled())

        def prepare(dlg):
            with mock.patch.object(QMessageBox, "question", return_value=YES):
                dlg._start_cleanup()

        self.run_dialog(w.open_health, prepare)
        self.assertTrue(self.settled())

        left = [m["location"] for m in self.read_state()["media"]]
        self.assertEqual(left, [alive["location"]])
        closes = [c for c in self.calls() if c.startswith("closemedium")]
        self.assertEqual(len(closes), 2)
        self.assertTrue(all("--delete" not in c for c in closes),
                        "there is no file left to delete")

    def test_a_registry_snapshot_is_written_before_the_cleanup(self):
        medium = self.seed_disk("gone.vdi")
        w = self.open_window()
        os.remove(medium["location"])
        w.refresh_media()
        self.assertTrue(self.settled())

        def prepare(dlg):
            with mock.patch.object(QMessageBox, "question", return_value=YES):
                dlg._start_cleanup()

        self.run_dialog(w.open_health, prepare)
        self.assertTrue(self.settled())

        written = os.listdir(vboxfront.snapshot_dir())
        self.assertTrue(any(f.endswith("-health-cleanup.json") for f in written))


class DiscardScanTest(E2ECase):
    def test_a_disk_attached_without_discard_is_reported(self):
        medium = self.seed_disk("guest.vdi")
        state = self.read_state()
        state["vms"][0]["attachments"] = [{
            "controller": "SATA", "port": 0, "device": 0,
            "uuid": medium["uuid"], "location": medium["location"],
        }]
        self.write_state(**state)
        w = self.open_window()

        seen = {}

        def prepare(dlg):
            seen["findings"] = [
                dlg.list.item(i).text() for i in range(dlg.list.count())
            ]
            dlg.reject()

        with mock.patch.object(QDialog, "exec",
                               lambda dlg: (prepare(dlg), 0)[1]):
            w.scan_attach_flags()
            self.assertTrue(wait_until(lambda: "findings" in seen, 20))
        self.assertTrue(
            any("without --discard" in line for line in seen["findings"]),
            seen["findings"],
        )


class MultiAttachTest(E2ECase):
    def test_a_selection_fills_consecutive_free_slots(self):
        for name in ("one.vdi", "two.vdi", "three.vdi"):
            self.seed_disk(name, capacity_mb=16)
        w = self.open_window()
        self.select_many(["one.vdi", "three.vdi", "two.vdi"])

        def prepare(dlg):
            self.assertTrue(wait_until(lambda: dlg.ctl_combo.count() > 0))
            dlg._ok()

        self.run_dialog(w.attach_selected, prepare)
        self.assertTrue(self.settled())

        attachments = self.read_state()["vms"][0]["attachments"]
        self.assertEqual(len(attachments), 3)
        self.assertEqual(sorted(a["port"] for a in attachments), [0, 1, 2])
        self.assertEqual(len({a["uuid"] for a in attachments}), 3)

    def test_an_occupied_slot_is_stepped_over_not_replaced(self):
        existing = self.seed_disk("resident.vdi", capacity_mb=16)
        self.seed_disk("new1.vdi", capacity_mb=16)
        self.seed_disk("new2.vdi", capacity_mb=16)
        state = self.read_state()
        state["vms"][0]["attachments"] = [{
            "controller": "SATA", "port": 0, "device": 0,
            "uuid": existing["uuid"], "location": existing["location"],
        }]
        self.write_state(**state)
        w = self.open_window()
        self.select_many(["new1.vdi", "new2.vdi"])

        def prepare(dlg):
            self.assertTrue(wait_until(lambda: dlg.ctl_combo.count() > 0))
            dlg._ok()

        self.run_dialog(w.attach_selected, prepare)
        self.assertTrue(self.settled())

        attachments = self.read_state()["vms"][0]["attachments"]
        self.assertEqual(len(attachments), 3, "the resident disk was detached")
        self.assertIn(existing["uuid"], {a["uuid"] for a in attachments})


class MultiPropertiesTest(E2ECase):
    def test_one_type_change_reaches_every_selected_medium(self):
        for name in ("p1.vdi", "p2.vdi"):
            self.seed_disk(name)
        w = self.open_window()
        self.select_many(["p1.vdi", "p2.vdi"])

        def prepare(dlg):
            dlg.type_combo.setCurrentText("writethrough")
            dlg._ok()

        self.run_dialog(w.edit_properties_selected, prepare)
        self.assertTrue(self.settled())

        types = {m["type"] for m in self.read_state()["media"]}
        self.assertEqual(types, {"writethrough"})


class OrphanCollisionTest(E2ECase):
    def test_a_copied_image_is_named_as_a_uuid_collision(self):
        original = self.seed_disk("original.vdi", capacity_mb=4)
        copy = os.path.join(self.images, "copy.vdi")
        shutil.copyfile(original["location"], copy)
        w = self.open_window()

        seen = {}

        def prepare(dlg):
            dlg.folders.clear()
            dlg.folders.addItem(self.images)
            dlg._scan()
            self.assertEqual(dlg.results.count(), 1)
            dlg._explain()
            self.assertTrue(wait_until(lambda: dlg.explain_btn.isEnabled(), 20))
            item = dlg.results.item(0)
            seen["text"] = item.text()
            seen["checked"] = item.checkState() == Qt.CheckState.Checked
            dlg.reject()

        self.run_dialog(w.open_orphan_scan, prepare, accept=False)
        self.assertTrue(self.settled())

        self.assertIn("already", seen["text"])
        self.assertIn(original["uuid"], seen["text"])
        self.assertFalse(seen["checked"],
                         "adopting it would only fail on every refresh")


class PlanModeTest(E2ECase):
    def tearDown(self):
        vboxfront.set_plan_mode(False)
        super().tearDown()

    def test_a_batch_shows_its_commands_and_runs_none_of_them(self):
        for name in ("a.vdi", "b.vdi"):
            self.seed_disk(name)
        w = self.open_window()
        vboxfront.set_plan_mode(True)
        self.select_many(["a.vdi", "b.vdi"])
        before = len(self.calls())

        seen = {}

        def prepare(dlg):
            seen["script"] = vboxfront.plan_script(dlg._commands)
            dlg.reject()

        with mock.patch.object(QDialog, "exec",
                               lambda dlg: (prepare(dlg), 0)[1]), \
             mock.patch.object(QMessageBox, "question", return_value=YES):
            w.compact_selected()
        self.assertTrue(self.settled())

        self.assertEqual(seen["script"].count("--compact"), 2)
        self.assertEqual(
            len([c for c in self.calls() if "--compact" in c]), 0,
            "plan mode ran a command",
        )
        self.assertEqual(len(self.calls()), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
