"""Settings transfer, per-medium history, TRIM fix, chain repair, bundles."""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from PyQt6.QtWidgets import QApplication

from tests.test_locate import OTHER, OWN, PARENT, descriptor, write_vdi
from tests.test_window import wait_until
from vboxfront_app import bundles, cli, settings_io
from vboxfront_app.chains import (
    STATUS_MISSING,
    STATUS_OK,
    STATUS_REGISTER,
    STATUS_REPOINT,
    chain_fixes,
    chain_is_repairable,
    relink_blocker,
    trace_chain,
)
from vboxfront_app.domain import discard_fix_plan, history_for
from vboxfront_app.library_views import SavedFilterStore
from vboxfront_app.locate import parent_first
from vboxfront_app.models import MediumRecord
from vboxfront_app.persistence import (
    append_history,
    clear_history,
    get_settings,
    load_history,
    load_library,
    save_library,
)
from vboxfront_app.tools_dialogs import (
    BackupDialog,
    ChainRepairDialog,
    DiscardFixDialog,
    RestoreDialog,
    SettingsTransferDialog,
)

GRAND = "0b1c2d3e-4f50-4617-8899-aabbccddeeff"


def rec(uuid, location, parent="", **extra):
    base = dict(uuid=uuid, location=location, fmt="VDI", capacity="64 MBytes",
                size_on_disk="2 MBytes", state="created",
                parent_uuid=parent or "base")
    base.update(extra)
    return MediumRecord(**base)


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def path(self, *parts):
        return os.path.join(self.dir, *parts)


# -- settings --------------------------------------------------------------

class SettingsIoTest(TempDirCase):
    def setUp(self):
        super().setUp()
        settings = get_settings()
        for key in ("image_folder", "plan_mode", "vboxmanage_path", "folders",
                    SavedFilterStore.KEY):
            settings.remove(key)
        for kind in ("disk", "dvd", "floppy"):
            save_library(kind, [])
        clear_history()
        self.addCleanup(lambda: [save_library(k, []) for k in ("disk", "dvd", "floppy")])
        self.addCleanup(clear_history)
        self.addCleanup(lambda: get_settings().remove(SavedFilterStore.KEY))
        self.addCleanup(lambda: get_settings().remove("plan_mode"))
        self.addCleanup(lambda: get_settings().remove("image_folder"))

    def test_round_trip_merges_by_default_and_replaces_on_request(self):
        get_settings().setValue("image_folder", self.dir)
        get_settings().setValue("plan_mode", True)
        save_library("disk", [self.path("a.vdi")])
        SavedFilterStore().save("big", "size:>10G")
        get_settings().setValue("folders/create", self.dir)
        append_history("VBoxManage list hdds", 0, "2026-11-01 10:00:00")
        out = self.path("s.json")
        data = settings_io.write_settings_file(out)
        self.assertNotIn("history", data, "history is opt-in")
        self.assertEqual(data["preferences"]["plan_mode"], True)

        # A different machine: its own library entry and filter survive a merge.
        save_library("disk", [self.path("local.vdi")])
        get_settings().remove(SavedFilterStore.KEY)
        SavedFilterStore().save("mine", "fmt:vmdk")
        get_settings().setValue("plan_mode", False)
        report = settings_io.import_settings(settings_io.read_settings_file(out))
        self.assertEqual(load_library("disk"), [self.path("local.vdi"), self.path("a.vdi")])
        self.assertEqual(SavedFilterStore().load(), {"mine": "fmt:vmdk", "big": "size:>10G"})
        self.assertTrue(get_settings().value("plan_mode", False, bool))
        self.assertIn("do not exist on this machine", report.text())

        settings_io.import_settings(settings_io.read_settings_file(out), replace=True)
        self.assertEqual(load_library("disk"), [self.path("a.vdi")])
        self.assertEqual(SavedFilterStore().load(), {"big": "size:>10G"})

    def test_an_unusable_vboxmanage_path_is_not_imported(self):
        data = {"format": settings_io.FORMAT, "version": 1,
                "preferences": {"vboxmanage_path": "/no/such/VBoxManage"}}
        report = settings_io.import_settings(data)
        self.assertEqual(get_settings().value("vboxmanage_path", "", str), "")
        self.assertIn("Kept this machine's VBoxManage", report.text())

    def test_history_import_dedupes_and_keeps_time_order(self):
        append_history("VBoxManage a", 0, "2026-11-02 10:00:00")
        data = settings_io.export_settings([settings_io.SECTION_HISTORY])
        data["history"].append({"command": "VBoxManage b", "exit": 1,
                                "when": "2026-11-01 10:00:00"})
        settings_io.import_settings(data)
        settings_io.import_settings(data)
        self.assertEqual([e["command"] for e in load_history()],
                         ["VBoxManage b", "VBoxManage a"])

    def test_bad_files_are_refused_with_a_reason(self):
        cases = {"x.json": "not json", "y.json": json.dumps({"format": "other"}),
                 "z.json": json.dumps({"format": settings_io.FORMAT, "version": 99})}
        for name, text in cases.items():
            with open(self.path(name), "w") as handle:
                handle.write(text)
            with self.assertRaises(settings_io.SettingsFileError):
                settings_io.read_settings_file(self.path(name))
        with self.assertRaises(settings_io.SettingsFileError):
            settings_io.read_settings_file(self.path("absent.json"))

    def test_import_sections_can_be_limited(self):
        data = settings_io.export_settings(settings_io.SECTIONS)
        data["library"] = {"disk": [self.path("only.vdi")]}
        data["preferences"] = {"plan_mode": True}
        settings_io.import_settings(data, ["library"])
        self.assertEqual(load_library("disk"), [self.path("only.vdi")])
        self.assertFalse(get_settings().value("plan_mode", False, bool))


