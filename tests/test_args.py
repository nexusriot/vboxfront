"""Command argv builders, and the version the build scripts read."""

import os
import pathlib
import sys
import tempfile
import unittest

from vboxfront import (
    APP_VERSION,
    PASSWORD_STDIN,
    get_settings,
    attach_args,
    check_password_args,
    create_disk_args,
    decrypt_args,
    detach_args,
    encrypt_args,
    change_password_args,
    is_uuid,
    list_media_args,
    move_args,
    properties_args,
    repairhd_args,
    sethdparentuuid_args,
    sethduuid_args,
    setlocation_args,
    vboxmanage_path,
    vboxmanage_problem,
    write_secret_file,
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
            ["encryptmedium", "u", "--newpassword", "stdin",
             "--newpasswordid", "backup-id", "--cipher", "AES-XTS256-PLAIN64"],
        )

    def test_decrypt(self):
        args, stdin = decrypt_args("u", "pw")
        self.assertEqual(args, ["encryptmedium", "u", "--oldpassword", "stdin"])
        self.assertEqual(stdin, b"pw\n")

    def test_check_password(self):
        args, stdin = check_password_args("u", "pw")
        self.assertEqual(args, ["checkmediumpwd", "u", "stdin"])
        self.assertEqual(stdin, b"pw\n")

    def test_passwords_never_use_the_console_placeholder(self):
        # "-" makes VBoxManage prompt on a terminal (it disables echo first),
        # which fails outright on QProcess's pipe; only "stdin" is readable.
        for args, _stdin in (
            encrypt_args("u", "pw", "id", "AES-XTS256-PLAIN64"),
            decrypt_args("u", "pw"),
            check_password_args("u", "pw"),
        ):
            self.assertNotIn("-", args[1:], args)


class RescueArgBuildersTest(unittest.TestCase):
    def test_setlocation(self):
        self.assertEqual(
            setlocation_args("disk", "u", "/vms/moved.vdi"),
            ["modifymedium", "disk", "u", "--setlocation", "/vms/moved.vdi"],
        )
        self.assertEqual(setlocation_args("dvd", "u", "/i.iso")[1], "dvd")

    def test_repairhd_dry_run_and_format(self):
        self.assertEqual(
            repairhd_args("/a.vdi", "vdi", dry_run=True),
            ["internalcommands", "repairhd", "-dry-run", "-format", "VDI", "/a.vdi"],
        )
        self.assertEqual(
            repairhd_args("/a.vdi"),
            ["internalcommands", "repairhd", "/a.vdi"],
        )
        # The filename is always last, so a path is never read as a flag value.
        self.assertEqual(repairhd_args("/a.vdi", "VMDK", True)[-1], "/a.vdi")

    def test_sethduuid_random_and_explicit(self):
        self.assertEqual(
            sethduuid_args("/a.vdi"),
            ["internalcommands", "sethduuid", "/a.vdi"],
        )
        self.assertEqual(
            sethduuid_args("/a.vdi", "11111111-2222-3333-4444-555555555555"),
            ["internalcommands", "sethduuid", "/a.vdi",
             "11111111-2222-3333-4444-555555555555"],
        )

    def test_sethdparentuuid(self):
        self.assertEqual(
            sethdparentuuid_args("/child.vdi", "u"),
            ["internalcommands", "sethdparentuuid", "/child.vdi", "u"],
        )

    def test_change_password_reads_both_secrets_from_files(self):
        args = change_password_args("u", "/tmp/old", "/tmp/new", "id2", "AES-XTS256-PLAIN64")
        self.assertEqual(
            args,
            ["encryptmedium", "u", "--oldpassword", "/tmp/old",
             "--newpassword", "/tmp/new", "--newpasswordid", "id2",
             "--cipher", "AES-XTS256-PLAIN64"],
        )
        # Never the stdin placeholder: the first read would eat both secrets.
        self.assertNotIn(PASSWORD_STDIN, args)

    def test_write_secret_file_is_private_and_newline_terminated(self):
        path = write_secret_file("s3cret")
        try:
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
            with open(path) as f:
                self.assertEqual(f.read(), "s3cret\n")
        finally:
            os.remove(path)

    def test_is_uuid(self):
        self.assertTrue(is_uuid("2be9b94d-a960-4746-8a5d-73622974890a"))
        self.assertTrue(is_uuid("{2be9b94d-a960-4746-8a5d-73622974890a}"))
        self.assertFalse(is_uuid("2be9b94d"))
        self.assertFalse(is_uuid(""))
        self.assertFalse(is_uuid("zzzzzzzz-a960-4746-8a5d-73622974890a"))


class VboxmanageProblemTest(unittest.TestCase):
    def tearDown(self):
        get_settings().setValue("vboxmanage_path", "")

    def test_no_problem_when_the_configured_path_is_executable(self):
        get_settings().setValue("vboxmanage_path", sys.executable)
        self.assertEqual(vboxmanage_problem(), "")

    def test_a_configured_path_that_cannot_run_is_reported(self):
        get_settings().setValue("vboxmanage_path", "/does/not/exist/VBoxManage")
        problem = vboxmanage_problem()
        self.assertIn("not an executable file", problem)
        self.assertIn("/does/not/exist/VBoxManage", problem)

    def test_a_directory_is_not_mistaken_for_the_binary(self):
        get_settings().setValue("vboxmanage_path", tempfile.gettempdir())
        self.assertIn("not an executable file", vboxmanage_problem())

    def test_falls_back_to_path_when_unset(self):
        get_settings().setValue("vboxmanage_path", "")
        # Whatever this machine has: either it is on PATH, or the message says
        # it is missing. Never a false "unusable" for a working install.
        problem = vboxmanage_problem()
        self.assertEqual(bool(problem), vboxmanage_path() is None)


class VersionTest(unittest.TestCase):
    """APP_VERSION is the single source; nothing may restate it out of sync."""

    root = pathlib.Path(__file__).resolve().parent.parent

    def test_version_looks_like_a_release(self):
        self.assertRegex(APP_VERSION, r"^\d+\.\d+\.\d+$")

    def test_changelog_leads_with_the_current_version(self):
        head = (self.root / "scripts/deb/changelog").read_text().splitlines()[0]
        self.assertEqual(head.split()[1], f"({APP_VERSION})")

    def test_deb_script_rebuilds_a_stale_binary(self):
        # Packaging a leftover binary from an earlier version yields a .deb
        # whose control file and executable disagree about what they are.
        script = (self.root / "scripts/build-deb.sh").read_text()
        self.assertIn("-nt", script, "no staleness check on the packaged binary")
        self.assertIn("vboxfront.py", script)

    def test_build_inputs_do_not_hardcode_a_version(self):
        # The spec and the build scripts must read APP_VERSION, not repeat it.
        for name in ("vboxfront.spec", "scripts/build-deb.sh", "Makefile"):
            text = (self.root / name).read_text()
            self.assertNotIn(APP_VERSION, text, f"{name} restates the version")
            self.assertIn("APP_VERSION", text, f"{name} does not read APP_VERSION")


if __name__ == "__main__":
    unittest.main()
