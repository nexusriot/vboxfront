"""Dialog accept-logic tests (offscreen Qt, no display needed)."""

import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMessageBox, QSizePolicy

from tests.test_parsing import HDDBACKENDS
from vboxfront import (
    AttachDialog,
    BatchConvertDialog,
    CommandHistoryDialog,
    HealthDialog,
    MediumPropertyDialog,
    OrphanScanDialog,
    append_history,
    clear_history,
    ContentsDialog,
    ElidingLabel,
    CreateDiffDialog,
    CreateFloppyDialog,
    ResolveDialog,
    UuidToolsDialog,
    ConvertDialog,
    CreateDiskDialog,
    EncryptDialog,
    MediumRecord,
    PropertiesDialog,
    RemoveDialog,
    ResizeDialog,
    SettingsDialog,
    add_library_path,
    get_settings,
    parse_hddbackends,
    load_library,
    save_library,
    vboxmanage_path,
)

VMINFO = """\
storagecontrollername0="SATA Controller"
storagecontrollerportcount0="4"
"SATA Controller-0-0"="/busy.vdi"
"SATA Controller-ImageUUID-0-0"="99999999-9999-9999-9999-999999999999"
"SATA Controller-1-0"="none"
"SATA Controller-2-0"="none"
"SATA Controller-3-0"="none"
"""


def record(**overrides) -> MediumRecord:
    base = dict(
        uuid="7894924d-30ae-41ec-a640-98cdba451f17",
        location="/vms/test.vdi",
        fmt="VDI",
        capacity="64 MBytes",
        size_on_disk="2 MBytes",
        state="created",
        medium_type="normal (base)",
        parent_uuid="base",
        variant="dynamic default",
        encryption="disabled",
    )
    base.update(overrides)
    return MediumRecord(**base)


class DialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv[:1])

    def test_create_disk_defaults_and_args(self):
        dlg = CreateDiskDialog()
        dlg.path_edit.setText("/nonexistent/new.vdi")
        dlg.spin.setValue(2048)
        dlg._ok()
        self.assertEqual(dlg.path, "/nonexistent/new.vdi")
        self.assertEqual(
            dlg.args(),
            ["createmedium", "disk", "--filename", "/nonexistent/new.vdi",
             "--size", "2048", "--format", "VDI"],
        )

    def test_create_disk_vmdk_variants_and_extension(self):
        dlg = CreateDiskDialog()
        dlg.path_edit.setText("/nonexistent/new.vdi")
        dlg.fmt_combo.setCurrentText("VMDK")
        variants = [dlg.variant_combo.itemText(i) for i in range(dlg.variant_combo.count())]
        self.assertIn("Split2G", variants)
        self.assertEqual(dlg.path_edit.text(), "/nonexistent/new.vmdk")
        dlg.variant_combo.setCurrentText("Fixed")
        dlg._ok()
        self.assertEqual(dlg.args()[-2:], ["--variant", "Fixed"])

    def test_create_disk_refuses_existing_path(self):
        dlg = CreateDiskDialog()
        dlg.path_edit.setText(__file__)
        with mock.patch.object(QMessageBox, "warning") as warn:
            dlg._ok()
        warn.assert_called_once()
        self.assertEqual(dlg.result(), 0)

    def test_resize_prefills_current_capacity(self):
        dlg = ResizeDialog(record(capacity="64 MBytes"))
        self.assertEqual(dlg.spin.value(), 64)
        dlg.spin.setValue(128)
        dlg._ok()
        self.assertEqual(dlg.size_mb, 128)

    def test_convert_defaults_to_other_format(self):
        dlg = ConvertDialog(record())
        self.assertNotEqual(dlg.fmt.currentText(), "VDI")
        self.assertEqual(dlg.path_edit.text(), "/vms/test-converted.vmdk")
        dlg.fmt.setCurrentText("VHD")
        self.assertEqual(dlg.path_edit.text(), "/vms/test-converted.vhd")
        dlg._ok()
        self.assertEqual(dlg.target_format, "VHD")
        self.assertEqual(dlg.target_path, "/vms/test-converted.vhd")

    def test_convert_refuses_the_source_as_its_own_target(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "important.vdi")
            with open(src, "w") as f:
                f.write("disk")
            dlg = ConvertDialog(record(location=src))
            dlg.path_edit.setText(os.path.join(d, "sub", "..", "important.vdi"))
            with mock.patch.object(QMessageBox, "warning") as warn:
                dlg._ok()
            warn.assert_called_once()
            self.assertEqual(dlg.result(), 0)
            self.assertTrue(os.path.exists(src), "source disk was deleted")

    def test_convert_defers_overwrite_to_the_caller(self):
        # The dialog must not remove the target: the command it belongs to may
        # still never run (busy runner), and then the file is gone for nothing.
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "old.vdi")
            with open(target, "w") as f:
                f.write("old")
            dlg = ConvertDialog(record())
            dlg.path_edit.setText(target)
            with mock.patch.object(
                QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
            ):
                dlg._ok()
            self.assertTrue(dlg.overwrite)
            self.assertTrue(os.path.exists(target), "target deleted at accept time")

    def test_convert_without_overwrite_sets_no_flag(self):
        dlg = ConvertDialog(record())
        dlg.path_edit.setText("/nonexistent/out.vmdk")
        dlg._ok()
        self.assertFalse(dlg.overwrite)
        self.assertEqual(dlg.target_path, "/nonexistent/out.vmdk")

    def test_attach_warns_before_replacing_an_occupied_slot(self):
        dlg = AttachDialog(
            "disk", record(),
            vms=[("vm1", "24aa0bbd-3d9c-4ba3-a41f-273c0ab57661")],
            running=set(), capture=lambda args, cb: cb(0, VMINFO),
        )
        # Port 0 holds /busy.vdi in the fixture; the dialog must say so.
        dlg.port_spin.setValue(0)
        self.assertIn("/busy.vdi", dlg.slot_status.text())
        with mock.patch.object(
            QMessageBox, "warning", return_value=QMessageBox.StandardButton.No
        ) as warn:
            dlg._ok()
        warn.assert_called_once()
        self.assertEqual(dlg.result(), 0)
        with mock.patch.object(
            QMessageBox, "warning", return_value=QMessageBox.StandardButton.Yes
        ):
            dlg._ok()
        self.assertEqual(dlg.args()[-4:], ["--type", "hdd", "--medium", record().uuid])

    def test_attach_to_a_free_slot_needs_no_confirmation(self):
        dlg = AttachDialog(
            "disk", record(),
            vms=[("vm1", "24aa0bbd-3d9c-4ba3-a41f-273c0ab57661")],
            running=set(), capture=lambda args, cb: cb(0, VMINFO),
        )
        self.assertEqual(dlg.port_spin.value(), 1)
        self.assertEqual(dlg.slot_status.text(), "")
        with mock.patch.object(QMessageBox, "warning") as warn:
            dlg._ok()
        warn.assert_not_called()

    def test_remove_defaults_to_keep_file(self):
        dlg = RemoveDialog(record())
        dlg._ok()
        self.assertFalse(dlg.delete_file)

    def test_remove_delete_needs_confirmation(self):
        dlg = RemoveDialog(record())
        dlg.mode.setCurrentIndex(1)
        with mock.patch.object(
            QMessageBox, "warning", return_value=QMessageBox.StandardButton.Yes
        ):
            dlg._ok()
        self.assertTrue(dlg.delete_file)

    def test_properties_unchanged_yields_no_args(self):
        dlg = PropertiesDialog(record())
        dlg._ok()
        self.assertIsNone(dlg.args("disk"))

    def test_properties_changed(self):
        dlg = PropertiesDialog(record())
        dlg.type_combo.setCurrentText("immutable")
        dlg.desc_edit.setPlainText("scratch disk")
        dlg._ok()
        self.assertEqual(
            dlg.args("disk"),
            ["modifymedium", "disk", record().uuid,
             "--type", "immutable", "--description", "scratch disk"],
        )

    def test_encrypt_flow(self):
        dlg = EncryptDialog(record())
        self.assertTrue(dlg.encrypt_radio.isChecked())
        dlg.password.setText("pw")
        dlg.confirm.setText("pw")
        dlg.pwid.setText("myid")
        dlg._ok()
        args, stdin, secrets = dlg.command_parts()
        self.assertIn("--newpassword", args)
        self.assertEqual(stdin, b"pw\n")
        self.assertEqual(secrets, [], "a single secret needs no file on disk")

    def test_decrypt_default_when_encrypted(self):
        dlg = EncryptDialog(record(encryption="enabled"))
        self.assertTrue(dlg.decrypt_radio.isChecked())
        self.assertFalse(dlg.encrypt_radio.isEnabled())
        dlg.old_password.setText("old")
        dlg._ok()
        args, stdin, secrets = dlg.command_parts()
        self.assertEqual(args, ["encryptmedium", record().uuid, "--oldpassword", "stdin"])
        self.assertEqual(stdin, b"old\n")
        self.assertEqual(secrets, [])

    def test_decrypt_requires_the_current_password(self):
        dlg = EncryptDialog(record(encryption="enabled"))
        with mock.patch.object(QMessageBox, "warning") as warn:
            dlg._ok()
        warn.assert_called_once()
        self.assertEqual(dlg.result(), 0)

    def test_change_password_uses_two_secret_files(self):
        dlg = EncryptDialog(record(encryption="enabled", cipher="AES-XTS128-PLAIN64"))
        self.assertEqual(dlg.cipher.currentText(), "AES-XTS128-PLAIN64")
        dlg.change_radio.setChecked(True)
        dlg.old_password.setText("old-pw")
        dlg.password.setText("new-pw")
        dlg.confirm.setText("new-pw")
        dlg.pwid.setText("rekeyed")
        dlg._ok()
        args, stdin, secrets = dlg.command_parts()
        try:
            self.assertIsNone(stdin, "two secrets cannot share one stdin stream")
            self.assertEqual(len(secrets), 2)
            old_file, new_file = secrets
            self.assertEqual(
                args,
                ["encryptmedium", record().uuid, "--oldpassword", old_file,
                 "--newpassword", new_file, "--newpasswordid", "rekeyed",
                 "--cipher", "AES-XTS128-PLAIN64"],
            )
            for path, secret in ((old_file, "old-pw"), (new_file, "new-pw")):
                self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
                with open(path) as f:
                    self.assertEqual(f.read(), secret + "\n")
            self.assertNotIn("old-pw", " ".join(args))
            self.assertNotIn("new-pw", " ".join(args))
        finally:
            for path in secrets:
                if os.path.exists(path):
                    os.remove(path)

    def test_change_password_writes_nothing_until_asked(self):
        # A dialog that is accepted but never launched must leave no secrets.
        before = set(pathlib.Path(tempfile.gettempdir()).glob("vboxfront-pw-*"))
        dlg = EncryptDialog(record(encryption="enabled"))
        dlg.change_radio.setChecked(True)
        dlg.old_password.setText("old")
        dlg.password.setText("new")
        dlg.confirm.setText("new")
        dlg._ok()
        self.assertEqual(dlg.result(), 1)
        after = set(pathlib.Path(tempfile.gettempdir()).glob("vboxfront-pw-*"))
        self.assertEqual(after - before, set())

    def test_attach_dialog_populates_and_builds_args(self):
        captured = []

        def capture(args, cb):
            captured.append(args)
            cb(0, VMINFO)

        dlg = AttachDialog(
            "disk", record(),
            vms=[("vbf-smoke", "24aa0bbd-3d9c-4ba3-a41f-273c0ab57661")],
            running=set(), capture=capture,
        )
        self.assertEqual(captured[0][0], "showvminfo")
        self.assertEqual(dlg.ctl_combo.currentData(), "SATA Controller")
        # Port 0 is occupied in the fixture, so the first free port is 1.
        self.assertEqual(dlg.port_spin.value(), 1)
        dlg._ok()
        self.assertEqual(
            dlg.args(),
            ["storageattach", "24aa0bbd-3d9c-4ba3-a41f-273c0ab57661",
             "--storagectl", "SATA Controller", "--port", "1", "--device", "0",
             "--type", "hdd", "--medium", record().uuid],
        )

    def test_attach_dialog_marks_running_vms(self):
        dlg = AttachDialog(
            "disk", record(),
            vms=[("vm1", "24aa0bbd-3d9c-4ba3-a41f-273c0ab57661")],
            running={"24aa0bbd-3d9c-4ba3-a41f-273c0ab57661"},
            capture=lambda args, cb: cb(0, VMINFO),
        )
        self.assertEqual(dlg.vm_combo.currentText(), "vm1 (running)")

    def test_resolve_relocates_to_an_existing_file(self):
        with tempfile.TemporaryDirectory() as d:
            moved = os.path.join(d, "moved.vdi")
            with open(moved, "w") as f:
                f.write("disk")
            dlg = ResolveDialog("disk", record(location="/gone/orig.vdi"))
            # The registered file is missing, so repair cannot apply.
            self.assertFalse(dlg.repair_radio.isEnabled())
            self.assertFalse(dlg.dryrun_radio.isEnabled())
            dlg.path_edit.setText(moved)
            dlg._ok()
            self.assertEqual(
                dlg.command,
                ["modifymedium", "disk", record().uuid, "--setlocation", moved],
            )

    def test_resolve_refuses_a_target_that_does_not_exist(self):
        dlg = ResolveDialog("disk", record(location="/gone/orig.vdi"))
        dlg.path_edit.setText("/also/gone.vdi")
        with mock.patch.object(QMessageBox, "warning") as warn:
            dlg._ok()
        warn.assert_called_once()
        self.assertIsNone(dlg.command)

    def test_resolve_refuses_the_location_already_registered(self):
        with tempfile.TemporaryDirectory() as d:
            here = os.path.join(d, "here.vdi")
            with open(here, "w") as f:
                f.write("disk")
            dlg = ResolveDialog("disk", record(location=here))
            with mock.patch.object(QMessageBox, "warning") as warn:
                dlg._ok()
            warn.assert_called_once()
            self.assertIsNone(dlg.command)

    def test_resolve_dry_run_and_repair(self):
        with tempfile.TemporaryDirectory() as d:
            img = os.path.join(d, "sick.vdi")
            with open(img, "w") as f:
                f.write("disk")
            dlg = ResolveDialog("disk", record(location=img, state="inaccessible"))
            self.assertTrue(dlg.dryrun_radio.isEnabled())
            dlg.dryrun_radio.setChecked(True)
            dlg._ok()
            self.assertEqual(
                dlg.command,
                ["internalcommands", "repairhd", "-dry-run", "-format", "VDI", img],
            )

            dlg = ResolveDialog("disk", record(location=img, state="inaccessible"))
            dlg.repair_radio.setChecked(True)
            with mock.patch.object(
                QMessageBox, "warning", return_value=QMessageBox.StandardButton.No
            ) as warn:
                dlg._ok()
            warn.assert_called_once()
            self.assertIsNone(dlg.command, "a rewrite must not happen unconfirmed")
            with mock.patch.object(
                QMessageBox, "warning", return_value=QMessageBox.StandardButton.Yes
            ):
                dlg._ok()
            self.assertEqual(
                dlg.command, ["internalcommands", "repairhd", "-format", "VDI", img]
            )

    def test_resolve_offers_no_repair_for_dvds(self):
        with tempfile.TemporaryDirectory() as d:
            iso = os.path.join(d, "x.iso")
            with open(iso, "w") as f:
                f.write("iso")
            dlg = ResolveDialog("dvd", record(location=iso, fmt="RAW"))
            self.assertFalse(dlg.repair_radio.isEnabled())

    def test_uuid_tools_new_specific_and_parent(self):
        with tempfile.TemporaryDirectory() as d:
            img = os.path.join(d, "copy.vdi")
            with open(img, "w") as f:
                f.write("disk")
            dlg = UuidToolsDialog({})
            dlg.path_edit.setText(img)
            dlg._ok()
            self.assertEqual(dlg.command, ["internalcommands", "sethduuid", img])

            dlg = UuidToolsDialog({})
            dlg.path_edit.setText(img)
            dlg.own_radio.setChecked(True)
            dlg.uuid_edit.setText("{11111111-2222-3333-4444-555555555555}")
            dlg._ok()
            self.assertEqual(
                dlg.command,
                ["internalcommands", "sethduuid", img,
                 "11111111-2222-3333-4444-555555555555"],
            )

            dlg = UuidToolsDialog({})
            dlg.path_edit.setText(img)
            dlg.parent_radio.setChecked(True)
            dlg.uuid_edit.setText("11111111-2222-3333-4444-555555555555")
            dlg._ok()
            self.assertEqual(dlg.command[1], "sethdparentuuid")

    def test_uuid_tools_validates_input(self):
        dlg = UuidToolsDialog({})
        dlg.path_edit.setText("/no/such/file.vdi")
        with mock.patch.object(QMessageBox, "warning") as warn:
            dlg._ok()
        warn.assert_called_once()
        self.assertIsNone(dlg.command)

        with tempfile.TemporaryDirectory() as d:
            img = os.path.join(d, "copy.vdi")
            with open(img, "w") as f:
                f.write("disk")
            dlg = UuidToolsDialog({})
            dlg.path_edit.setText(img)
            dlg.own_radio.setChecked(True)
            dlg.uuid_edit.setText("not-a-uuid")
            with mock.patch.object(QMessageBox, "warning") as warn:
                dlg._ok()
            warn.assert_called_once()
            self.assertIsNone(dlg.command)

    def test_uuid_tools_warns_on_a_registered_image(self):
        with tempfile.TemporaryDirectory() as d:
            img = os.path.join(d, "registered.vdi")
            with open(img, "w") as f:
                f.write("disk")
            dlg = UuidToolsDialog({img: "aaaa-uuid"})
            dlg.path_edit.setText(img)
            self.assertIn("registered", dlg.warning.text())
            with mock.patch.object(
                QMessageBox, "warning", return_value=QMessageBox.StandardButton.No
            ) as warn:
                dlg._ok()
            warn.assert_called_once()
            self.assertIsNone(dlg.command)

    def test_contents_builds_a_hex_read(self):
        dlg = ContentsDialog("disk", record())
        dlg.offset_edit.setText("0x200")
        dlg.size_edit.setText("1K")
        dlg._ok()
        args, stdin = dlg.command
        self.assertEqual(
            args,
            ["mediumio", f"--disk={record().uuid}", "cat", "--hex",
             "--offset=512", "--size=1024"],
        )
        self.assertIsNone(stdin)

    def test_contents_rejects_bad_numbers(self):
        for offset, size in (("nonsense", "512"), ("0", "0"), ("0", "")):
            dlg = ContentsDialog("disk", record())
            dlg.offset_edit.setText(offset)
            dlg.size_edit.setText(size)
            with mock.patch.object(QMessageBox, "warning") as warn:
                dlg._ok()
            warn.assert_called_once()
            self.assertIsNone(dlg.command)

    def test_contents_requires_a_password_for_encrypted_media(self):
        dlg = ContentsDialog("disk", record(encryption="enabled"))
        self.assertTrue(dlg.password.isEnabled())
        with mock.patch.object(QMessageBox, "warning") as warn:
            dlg._ok()
        warn.assert_called_once()
        self.assertIsNone(dlg.command)
        dlg.password.setText("pw")
        dlg._ok()
        args, stdin = dlg.command
        self.assertIn("--password-file=stdin", args)
        self.assertEqual(stdin, b"pw\n")
        self.assertNotIn("pw", " ".join(args))

    def test_contents_save_to_file_refuses_the_image_itself(self):
        with tempfile.TemporaryDirectory() as d:
            img = os.path.join(d, "a.vdi")
            with open(img, "w") as f:
                f.write("disk")
            dlg = ContentsDialog("disk", record(location=img))
            dlg.save_radio.setChecked(True)
            dlg.path_edit.setText(img)
            with mock.patch.object(QMessageBox, "warning") as warn:
                dlg._ok()
            warn.assert_called_once()
            self.assertIsNone(dlg.command)

    def test_contents_confirms_overwriting_the_output(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "dump.bin")
            with open(out, "w") as f:
                f.write("old")
            dlg = ContentsDialog("disk", record())
            dlg.save_radio.setChecked(True)
            dlg.path_edit.setText(out)
            with mock.patch.object(
                QMessageBox, "question", return_value=QMessageBox.StandardButton.No
            ):
                dlg._ok()
            self.assertIsNone(dlg.command)
            with mock.patch.object(
                QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
            ):
                dlg._ok()
            args, _stdin = dlg.command
            self.assertIn(f"--output={out}", args)
            self.assertNotIn("--hex", args)
            # mediumio truncates its own output file; nothing is deleted here.
            self.assertTrue(os.path.exists(out))

    def test_create_diff_inherits_size_and_refuses_existing(self):
        dlg = CreateDiffDialog(record(location="/vms/base.vdi"))
        self.assertEqual(dlg.path_edit.text(), "/vms/base-diff.vdi")
        dlg._ok()
        self.assertEqual(
            dlg.args(),
            ["createmedium", "disk", "--filename", "/vms/base-diff.vdi",
             "--diffparent", record().uuid],
        )
        self.assertNotIn("--size", dlg.args())

        dlg = CreateDiffDialog(record())
        dlg.path_edit.setText(__file__)
        with mock.patch.object(QMessageBox, "warning") as warn:
            dlg._ok()
        warn.assert_called_once()

    def test_create_diff_format_choices_come_from_backends(self):
        backends = parse_hddbackends(HDDBACKENDS)
        dlg = CreateDiffDialog(record(), backends)
        choices = [dlg.fmt_combo.itemData(i) for i in range(dlg.fmt_combo.count())]
        self.assertEqual(choices[0], "", "the default must be 'same as parent'")
        self.assertIn("VDI", choices)
        self.assertNotIn("RAW", choices, "RAW cannot hold a differencing image")
        dlg.fmt_combo.setCurrentIndex(choices.index("VMDK"))
        dlg._ok()
        self.assertEqual(dlg.args()[-2:], ["--format", "VMDK"])

    def test_create_floppy_formats_by_default(self):
        dlg = CreateFloppyDialog()
        dlg.path_edit.setText("/nonexistent/f.img")
        dlg._ok()
        self.assertEqual(
            dlg.args(),
            ["createmedium", "floppy", "--filename", "/nonexistent/f.img",
             "--size", "2", "--variant", "Formatted"],
        )
        dlg = CreateFloppyDialog()
        dlg.path_edit.setText("/nonexistent/f.img")
        dlg.raw_check.setChecked(True)
        dlg._ok()
        self.assertNotIn("--variant", dlg.args())

    def test_create_disk_formats_come_from_backends(self):
        backends = parse_hddbackends(HDDBACKENDS)
        dlg = CreateDiskDialog(backends)
        formats = [dlg.fmt_combo.itemText(i) for i in range(dlg.fmt_combo.count())]
        self.assertEqual(formats, ["VMDK", "VDI", "VHD", "Parallels"])
        dlg.fmt_combo.setCurrentText("Parallels")
        variants = [dlg.variant_combo.itemText(i) for i in range(dlg.variant_combo.count())]
        self.assertEqual(variants, ["Standard"], "Parallels cannot create fixed images")
        self.assertTrue(dlg.path_edit.text().endswith(".hdd"))
        dlg.fmt_combo.setCurrentText("VMDK")
        variants = [dlg.variant_combo.itemText(i) for i in range(dlg.variant_combo.count())]
        self.assertEqual(variants, ["Standard", "Fixed", "Split2G"])

    def test_create_disk_falls_back_without_backends(self):
        dlg = CreateDiskDialog()
        formats = [dlg.fmt_combo.itemText(i) for i in range(dlg.fmt_combo.count())]
        self.assertEqual(formats, ["VDI", "VMDK", "VHD"])

    def test_properties_autoreset_only_for_immutable(self):
        dlg = PropertiesDialog(record(medium_type="normal (base)"))
        self.assertFalse(dlg.autoreset.isEnabled())
        dlg.type_combo.setCurrentText("immutable")
        self.assertTrue(dlg.autoreset.isEnabled())
        dlg.autoreset.setChecked(True)
        dlg._ok()
        self.assertEqual(
            dlg.autoreset_command(),
            ["modifymedium", "disk", record().uuid, "--autoreset", "on"],
        )

    def test_properties_autoreset_unchanged_yields_nothing(self):
        dlg = PropertiesDialog(record(medium_type="immutable", auto_reset="on"))
        self.assertTrue(dlg.autoreset.isChecked())
        dlg._ok()
        self.assertIsNone(dlg.autoreset_command())

    def test_properties_refuses_type_change_on_a_differencing_image(self):
        # VirtualBox: "Cannot change the type of medium ... because it is a
        # differencing medium" — better said here than surfaced as a COM error.
        dlg = PropertiesDialog(record(medium_type="normal (differencing)"))
        self.assertFalse(dlg.type_combo.isEnabled())
        self.assertIn("differencing", dlg.hint.text())
        dlg.type_combo.setCurrentText("immutable")
        dlg._ok()
        self.assertIsNone(dlg.args("disk"))

    def test_attach_storage_flags_are_omitted_unless_asked(self):
        dlg = AttachDialog(
            "disk", record(),
            vms=[("vm1", "24aa0bbd-3d9c-4ba3-a41f-273c0ab57661")],
            running=set(), capture=lambda args, cb: cb(0, VMINFO),
        )
        dlg._ok()
        for flag in ("--mtype", "--discard", "--nonrotational", "--hotpluggable"):
            self.assertNotIn(flag, dlg.args())
        dlg.discard_check.setChecked(True)
        dlg.nonrotational_check.setChecked(True)
        dlg.mtype_combo.setCurrentText("writethrough")
        args = dlg.args()
        self.assertEqual(args[-6:], ["--mtype", "writethrough", "--discard", "on",
                                     "--nonrotational", "on"])

    def test_attach_storage_flags_are_disk_only(self):
        dlg = AttachDialog(
            "dvd", record(),
            vms=[("vm1", "24aa0bbd-3d9c-4ba3-a41f-273c0ab57661")],
            running=set(), capture=lambda args, cb: cb(0, VMINFO),
        )
        dlg.discard_check.setChecked(True)
        dlg.mtype_combo.setCurrentText("readonly")
        self.assertNotIn("--discard", dlg.args())
        self.assertNotIn("--mtype", dlg.args())

    def test_eliding_label_never_demands_more_than_it_is_given(self):
        label = ElidingLabel("Idle.")
        label.resize(200, 20)
        long_text = "Running: /usr/bin/VBoxManage " + "closemedium/disk/x" * 20
        label.setFullText(long_text)
        self.assertEqual(label.fullText(), long_text)
        self.assertIn("…", label.text())
        self.assertLess(len(label.text()), len(long_text))
        self.assertEqual(label.toolTip(), long_text, "the full line stays recoverable")
        # Ignored horizontally, so the text can never drive the layout wider.
        self.assertEqual(
            label.sizePolicy().horizontalPolicy(), QSizePolicy.Policy.Ignored
        )

    def test_eliding_label_re_elides_when_resized(self):
        label = ElidingLabel()
        label.show()  # Qt defers resize events for hidden widgets
        label.resize(400, 20)
        QApplication.processEvents()
        label.setFullText("x" * 400)
        wide = label.text()
        label.resize(80, 20)
        QApplication.processEvents()
        narrow = label.text()
        self.assertLess(len(narrow), len(wide))
        label.resize(400, 20)
        QApplication.processEvents()
        self.assertEqual(label.text(), wide, "widening must restore the text")
        label.close()

    def test_check_password_button_runs_checkmediumpwd(self):
        dlg = EncryptDialog(record(encryption="enabled"))
        self.assertTrue(dlg.check_btn.isEnabled())
        with mock.patch.object(QMessageBox, "warning") as warn:
            dlg._check_password()
        warn.assert_called_once()
        self.assertIsNone(dlg.check_command, "no password, no command")

        dlg.old_password.setText("pw")
        dlg._check_password()
        self.assertEqual(dlg.result(), EncryptDialog.CHECK_ONLY)
        args, stdin = dlg.check_command
        self.assertEqual(args, ["checkmediumpwd", record().uuid, "stdin"])
        self.assertEqual(stdin, b"pw\n")

    def test_check_password_is_offline_while_encrypting(self):
        # Nothing to check yet on a medium that is not encrypted.
        dlg = EncryptDialog(record())
        self.assertTrue(dlg.encrypt_radio.isChecked())
        self.assertFalse(dlg.check_btn.isEnabled())

    def test_contents_confirms_a_huge_hex_dump(self):
        dlg = ContentsDialog("disk", record())
        dlg.size_edit.setText("64M")
        with mock.patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.No
        ) as ask:
            dlg._ok()
        ask.assert_called_once()
        self.assertIsNone(dlg.command)
        with mock.patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
        ):
            dlg._ok()
        self.assertIn("--size=67108864", dlg.command[0])

    def test_contents_does_not_nag_about_a_large_extract(self):
        # Only the in-panel hex dump is expensive; writing to a file is not.
        with tempfile.TemporaryDirectory() as d:
            dlg = ContentsDialog("disk", record())
            dlg.size_edit.setText("64M")
            dlg.save_radio.setChecked(True)
            dlg.path_edit.setText(os.path.join(d, "big.bin"))
            with mock.patch.object(QMessageBox, "question") as ask:
                dlg._ok()
            ask.assert_not_called()
            self.assertIsNotNone(dlg.command)

    def test_eliding_label_keeps_the_full_text_through_setText(self):
        label = ElidingLabel()
        label.show()
        label.resize(60, 20)
        QApplication.processEvents()
        label.setText("a rather long status line that will not fit in sixty pixels")
        self.assertEqual(
            label.fullText(), "a rather long status line that will not fit in sixty pixels"
        )
        self.assertIn("…", label.text())
        label.close()

    def test_settings_dialog_edits_library_and_path(self):
        save_library("disk", [])
        save_library("dvd", [])
        add_library_path("disk", "/vms/a.vdi")
        add_library_path("dvd", "/isos/b.iso")
        dlg = SettingsDialog()
        self.assertEqual(dlg.library_list.count(), 2)
        dlg.library_list.setCurrentRow(0)
        dlg._remove_selected()
        dlg.path_edit.setText("/opt/vbox/VBoxManage")
        dlg._ok()
        self.assertEqual(load_library("disk"), [])
        self.assertEqual(load_library("dvd"), ["/isos/b.iso"])
        self.assertEqual(vboxmanage_path(), "/opt/vbox/VBoxManage")
        get_settings().setValue("vboxmanage_path", "")
        save_library("dvd", [])


