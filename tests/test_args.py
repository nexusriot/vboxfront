"""Command argv builders."""

import unittest

from vboxfront import (
    attach_args,
    check_password_args,
    create_disk_args,
    decrypt_args,
    detach_args,
    encrypt_args,
    list_media_args,
    move_args,
    properties_args,
)


class ArgBuildersTest(unittest.TestCase):
    def test_list_media(self):
        self.assertEqual(list_media_args("disk"), ["list", "-l", "hdds"])
        self.assertEqual(list_media_args("dvd"), ["list", "-l", "dvds"])
        self.assertEqual(list_media_args("floppy"), ["list", "-l", "floppies"])

    def test_create_standard_variant_is_omitted(self):
        self.assertEqual(
            create_disk_args("/a.vdi", 1024, "VDI", "Standard"),
            ["createmedium", "disk", "--filename", "/a.vdi", "--size", "1024", "--format", "VDI"],
        )

    def test_create_fixed_variant(self):
        args = create_disk_args("/a.vmdk", 2048, "VMDK", "Fixed")
        self.assertEqual(args[-2:], ["--variant", "Fixed"])

    def test_attach(self):
        self.assertEqual(
            attach_args("vm-uuid", "SATA Controller", 1, 0, "disk", "med-uuid"),
            ["storageattach", "vm-uuid", "--storagectl", "SATA Controller",
             "--port", "1", "--device", "0", "--type", "hdd", "--medium", "med-uuid"],
        )

    def test_attach_type_per_kind(self):
        self.assertIn("dvddrive", attach_args("v", "c", 0, 0, "dvd", "m"))
        self.assertIn("fdd", attach_args("v", "c", 0, 0, "floppy", "m"))

    def test_detach(self):
        args = detach_args("vm", "IDE", 0, 1)
        self.assertEqual(args[-2:], ["--medium", "none"])
        self.assertNotIn("--type", args)

    def test_properties_none_when_unchanged(self):
        self.assertIsNone(properties_args("disk", "u", None, None))

    def test_properties_type_and_description(self):
        self.assertEqual(
            properties_args("disk", "u", "immutable", "hello"),
            ["modifymedium", "disk", "u", "--type", "immutable", "--description", "hello"],
        )

    def test_properties_clear_description(self):
        self.assertEqual(
            properties_args("disk", "u", None, ""),
            ["modifymedium", "disk", "u", "--description", ""],
        )

    def test_move(self):
        self.assertEqual(
            move_args("u", "/new/place.vdi"),
            ["modifymedium", "disk", "u", "--move", "/new/place.vdi"],
        )

    def test_encrypt_password_goes_to_stdin(self):
        args, stdin = encrypt_args("u", "s3cret", "backup-id", "AES-XTS256-PLAIN64")
        self.assertNotIn("s3cret", " ".join(args))
        self.assertEqual(stdin, b"s3cret\n")
        self.assertEqual(
            args,
            ["encryptmedium", "u", "--newpassword", "-",
             "--newpasswordid", "backup-id", "--cipher", "AES-XTS256-PLAIN64"],
        )

    def test_decrypt(self):
        args, stdin = decrypt_args("u", "pw")
        self.assertEqual(args, ["encryptmedium", "u", "--oldpassword", "-"])
        self.assertEqual(stdin, b"pw\n")

    def test_check_password(self):
        args, stdin = check_password_args("u", "pw")
        self.assertEqual(args, ["checkmediumpwd", "u", "-"])
        self.assertEqual(stdin, b"pw\n")


if __name__ == "__main__":
    unittest.main()