# -- history ---------------------------------------------------------------

class HistoryForTest(TempDirCase):
    def test_matches_uuid_path_spellings_and_option_values(self):
        disk = self.path("d.vdi")
        with open(disk, "w"):
            pass
        entries = [
            {"command": f"VBoxManage modifymedium disk {OWN.upper()} --compact"},
            {"command": f"VBoxManage storageattach vm --medium {{{OWN}}}"},
            {"command": f"VBoxManage showmediuminfo disk '{self.dir}/./d.vdi'"},
            {"command": f"VBoxManage mediumio --disk={disk} cat"},
            {"command": f"VBoxManage modifymedium disk {OTHER} --compact"},
            {"command": "VBoxManage list hdds"},
            {"command": "VBoxManage broken 'quote"},
        ]
        picked = history_for(entries, rec(OWN, disk))
        self.assertEqual(len(picked), 4)
        self.assertEqual(history_for(entries, rec(OTHER, "")), [entries[4]])

    def test_earlier_locations_count(self):
        entries = [{"command": "VBoxManage modifymedium disk x --move /new/d.vdi"},
                   {"command": "VBoxManage showmediuminfo disk /old/d.vdi"}]
        self.assertEqual(history_for(entries, rec(OWN, "/new/d.vdi"), ["/old/d.vdi"]),
                         entries)


# -- TRIM ------------------------------------------------------------------

def vm_info(name, state="poweroff", discard="off", image=OWN, dvd=OTHER):
    return {
        "name": name, "VMState": state,
        "storagecontrollername0": "SATA", "storagecontrollertype0": "IntelAhci",
        "storagecontrollerportcount0": "2",
        "storagecontrollername1": "IDE", "storagecontrollertype1": "PIIX4",
        "storagecontrollerportcount1": "1",
        "SATA-0-0": "/vms/d.vdi", "SATA-ImageUUID-0-0": image,
        "SATA-discard-0-0": discard, "SATA-1-0": "none",
        "IDE-0-1": "/isos/x.iso", "IDE-ImageUUID-0-1": dvd,
    }


