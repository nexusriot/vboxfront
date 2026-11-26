"""Finding a moved image by the UUID stored inside it."""
import os
import struct
import sys
import tempfile
import unittest
import uuid as uuidlib
from unittest import mock

from PyQt6.QtWidgets import QApplication, QMessageBox

from tests.test_window import wait_until
from vboxfront_app.constants import TRASH_DIR_NAME
from vboxfront_app.locate import (
    MATCH_NAME,
    MATCH_UUID,
    default_search_folders,
    find_moved_image,
    missing_extents,
    read_image_identity,
    read_image_uuid,
)
from vboxfront_app.locate_dialog import LocateMovedDialog
from vboxfront_app.media_dialogs import ResolveDialog
from vboxfront_app.models import MediumRecord

OWN = "7c148bc5-f95c-43b6-b4d1-55f329896b7e"
PARENT = "a2b53516-8da0-41e2-a44a-c957b46cc6dc"
OTHER = "18c37bae-68c3-412b-943d-99427624e5f2"

# The 16 bytes VBoxManage 7.2.22 wrote for OWN at VDI offset 0x188, and for
# PARENT at VHD footer offset 68. Pinned as raw bytes so a byte-order mistake
# in the reader cannot be hidden by the test encoding the same way.
OWN_RAW = bytes.fromhex("c58b147c5cf9b643b4d155f329896b7e")
PARENT_RAW = bytes.fromhex("1635b5a2a08de241a44ac957b46cc6dc")


def le(text: str) -> bytes:
    return uuidlib.UUID(text).bytes_le


def write_vdi(path, own=OWN, parent="", version=0x00010001, size=4096):
    head = bytearray(size)
    head[:38] = b"<<< Oracle VirtualBox Disk Image >>>\n\0"
    struct.pack_into("<II", head, 0x40, 0xBEDA107F, version)
    head[0x188:0x198] = le(own)
    if parent:
        head[0x1A8:0x1B8] = le(parent)
    with open(path, "wb") as handle:
        handle.write(head)


def vhd_footer(own):
    footer = bytearray(512)
    footer[:8] = b"conectix"
    footer[68:84] = le(own)
    return bytes(footer)


def write_vhd(path, own=OWN, parent="", dynamic=True, data=8192):
    with open(path, "wb") as handle:
        if dynamic:
            handle.write(vhd_footer(own))
            sparse = bytearray(1024)
            sparse[:8] = b"cxsparse"
            if parent:
                sparse[40:56] = le(parent)
            handle.write(sparse)
        handle.write(b"\0" * data)
        handle.write(vhd_footer(own))


def descriptor(own, parent="", extent=None):
    parent = parent or "00000000-0000-0000-0000-000000000000"
    lines = ["# Disk DescriptorFile", "version=1", "CID=12345678",
             "parentCID=ffffffff", 'createType="monolithicFlat"', ""]
    if extent:
        lines.append(f'RW 16384 FLAT "{extent}" 0')
    lines += ["", 'ddb.virtualHWVersion="4"', f'ddb.uuid.image="{own}"',
              f'ddb.uuid.parent="{parent}"']
    return "\n".join(lines) + "\n"


def write_sparse_vmdk(path, own=OWN, parent=""):
    text = descriptor(own, parent, os.path.basename(path)).encode()
    head = bytearray(512)
    head[:4] = b"KDMV"
    struct.pack_into("<QQ", head, 28, 1, 20)
    body = text.ljust(20 * 512, b"\0")
    with open(path, "wb") as handle:
        handle.write(head + body + b"\0" * 4096)


