"""Dialog accept-logic tests (offscreen Qt, no display needed)."""

import sys
import unittest
from unittest import mock

from PyQt6.QtWidgets import QApplication, QMessageBox

from vboxfront import (
    AttachDialog,
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
        args, stdin = dlg.command
        self.assertIn("--newpassword", args)
        self.assertEqual(stdin, b"pw\n")

    def test_decrypt_default_when_encrypted(self):
        dlg = EncryptDialog(record(encryption="AES-XTS256-PLAIN64"))
        self.assertTrue(dlg.decrypt_radio.isChecked())
        dlg.password.setText("old")
        dlg._ok()
        args, stdin = dlg.command
        self.assertEqual(args, ["encryptmedium", record().uuid, "--oldpassword", "-"])
        self.assertEqual(stdin, b"old\n")

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