class DiscardFixPlanTest(unittest.TestCase):
    media = {"disk": [rec(OWN, "/vms/d.vdi")], "dvd": [rec(OTHER, "/isos/x.iso")]}

    def test_powered_off_vm_gets_an_in_place_reattach(self):
        commands, skipped = discard_fix_plan(self.media, {"vm-1": vm_info("one")})
        self.assertEqual(commands, [[
            "storageattach", "vm-1", "--storagectl", "SATA", "--port", "0",
            "--device", "0", "--type", "hdd", "--medium", OWN,
            "--discard", "on", "--nonrotational", "on"]])
        self.assertEqual(skipped, [])
        commands, _ = discard_fix_plan(self.media, {"vm-1": vm_info("one")},
                                       nonrotational=False)
        self.assertNotIn("--nonrotational", commands[0])

    def test_running_vm_is_skipped_and_said_why(self):
        commands, skipped = discard_fix_plan(
            self.media, {"vm-2": vm_info("two", state="running")})
        self.assertEqual(commands, [])
        self.assertIn("running", skipped[0])

    def test_already_on_dvds_and_unknown_media_are_left_alone(self):
        infos = {"a": vm_info("a", discard="on"), "b": vm_info("b", image=GRAND)}
        self.assertEqual(discard_fix_plan(self.media, infos), ([], []))


# -- chains ----------------------------------------------------------------

class ParentFirstTest(TempDirCase):
    def test_children_follow_parents_and_others_keep_order(self):
        write_vdi(self.path("base.vdi"), own=PARENT)
        write_vdi(self.path("kid.vdi"), own=OWN, parent=PARENT)
        write_vdi(self.path("grand.vdi"), own=GRAND, parent=OWN)
        paths = [self.path("grand.vdi"), self.path("x.iso"), self.path("kid.vdi"),
                 self.path("base.vdi")]
        self.assertEqual(parent_first(paths), [
            self.path("base.vdi"), self.path("kid.vdi"), self.path("grand.vdi"),
            self.path("x.iso")])

    def test_a_cycle_does_not_hang_or_drop_entries(self):
        write_vdi(self.path("a.vdi"), own=OWN, parent=OTHER)
        write_vdi(self.path("b.vdi"), own=OTHER, parent=OWN)
        result = parent_first([self.path("a.vdi"), self.path("b.vdi")])
        self.assertEqual(sorted(result), [self.path("a.vdi"), self.path("b.vdi")])


class TraceChainTest(TempDirCase):
    def test_moved_parent_is_found_and_planned_as_a_repoint(self):
        os.makedirs(self.path("moved"))
        write_vdi(self.path("moved", "base.vdi"), own=PARENT)
        write_vdi(self.path("kid.vdi"), own=OWN, parent=PARENT)
        disks = [rec(PARENT, self.path("base.vdi")),
                 rec(OWN, self.path("kid.vdi"), parent=PARENT)]
        links = trace_chain(OWN, "", disks, [self.dir], [d.location for d in disks])
        self.assertEqual([(l.uuid, l.status) for l in links],
                         [(PARENT, STATUS_REPOINT), (OWN, STATUS_OK)])
        commands, register = chain_fixes(links)
        self.assertEqual(commands, [["modifymedium", "disk", PARENT, "--setlocation",
                                     self.path("moved", "base.vdi")]])
        self.assertEqual(register, [])

    def test_unregistered_files_are_registered_base_first(self):
        write_vdi(self.path("base.vdi"), own=PARENT)
        write_vdi(self.path("kid.vdi"), own=OWN, parent=PARENT)
        write_vdi(self.path("grand.vdi"), own=GRAND, parent=OWN)
        links = trace_chain(path=self.path("grand.vdi"), folders=[self.dir])
        self.assertEqual([l.status for l in links], [STATUS_REGISTER] * 3)
        self.assertEqual(chain_fixes(links)[1], [self.path("base.vdi"), self.path("kid.vdi"),
                                                 self.path("grand.vdi")])

    def test_a_lost_parent_stops_the_chain_as_missing(self):
        write_vdi(self.path("kid.vdi"), own=OWN, parent=PARENT)
        links = trace_chain(path=self.path("kid.vdi"), folders=[self.dir])
        self.assertEqual([(l.uuid, l.status) for l in links],
                         [(PARENT, STATUS_MISSING), (OWN, STATUS_REGISTER)])
        self.assertFalse(chain_is_repairable(links))

    def test_registered_parent_missing_on_disk_reports_where_it_was(self):
        write_vdi(self.path("kid.vdi"), own=OWN, parent=PARENT)
        disks = [rec(PARENT, "/gone/base.vdi"), rec(OWN, self.path("kid.vdi"), parent=PARENT)]
        links = trace_chain(OWN, "", disks, [self.dir])
        self.assertEqual(links[0].status, STATUS_MISSING)
        self.assertIn("/gone/base.vdi", links[0].note)

    def test_a_parent_loop_is_reported_not_followed(self):
        write_vdi(self.path("a.vdi"), own=OWN, parent=OTHER)
        write_vdi(self.path("b.vdi"), own=OTHER, parent=OWN)
        links = trace_chain(path=self.path("a.vdi"), folders=[self.dir])
        self.assertEqual(len(links), 2)
        self.assertIn("loops", links[0].note)

    def test_a_file_without_a_header(self):
        with open(self.path("x.img"), "wb") as handle:
            handle.write(b"\0" * 4096)
        links = trace_chain(path=self.path("x.img"))
        self.assertEqual(links[0].status, STATUS_MISSING)

    def test_relink_blockers(self):
        write_vdi(self.path("kid.vdi"), own=OWN, parent=PARENT)
        write_vdi(self.path("other.vdi"), own=OTHER)
        write_vdi(self.path("same.vdi"), own=PARENT)
        links = trace_chain(path=self.path("kid.vdi"), folders=[self.path("nowhere")])
        child = links[1]
        self.assertEqual(relink_blocker(child, self.path("other.vdi")), "")
        self.assertIn("already", relink_blocker(child, self.path("same.vdi")))
        self.assertIn("own parent", relink_blocker(child, self.path("kid.vdi")))
        child.record = rec(OWN, self.path("kid.vdi"))
        self.assertIn("registered", relink_blocker(child, self.path("other.vdi")))