def touch(path, size=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.truncate(size)


class ReadIdentityTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def path(self, name):
        return os.path.join(self.dir, name)

    def test_vdi_uuid_matches_the_bytes_virtualbox_wrote(self):
        p = self.path("a.vdi")
        write_vdi(p)
        with open(p, "r+b") as handle:
            handle.seek(0x188)
            handle.write(OWN_RAW)
        self.assertEqual(read_image_uuid(p), OWN)

    def test_vdi_differencing_image_reports_its_parent(self):
        p = self.path("child.vdi")
        write_vdi(p, parent=PARENT)
        ident = read_image_identity(p)
        self.assertEqual((ident.fmt, ident.uuid, ident.parent_uuid), ("VDI", OWN, PARENT))

    def test_vdi_with_an_unknown_major_version_is_not_trusted(self):
        p = self.path("old.vdi")
        write_vdi(p, version=0x00000001)
        self.assertIsNone(read_image_identity(p))

    def test_vhd_uuid_uses_virtualbox_byte_order_not_the_spec_order(self):
        p = self.path("fixed.vhd")
        footer = bytearray(vhd_footer(OWN))
        footer[68:84] = PARENT_RAW
        with open(p, "wb") as handle:
            handle.write(b"\0" * 8192 + bytes(footer))
        self.assertEqual(read_image_uuid(p), PARENT)

    def test_fixed_vhd_is_read_from_its_trailing_footer(self):
        p = self.path("fixed.vhd")
        write_vhd(p, dynamic=False)
        self.assertEqual(read_image_identity(p).fmt, "VHD")
        self.assertEqual(read_image_uuid(p), OWN)

    def test_dynamic_vhd_reports_its_parent(self):
        p = self.path("diff.vhd")
        write_vhd(p, parent=PARENT)
        ident = read_image_identity(p)
        self.assertEqual((ident.uuid, ident.parent_uuid), (OWN, PARENT))

    def test_sparse_vmdk_reads_the_embedded_descriptor(self):
        p = self.path("s.vmdk")
        write_sparse_vmdk(p, parent=PARENT)
        ident = read_image_identity(p)
        self.assertEqual((ident.fmt, ident.uuid, ident.parent_uuid), ("VMDK", OWN, PARENT))
        self.assertEqual(missing_extents(p), [], "a sparse VMDK names itself")

    def test_flat_vmdk_descriptor_and_its_missing_extent(self):
        p = self.path("f.vmdk")
        with open(p, "w") as handle:
            handle.write(descriptor(OWN, extent="f-flat.vmdk"))
        self.assertEqual(read_image_uuid(p), OWN)
        self.assertEqual(missing_extents(p), ["f-flat.vmdk"])
        touch(self.path("f-flat.vmdk"), 1024)
        self.assertEqual(missing_extents(p), [])

    def test_files_without_an_identity(self):
        touch(self.path("blank.iso"), 4096)
        with open(self.path("junk.vdi"), "wb") as handle:
            handle.write(os.urandom(2048))
        write_vdi(self.path("null.vdi"), own="00000000-0000-0000-0000-000000000000")
        for name in ("blank.iso", "junk.vdi", "null.vdi", "absent.vdi"):
            self.assertEqual(read_image_uuid(self.path(name)), "", name)
        self.assertIsNone(read_image_identity(self.path("tiny.vdi")))

    def test_format_is_decided_by_content_not_extension(self):
        p = self.path("renamed.img")
        write_vdi(p)
        self.assertEqual(read_image_identity(p).fmt, "VDI")


class FindMovedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def path(self, *parts):
        return os.path.join(self.dir, *parts)

    def test_finds_the_file_by_uuid_whatever_it_is_now_called(self):
        os.makedirs(self.path("new", "deep"))
        write_vdi(self.path("new", "deep", "renamed.vdi"))
        write_vdi(self.path("new", "orig.vdi"), own=OTHER)  # same name, other image
        found = find_moved_image(OWN, "/gone/orig.vdi", [self.dir])
        self.assertEqual([(c.path, c.match) for c in found],
                         [(self.path("new", "deep", "renamed.vdi"), MATCH_UUID)])

    def test_iso_falls_back_to_name_and_size(self):
        touch(self.path("a", "debian.iso"), 3 * 1024 * 1024)
        touch(self.path("b", "debian.iso"), 9 * 1024 * 1024)   # wrong size
        touch(self.path("c", "other.iso"), 3 * 1024 * 1024)    # wrong name
        found = find_moved_image(OWN, "/gone/Debian.iso", [self.dir], expected_mb=3)
        self.assertEqual([(c.path, c.match) for c in found],
                         [(self.path("a", "debian.iso"), MATCH_NAME)])
        self.assertIn("no UUID", found[0].note)
        self.assertEqual(find_moved_image(OWN, "/gone/debian.iso", [self.dir]), [],
                         "with no known size a name alone is not offered")

    def test_known_trash_and_non_recursive(self):
        os.makedirs(self.path("sub"))
        os.makedirs(self.path(TRASH_DIR_NAME))
        write_vdi(self.path("registered.vdi"))
        write_vdi(self.path(TRASH_DIR_NAME, "deleted.vdi"))
        write_vdi(self.path("sub", "deep.vdi"))
        found = find_moved_image(OWN, "/gone/x.vdi", [self.dir],
                                 known=[self.path("registered.vdi")])
        self.assertEqual([c.path for c in found], [self.path("sub", "deep.vdi")])
        self.assertEqual(find_moved_image(OWN, "/gone/x.vdi", [self.dir],
                                          known=[self.path("registered.vdi")],
                                          recursive=False), [])

    def test_uuid_match_sorts_first_and_incomplete_vmdk_after_complete(self):
        touch(self.path("x.iso"), 1024 * 1024)
        with open(self.path("broken.vmdk"), "w") as handle:
            handle.write(descriptor(OWN, extent="broken-flat.vmdk"))
        write_vdi(self.path("z.vdi"))
        found = find_moved_image(OWN.upper(), "/gone/x.iso", [self.dir], expected_mb=1)
        self.assertEqual([os.path.basename(c.path) for c in found],
                         ["z.vdi", "broken.vmdk", "x.iso"])
        self.assertIn("broken-flat.vmdk", found[1].note)

    def test_overlapping_folders_report_a_file_once(self):
        os.makedirs(self.path("sub"))
        write_vdi(self.path("sub", "a.vdi"))
        found = find_moved_image(OWN, "", [self.dir, self.path("sub")])
        self.assertEqual(len(found), 1)

    def test_cancel_stops_the_walk(self):
        write_vdi(self.path("a.vdi"))
        self.assertEqual(find_moved_image(OWN, "", [self.dir], cancelled=lambda: True), [])

    def test_default_folders_nearest_existing_ancestor_then_extra(self):
        os.makedirs(self.path("vms"))
        extra = self.path("images")
        os.makedirs(extra)
        self.assertEqual(
            default_search_folders(self.path("vms", "gone", "deeper", "x.vdi"),
                                   [extra, self.path("vms"), "", "/nowhere"]),
            [self.path("vms"), extra],
        )

    def test_default_folders_never_offer_the_root(self):
        self.assertEqual(default_search_folders("/no-such-dir-vboxfront/x.vdi"), [])
        self.assertEqual(default_search_folders(""), [])


def medium(location, uuid=OWN, **extra):
    base = dict(uuid=uuid, location=location, fmt="VDI", capacity="64 MBytes",
                size_on_disk="2 MBytes", state="inaccessible")
    base.update(extra)
    return MediumRecord(**base)


class LocateDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv[:1])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def test_search_runs_on_open_and_offers_the_match(self):
        os.makedirs(os.path.join(self.dir, "moved"))
        target = os.path.join(self.dir, "moved", "disk.vdi")
        write_vdi(target)
        dlg = LocateMovedDialog(medium(os.path.join(self.dir, "old", "disk.vdi")))
        self.assertEqual(dlg.folder_paths()[0], self.dir)
        self.assertTrue(wait_until(lambda: not dlg.searching()))
        self.assertEqual([c.path for c in dlg.candidates], [target])
        self.assertTrue(dlg.use_btn.isEnabled(), "the first result is preselected")
        dlg._use()
        self.assertEqual(dlg.chosen, target)

    def test_nothing_found_says_so_and_cannot_be_used(self):
        dlg = LocateMovedDialog(medium(os.path.join(self.dir, "gone.vdi")))
        self.assertTrue(wait_until(lambda: not dlg.searching()))
        self.assertEqual(dlg.candidates, [])
        self.assertIn("Nothing found", dlg.status.text())
        self.assertFalse(dlg.use_btn.isEnabled())

    def test_cancel_discards_a_running_search(self):
        dlg = LocateMovedDialog(medium(os.path.join(self.dir, "gone.vdi")), autostart=False)
        dlg.search()
        self.assertTrue(dlg.searching())
        dlg.cancel()
        self.assertFalse(dlg.searching())
        self.assertEqual(dlg.search_btn.text(), "Search")
        for _ in range(20):
            QApplication.processEvents()
        self.assertEqual(dlg.candidates, [], "a cancelled search must not fill results")
        dlg.reject()


class ResolveGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv[:1])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def test_refuses_a_file_carrying_another_uuid(self):
        other = os.path.join(self.dir, "other.vdi")
        write_vdi(other, own=OTHER)
        dlg = ResolveDialog("disk", medium("/gone/orig.vdi"))
        dlg.path_edit.setText(other)
        with mock.patch.object(QMessageBox, "warning") as warn:
            dlg._ok()
        warn.assert_called_once()
        self.assertIn(OTHER, warn.call_args[0][2])
        self.assertIsNone(dlg.command)

    def test_accepts_the_file_carrying_this_uuid_in_braces_or_capitals(self):
        mine = os.path.join(self.dir, "mine.vdi")
        write_vdi(mine)
        dlg = ResolveDialog("disk", medium("/gone/orig.vdi", uuid="{" + OWN.upper() + "}"))
        dlg.path_edit.setText(mine)
        with mock.patch.object(QMessageBox, "warning") as warn:
            dlg._ok()
        warn.assert_not_called()
        self.assertEqual(dlg.command[-2:], ["--setlocation", mine])

    def test_flat_vmdk_without_its_data_file_needs_confirmation(self):
        desc = os.path.join(self.dir, "f.vmdk")
        with open(desc, "w") as handle:
            handle.write(descriptor(OWN, extent="f-flat.vmdk"))
        dlg = ResolveDialog("disk", medium("/gone/f.vmdk", fmt="VMDK"))
        dlg.path_edit.setText(desc)
        with mock.patch.object(QMessageBox, "warning",
                               return_value=QMessageBox.StandardButton.No) as warn:
            dlg._ok()
        self.assertIn("f-flat.vmdk", warn.call_args[0][2])
        self.assertIsNone(dlg.command)
        with mock.patch.object(QMessageBox, "warning",
                               return_value=QMessageBox.StandardButton.Yes):
            dlg._ok()
        self.assertEqual(dlg.command[-1], desc)

    def test_find_fills_the_path_and_follows_the_relocate_choice(self):
        img = os.path.join(self.dir, "x.vdi")
        write_vdi(img)
        dlg = ResolveDialog("dvd", medium("/gone/x.iso", fmt="RAW"), known=["/k"])
        self.assertTrue(dlg.find_btn.isEnabled())

        def fake_exec(finder):
            self.assertEqual(finder._known, ["/k"])
            finder.chosen = "/found/x.iso"
            return 1

        with mock.patch.object(LocateMovedDialog, "exec", fake_exec), \
             mock.patch.object(LocateMovedDialog, "search"):
            dlg._find()
        self.assertEqual(dlg.path_edit.text(), "/found/x.iso")

        dlg = ResolveDialog("disk", medium(img))
        dlg.dryrun_radio.setChecked(True)
        self.assertFalse(dlg.find_btn.isEnabled())


if __name__ == "__main__":
    unittest.main()
