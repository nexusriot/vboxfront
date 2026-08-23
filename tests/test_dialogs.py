"""Dialog accept-logic tests (offscreen Qt, no display needed)."""

import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from PyQt6.QtWidgets import QApplication, QMessageBox, QSizePolicy

from tests.test_parsing import HDDBACKENDS
from vboxfront import (
    AttachDialog,
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


if __name__ == "__main__":
    unittest.main()