# -- bundles ---------------------------------------------------------------

class BundleTest(TempDirCase):
    def setUp(self):
        super().setUp()
        os.makedirs(self.path("vms"))
        os.makedirs(self.path("out"))
        write_vdi(self.path("vms", "base.vdi"), own=PARENT, size=3 * 1024 * 1024)
        with open(self.path("vms", "base.vdi"), "r+b") as handle:
            handle.seek(2 * 1024 * 1024)
            handle.write(b"payload")
        write_vdi(self.path("vms", "kid.vdi"), own=OWN, parent=PARENT)
        self.media = {"disk": [rec(PARENT, self.path("vms", "base.vdi")),
                               rec(OWN, self.path("vms", "kid.vdi"), parent=PARENT,
                                   in_use=[f"vm1 (UUID: {GRAND})"])],
                      "dvd": [], "floppy": []}

    def test_round_trip_keeps_chain_order_bytes_and_holes(self):
        members = bundles.plan_members(self.media, "disk", OWN)
        self.assertEqual([m.record.uuid for m in members], [PARENT, OWN])
        self.assertEqual(bundles.running_users(members, [GRAND.upper()]), ["kid.vdi (vm1)"])
        path = bundles.write_bundle(members, self.path("out"), "b")
        self.assertTrue(path.endswith(".vboxbundle"))
        self.assertEqual(bundles.verify_bundle(path), [])
        copy = os.path.join(path, "media", f"00-{PARENT[:8]}", "base.vdi")
        with open(copy, "rb") as a, open(self.path("vms", "base.vdi"), "rb") as b:
            self.assertEqual(a.read(), b.read())
        self.assertLess(os.stat(copy).st_blocks * 512, 3 * 1024 * 1024,
                        "zero runs are written as holes")

        steps, blockers = bundles.plan_restore(path, self.path("restored"),
                                               {PARENT: self.path("vms", "base.vdi")})
        self.assertEqual(blockers, [])
        self.assertTrue(steps[0].skip, "a base still registered is not restored again")
        bundles.restore_files(path, steps)
        self.assertEqual(os.listdir(self.path("restored")), ["kid.vdi"])

    def test_vmdk_extents_travel_with_their_descriptor(self):
        with open(self.path("vms", "f.vmdk"), "w") as handle:
            handle.write(descriptor(GRAND, extent="f-flat.vmdk"))
        with open(self.path("vms", "f-flat.vmdk"), "wb") as handle:
            handle.write(b"x" * 1000)
        self.media["disk"].append(rec(GRAND, self.path("vms", "f.vmdk"), fmt="VMDK"))
        members = bundles.plan_members(self.media, "disk", GRAND)
        self.assertEqual([os.path.basename(f) for f in members[0].files],
                         ["f.vmdk", "f-flat.vmdk"])
        os.remove(self.path("vms", "f-flat.vmdk"))
        with self.assertRaises(bundles.BundleError):
            bundles.plan_members(self.media, "disk", GRAND)

    def test_an_incomplete_chain_cannot_be_bundled(self):
        self.media["disk"] = self.media["disk"][1:]
        with self.assertRaises(bundles.BundleError):
            bundles.plan_members(self.media, "disk", OWN)

    def test_damage_is_detected(self):
        path = bundles.write_bundle(bundles.plan_members(self.media, "disk", OWN),
                                    self.path("out"), "b")
        damaged = os.path.join(path, "media", f"01-{OWN[:8]}", "kid.vdi")
        with open(damaged, "r+b") as handle:
            handle.seek(100)
            handle.write(b"!")
        self.assertEqual(len(bundles.verify_bundle(path)), 1)
        steps, _ = bundles.plan_restore(path, self.path("restored"), {})
        with self.assertRaises(bundles.BundleError):
            bundles.restore_files(path, steps)
        self.assertEqual(os.listdir(self.path("restored")), [],
                         "nothing half-restored is left behind")

    def test_cancel_leaves_no_bundle_and_existing_names_are_refused(self):
        members = bundles.plan_members(self.media, "disk", OWN)
        with self.assertRaises(bundles.Cancelled):
            bundles.write_bundle(members, self.path("out"), "c", cancelled=lambda: True)
        self.assertEqual(os.listdir(self.path("out")), [])
        bundles.write_bundle(members, self.path("out"), "c")
        with self.assertRaises(bundles.BundleError):
            bundles.write_bundle(members, self.path("out"), "c")

    def test_restore_blockers_and_unsafe_manifests(self):
        path = bundles.write_bundle(bundles.plan_members(self.media, "disk", OWN),
                                    self.path("out"), "b")
        os.makedirs(self.path("restored"))
        with open(self.path("restored", "kid.vdi"), "w") as handle:
            handle.write("mine")
        _steps, blockers = bundles.plan_restore(path, self.path("restored"),
                                                {OWN: "/gone/kid.vdi"})
        self.assertTrue(any("already exists" in b for b in blockers))
        self.assertTrue(any("file is gone" in b for b in blockers))
        manifest = os.path.join(path, bundles.MANIFEST)
        with open(manifest) as handle:
            data = json.load(handle)
        data["media"][0]["files"][0]["path"] = "../../etc/passwd"
        with open(manifest, "w") as handle:
            json.dump(data, handle)
        with self.assertRaises(bundles.BundleError):
            bundles.read_manifest(path)

    def test_restore_never_removes_a_file_it_did_not_create(self):
        path = bundles.write_bundle(bundles.plan_members(self.media, "disk", OWN),
                                    self.path("out"), "b")
        steps, _ = bundles.plan_restore(path, self.path("restored"), {})
        os.makedirs(self.path("restored"))
        with open(self.path("restored", "kid.vdi"), "w") as handle:
            handle.write("arrived meanwhile")
        with self.assertRaises(bundles.BundleError):
            bundles.restore_files(path, steps)
        with open(self.path("restored", "kid.vdi")) as handle:
            self.assertEqual(handle.read(), "arrived meanwhile")
        self.assertFalse(os.path.exists(self.path("restored", "base.vdi")))