IDE_VMINFO = """\
storagecontrollername0="IDE"
storagecontrollertype0="PIIX4"
storagecontrollerportcount0="2"
"IDE-0-0"="none"
"IDE-0-1"="none"
"IDE-1-0"="none"
"IDE-1-1"="none"
"""


class ResizeDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv[:1])

    def test_gigabytes_keep_the_size_they_convert_from(self):
        # setRange clamps before setValue, so a naive switch turned 25600 MB
        # into 4096 MB and then into 4 GB.
        dlg = ResizeDialog(record(capacity="25600 MBytes"))
        self.assertEqual(dlg.spin.value(), 25600)
        dlg.unit.setCurrentText("GB")
        self.assertEqual(dlg.spin.value(), 25)
        self.assertEqual(dlg.target_bytes(), 25600 * 1024 ** 2)
        dlg.unit.setCurrentText("MB")
        self.assertEqual(dlg.spin.value(), 25600)

    def test_the_delta_names_the_growth(self):
        dlg = ResizeDialog(record(capacity="1024 MBytes"))
        dlg.spin.setValue(2048)
        self.assertIn("+1.0 GB", dlg.delta.text())

    def test_shrinking_is_refused_with_a_reason(self):
        dlg = ResizeDialog(record(capacity="2048 MBytes"))
        dlg.spin.setValue(1024)
        self.assertIn("refuses to shrink", dlg.delta.text().lower()
                      .replace("virtualbox only grows images", "refuses to shrink"))
        with mock.patch.object(QMessageBox, "warning") as warn:
            dlg._ok()
        warn.assert_called_once()
        self.assertEqual(dlg.result(), 0)

    def test_the_same_size_is_not_a_command(self):
        dlg = ResizeDialog(record(capacity="1024 MBytes"))
        with mock.patch.object(QMessageBox, "information") as info:
            dlg._ok()
        info.assert_called_once()
        self.assertEqual(dlg.result(), 0)

    def test_an_exact_size_uses_resizebyte(self):
        dlg = ResizeDialog(record(capacity="64 MBytes"))
        dlg.exact_check.setChecked(True)
        # Hex is how a partition table quotes an offset; this one is not a
        # whole number of megabytes, which is exactly what --resize cannot say.
        dlg.exact_edit.setText("0x6000200")
        dlg._ok()
        self.assertEqual(dlg.args("u"),
                         ["modifymedium", "disk", "u", "--resizebyte", "100663808"])

    def test_a_whole_megabyte_exact_size_still_uses_resize(self):
        # --resizebyte exists for sizes --resize cannot express; a round one can
        # go the ordinary way and stay readable in the history.
        dlg = ResizeDialog(record(capacity="64 MBytes"))
        dlg.exact_check.setChecked(True)
        dlg.exact_edit.setText("128M")
        dlg._ok()
        self.assertEqual(dlg.args("u"),
                         ["modifymedium", "disk", "u", "--resize", "128"])

    def test_junk_is_rejected(self):
        dlg = ResizeDialog(record(capacity="64 MBytes"))
        dlg.exact_check.setChecked(True)
        dlg.exact_edit.setText("about a gig")
        with mock.patch.object(QMessageBox, "warning") as warn:
            dlg._ok()
        warn.assert_called_once()


class ConvertVariantTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv[:1])

    def test_variants_come_from_the_target_format(self):
        backends = parse_hddbackends(HDDBACKENDS)
        dlg = ConvertDialog(record(), backends)
        dlg.fmt.setCurrentText("VMDK")
        self.assertIn("Split2G", [dlg.variant.itemText(i)
                                  for i in range(dlg.variant.count())])
        dlg.fmt.setCurrentText("VDI")
        self.assertNotIn("Split2G", [dlg.variant.itemText(i)
                                     for i in range(dlg.variant.count())])

    def test_the_chosen_variant_reaches_the_command(self):
        dlg = ConvertDialog(record(), parse_hddbackends(HDDBACKENDS))
        dlg.fmt.setCurrentText("VMDK")
        dlg.variant.setCurrentText("Fixed")
        dlg.path_edit.setText("/nonexistent/out.vmdk")
        dlg._ok()
        args = dlg.args("u")
        self.assertEqual(args[-4:], ["--format", "VMDK", "--variant", "Fixed"])

    def test_existing_requires_a_file_that_is_there(self):
        dlg = ConvertDialog(record())
        dlg.existing_check.setChecked(True)
        dlg.path_edit.setText("/nonexistent/pre.vdi")
        with mock.patch.object(QMessageBox, "warning") as warn:
            dlg._ok()
        warn.assert_called_once()
        self.assertEqual(dlg.result(), 0)

    def test_existing_never_deletes_the_target(self):
        # --existing clones *into* the file; treating it as an overwrite target
        # would delete the very image being cloned into.
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "pre.vdi")
            with open(target, "w") as f:
                f.write("pre-allocated")
            dlg = ConvertDialog(record())
            dlg.existing_check.setChecked(True)
            dlg.path_edit.setText(target)
            dlg._ok()
            self.assertFalse(dlg.overwrite)
            self.assertTrue(dlg.existing)
            self.assertTrue(os.path.exists(target))
            self.assertEqual(dlg.args("u"),
                             ["clonemedium", "disk", "u", target, "--existing"])


class MediumPropertyDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv[:1])

    def _dialog(self, **overrides):
        rec = record(properties=["AllocationBlockSize=1048576"], **overrides)
        return MediumPropertyDialog("disk", rec, parse_hddbackends(HDDBACKENDS))

    def test_values_come_from_the_listing_not_a_command(self):
        dlg = self._dialog()
        self.assertEqual(dlg._edits["AllocationBlockSize"].text(), "1048576")

    def test_the_schema_lookup_survives_a_lowercase_format(self):
        # `list -l` reports "vdi" while backend ids are upper case; matching
        # literally left every field without its type and default.
        dlg = self._dialog(fmt="vdi")
        self.assertIn("AllocationBlockSize", dlg._edits)

    def test_an_unchanged_field_produces_no_command(self):
        self.assertEqual(self._dialog().commands(), [])

    def test_a_changed_value_is_set(self):
        dlg = self._dialog()
        dlg._edits["AllocationBlockSize"].setText("2097152")
        self.assertEqual(
            dlg.commands(),
            [["mediumproperty", "disk", "set", record().uuid,
              "AllocationBlockSize", "2097152"]],
        )

    def test_clearing_a_set_value_deletes_it(self):
        dlg = self._dialog()
        dlg._edits["AllocationBlockSize"].setText("")
        self.assertEqual(dlg.commands()[0][3:], [record().uuid, "AllocationBlockSize"])
        self.assertEqual(dlg.commands()[0][2], "delete")

    def test_clearing_a_field_that_was_never_set_does_nothing(self):
        dlg = MediumPropertyDialog("disk", record(properties=[]),
                                   parse_hddbackends(HDDBACKENDS))
        for edit in dlg._edits.values():
            edit.setText("")
        self.assertEqual(dlg.commands(), [])


class BatchConvertDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv[:1])

    def _disks(self, folder):
        return [
            record(uuid="1" * 8, location=os.path.join(folder, "a.vdi")),
            record(uuid="2" * 8, location=os.path.join(folder, "b.vdi")),
        ]

    def test_targets_are_derived_per_source(self):
        with tempfile.TemporaryDirectory() as d:
            dlg = BatchConvertDialog(self._disks(d), parse_hddbackends(HDDBACKENDS))
            dlg.fmt.setCurrentText("VMDK")
            self.assertEqual(
                [t for _s, t in dlg._plan()],
                [os.path.join(d, "a-converted.vmdk"), os.path.join(d, "b-converted.vmdk")],
            )

    def test_a_suffix_that_collides_with_the_source_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            disks = self._disks(d)
            for disk in disks:
                with open(disk.location, "w") as f:
                    f.write("x")
            dlg = BatchConvertDialog(disks, parse_hddbackends(HDDBACKENDS))
            dlg.fmt.setCurrentText("VDI")
            dlg.suffix.setText("")
            with mock.patch.object(QMessageBox, "warning") as warn:
                dlg._ok()
            warn.assert_called_once()
            self.assertIn("source", warn.call_args[0][2])
            self.assertTrue(all(os.path.exists(d_.location) for d_ in disks))

    def test_two_sources_may_not_share_one_target(self):
        with tempfile.TemporaryDirectory() as d:
            disks = [record(uuid="1" * 8, location=os.path.join(d, "sub1", "a.vdi")),
                     record(uuid="2" * 8, location=os.path.join(d, "sub2", "a.vdi"))]
            dlg = BatchConvertDialog(disks, parse_hddbackends(HDDBACKENDS))
            dlg.dir_edit.setText(d)
            with mock.patch.object(QMessageBox, "warning") as warn:
                dlg._ok()
            warn.assert_called_once()
            self.assertIn("same target", warn.call_args[0][2])

    def test_commands_carry_the_format_and_variant(self):
        with tempfile.TemporaryDirectory() as d:
            dlg = BatchConvertDialog(self._disks(d), parse_hddbackends(HDDBACKENDS))
            dlg.fmt.setCurrentText("VMDK")
            dlg.variant.setCurrentText("Fixed")
            dlg._ok()
            commands = dlg.commands()
            self.assertEqual(len(commands), 2)
            self.assertEqual(commands[0][:2], ["clonemedium", "disk"])
            self.assertEqual(commands[0][-4:], ["--format", "VMDK", "--variant", "Fixed"])


class HealthDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv[:1])

    def test_a_clean_registry_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ok.vdi")
            with open(path, "w") as f:
                f.write("x")
            dlg = HealthDialog({"disk": [record(location=path)], "dvd": [], "floppy": []})
            self.assertIn("no problems found", dlg._summary().lower())

    def test_problems_are_counted_by_severity(self):
        dlg = HealthDialog({"disk": [record(state="inaccessible")], "dvd": [], "floppy": []})
        self.assertIn("error", dlg._summary())
        self.assertTrue(dlg.list.count())

    def test_the_dry_run_only_offers_readable_vdis(self):
        with tempfile.TemporaryDirectory() as d:
            here = os.path.join(d, "here.vdi")
            with open(here, "w") as f:
                f.write("x")
            media = {
                "disk": [record(location=here),
                         record(uuid="2" * 8, location=os.path.join(d, "gone.vdi")),
                         record(uuid="3" * 8, fmt="VMDK",
                                location=os.path.join(d, "other.vmdk"))],
                "dvd": [], "floppy": [],
            }
            dlg = HealthDialog(media)
            with mock.patch.object(QMessageBox, "question",
                                   return_value=QMessageBox.StandardButton.Yes):
                dlg._start_dry_run()
            self.assertEqual(dlg.dry_run_paths, [here])

    def test_the_dry_run_caveat_names_the_misleading_line(self):
        # 7.2.12 signs off with "Corrupted VDI image repaired successfully"
        # even on a clean image it never wrote to.
        self.assertIn("repaired successfully", HealthDialog.dry_run_caveat())


class OrphanScanDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv[:1])

    def test_scanning_lists_only_unknown_images(self):
        with tempfile.TemporaryDirectory() as d:
            known = os.path.join(d, "known.vdi")
            orphan = os.path.join(d, "orphan.vdi")
            for path in (known, orphan):
                with open(path, "w") as f:
                    f.write("x")
            dlg = OrphanScanDialog([known])
            dlg.folders.clear()
            dlg.folders.addItem(d)
            dlg._scan()
            self.assertEqual(dlg.results.count(), 1)
            self.assertIn("orphan.vdi", dlg.results.item(0).text())

    def test_only_checked_rows_are_returned(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("a.vdi", "b.iso"):
                with open(os.path.join(d, name), "w") as f:
                    f.write("x")
            dlg = OrphanScanDialog([])
            dlg.folders.clear()
            dlg.folders.addItem(d)
            dlg._scan()
            dlg._set_all(False)
            dlg.results.item(0).setCheckState(Qt.CheckState.Checked)
            dlg._ok()
            self.assertEqual(len(dlg.chosen), 1)
            kind, path = dlg.chosen[0]
            self.assertEqual(kind, "disk" if path.endswith(".vdi") else "dvd")


class RemoveBatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv[:1])

    def test_one_medium_still_shows_its_details(self):
        dlg = RemoveDialog(record())
        self.assertEqual(dlg.windowTitle(), "Remove medium")

    def test_many_media_are_listed_and_the_delete_warning_counts_them(self):
        media = [record(uuid="1" * 8), record(uuid="2" * 8, location="/vms/two.vdi")]
        dlg = RemoveDialog(media)
        self.assertEqual(dlg.windowTitle(), "Remove media")
        dlg.mode.setCurrentIndex(1)
        with mock.patch.object(QMessageBox, "warning",
                               return_value=QMessageBox.StandardButton.No) as warn:
            dlg._ok()
        self.assertIn("2 disk image files", warn.call_args[0][2])
        self.assertFalse(dlg.delete_file, "declining the warning still armed --delete")


class HistoryRerunTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv[:1])

    def setUp(self):
        clear_history()

    def tearDown(self):
        clear_history()

    def _dialog(self, command):
        append_history(command, 0, "2026-01-01 00:00:00")
        dlg = CommandHistoryDialog()
        dlg.list.setCurrentRow(0)
        return dlg

    def test_a_harmless_command_is_confirmed_once(self):
        dlg = self._dialog("VBoxManage modifymedium disk u --compact")
        with mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.Yes):
            dlg._rerun_selected()
        self.assertEqual(dlg.rerun, ["modifymedium", "disk", "u", "--compact"])

    def test_a_destructive_command_needs_the_word_typed(self):
        dlg = self._dialog("VBoxManage closemedium disk u --delete")
        with mock.patch("vboxfront.QInputDialog.getText", return_value=("", True)):
            dlg._rerun_selected()
        self.assertIsNone(dlg.rerun, "an empty confirmation replayed a --delete")
        with mock.patch("vboxfront.QInputDialog.getText", return_value=("RUN", True)):
            dlg._rerun_selected()
        self.assertEqual(dlg.rerun, ["closemedium", "disk", "u", "--delete"])

    def test_a_command_whose_secret_is_gone_is_refused(self):
        dlg = self._dialog("VBoxManage encryptmedium u --newpassword stdin")
        with mock.patch.object(QMessageBox, "warning") as warn:
            dlg._rerun_selected()
        warn.assert_called_once()
        self.assertIsNone(dlg.rerun)


class AttachSlotsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv[:1])

    def test_sata_offers_no_second_device(self):
        dlg = AttachDialog(
            "disk", record(),
            vms=[("vm1", "24aa0bbd-3d9c-4ba3-a41f-273c0ab57661")],
            running=set(), capture=lambda args, cb: cb(0, VMINFO),
        )
        self.assertEqual(dlg.device_spin.maximum(), 0)

    def test_ide_offers_master_and_slave(self):
        dlg = AttachDialog(
            "disk", record(),
            vms=[("vm1", "24aa0bbd-3d9c-4ba3-a41f-273c0ab57661")],
            running=set(), capture=lambda args, cb: cb(0, IDE_VMINFO),
        )
        self.assertEqual(dlg.device_spin.maximum(), 1)

    def test_a_dvd_can_be_pointed_at_an_empty_or_host_drive(self):
        def capture(args, cb):
            cb(0, "UUID: aaaa\nName: /dev/sr0\n" if args[:2] == ["list", "hostdvds"]
               else IDE_VMINFO)
            return True

        dlg = AttachDialog(
            "dvd", record(location="/isos/boot.iso"),
            vms=[("vm1", "24aa0bbd-3d9c-4ba3-a41f-273c0ab57661")],
            running=set(), capture=capture,
        )
        options = [dlg.medium_combo.itemData(i) for i in range(dlg.medium_combo.count())]
        self.assertIn("emptydrive", options)
        self.assertIn("additions", options)
        self.assertIn("host:/dev/sr0", options)
        dlg.medium_combo.setCurrentIndex(options.index("host:/dev/sr0"))
        # Passthrough only means anything for a real drive.
        self.assertTrue(dlg.passthrough_check.isEnabled())
        dlg.passthrough_check.setChecked(True)
        args = dlg.args()
        self.assertIn("host:/dev/sr0", args)
        self.assertIn("--passthrough", args)

    def test_passthrough_is_dropped_for_an_image(self):
        def capture(args, cb):
            cb(0, "" if args[:2] == ["list", "hostdvds"] else IDE_VMINFO)
            return True

        dlg = AttachDialog(
            "dvd", record(location="/isos/boot.iso"),
            vms=[("vm1", "24aa0bbd-3d9c-4ba3-a41f-273c0ab57661")],
            running=set(), capture=capture,
        )
        self.assertFalse(dlg.passthrough_check.isEnabled())
        self.assertNotIn("--passthrough", dlg.args())

    def test_a_disk_attach_is_unchanged(self):
        dlg = AttachDialog(
            "disk", record(),
            vms=[("vm1", "24aa0bbd-3d9c-4ba3-a41f-273c0ab57661")],
            running=set(), capture=lambda args, cb: cb(0, VMINFO),
        )
        args = dlg.args()
        self.assertIn(record().uuid, args)
        for flag in ("--passthrough", "--tempeject", "--forceunmount"):
            self.assertNotIn(flag, args)


if __name__ == "__main__":
    unittest.main()