# -- dialogs ---------------------------------------------------------------

class ToolDialogsTest(TempDirCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv[:1])

    def test_settings_dialog_offers_history_unticked(self):
        dlg = SettingsTransferDialog()
        self.assertFalse(dlg.boxes["history"].isChecked())
        self.assertFalse(dlg.replace.isVisible())
        dlg = SettingsTransferDialog({"format": "x", "library": {}, "exported": "t"})
        self.assertEqual(list(dlg.boxes), ["library"])

    def test_discard_dialog_follows_the_ssd_box(self):
        media = {"disk": [rec(OWN, "/vms/d.vdi")]}
        dlg = DiscardFixDialog(media, {"v": vm_info("one"), "w": vm_info("two", "saved")})
        self.assertIn("--nonrotational", dlg.commands[0])
        self.assertEqual(dlg.list.count(), 2)
        dlg.ssd.setChecked(False)
        self.assertNotIn("--nonrotational", dlg.commands[0])

    def test_chain_dialog_traces_and_returns_fixes(self):
        os.makedirs(self.path("moved"))
        write_vdi(self.path("moved", "base.vdi"), own=PARENT)
        write_vdi(self.path("kid.vdi"), own=OWN, parent=PARENT)
        disks = [rec(PARENT, self.path("base.vdi")),
                 rec(OWN, self.path("kid.vdi"), parent=PARENT)]
        dlg = ChainRepairDialog(disks, OWN, known=[d.location for d in disks])
        self.assertEqual(dlg.folder_paths()[0], self.dir)
        self.assertTrue(wait_until(lambda: not dlg.busy()))
        self.assertTrue(dlg.repair_btn.isEnabled())
        self.assertFalse(dlg.relink_btn.isEnabled())
        dlg._repair()
        self.assertEqual(dlg.commands[0][-1], self.path("moved", "base.vdi"))

    def test_chain_dialog_relinks_only_after_confirmation(self):
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        write_vdi(self.path("kid.vdi"), own=OWN, parent=PARENT)
        write_vdi(self.path("copy.vdi"), own=OTHER)
        dlg = ChainRepairDialog([], path=self.path("kid.vdi"), autostart=False)
        dlg.show_links(trace_chain(path=self.path("kid.vdi"), folders=[self.path("x")]))
        self.assertFalse(dlg.repair_btn.isEnabled())
        self.assertTrue(dlg.relink_btn.isEnabled())
        with mock.patch.object(QFileDialog, "getOpenFileName",
                               return_value=(self.path("copy.vdi"), "")), \
             mock.patch.object(QMessageBox, "warning",
                               return_value=QMessageBox.StandardButton.No):
            dlg._choose_parent()
        self.assertIsNone(dlg.relink)
        with mock.patch.object(QFileDialog, "getOpenFileName",
                               return_value=(self.path("copy.vdi"), "")), \
             mock.patch.object(QMessageBox, "warning",
                               return_value=QMessageBox.StandardButton.Yes):
            dlg._choose_parent()
        self.assertEqual(dlg.relink, ["internalcommands", "sethdparentuuid",
                                      self.path("kid.vdi"), OTHER])

    def test_backup_and_restore_dialogs_run_their_copies(self):
        os.makedirs(self.path("vms"))
        write_vdi(self.path("vms", "base.vdi"), own=PARENT)
        media = {"disk": [rec(PARENT, self.path("vms", "base.vdi"))]}
        members = bundles.plan_members(media, "disk", PARENT)
        dlg = BackupDialog(members)
        dlg.dest.setText(self.dir)
        dlg.name.setText("bk")
        dlg._start()
        self.assertTrue(wait_until(lambda: not dlg.busy()))
        self.assertTrue(dlg.bundle_path.endswith("bk.vboxbundle"))

        manifest = bundles.read_manifest(dlg.bundle_path)
        verify = RestoreDialog(dlg.bundle_path, manifest, {}, verify_only=True)
        verify._start()
        self.assertTrue(wait_until(lambda: not verify.busy()))
        self.assertEqual(verify.problems, [])

        restore = RestoreDialog(dlg.bundle_path, manifest, {})
        restore.into.setText(self.path("back"))
        self.assertTrue(restore.start_btn.isEnabled())
        restore._start()
        self.assertTrue(wait_until(lambda: not restore.busy()))
        self.assertEqual(restore.restored_paths(),
                         [("disk", self.path("back", "base.vdi"))])
        restore.into.setText("")
        self.assertFalse(restore.start_btn.isEnabled())


class CliRoutingTest(unittest.TestCase):
    def test_new_commands_leave_the_gui(self):
        for name in ("locate", "chain", "history", "fix-discard", "backup",
                     "restore", "verify-bundle", "settings"):
            self.assertTrue(cli.wants_cli(["vboxfront", name]), name)

    def test_offline_commands_need_no_vboxmanage(self):
        clear_history()
        append_history(f"VBoxManage modifymedium disk {OWN} --compact", 0, "t")
        self.addCleanup(clear_history)
        out = io.StringIO()
        with mock.patch.object(cli, "vboxmanage_problem", return_value="nope"):
            self.assertEqual(cli.main(["history", OWN], out=out), 0)
        self.assertIn("--compact", out.getvalue())


if __name__ == "__main__":
    unittest.main()
