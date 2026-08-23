"""Parser tests against real VBoxManage 7.2.12 output samples."""

import unittest

from vboxfront import (
    CAP_DIFFERENCING,
    child_names,
    disk_backends,
    encryption_word,
    find_attachment,
    find_free_port,
    format_mbytes,
    medium_type_word,
    parse_capacity_mb,
    parse_in_use_entry,
    parse_machinereadable,
    is_differencing,
    is_inaccessible,
    parse_byte_size,
    parse_hddbackends,
    parse_media_list,
    parse_vms_list,
    real_parent_uuid,
    state_word,
    slot_occupant,
    totals_line,
    vm_storage_controllers,
)

HDDS_LONG = """\
UUID:           a8438587-84e2-480b-9a0b-b23227c77268
Parent UUID:    base
State:          created
Type:           normal (base)
Location:       /tmp/scratch/smokeB.vmdk
Storage format: VMDK
Format variant: fixed default
Capacity:       32 MBytes
Size on disk:   32 MBytes
Encryption:     disabled
Property:       BootSector=
                Partitions=
                RawDrive=
                Relative=

UUID:           7894924d-30ae-41ec-a640-98cdba451f17
Parent UUID:    base
State:          created
Type:           normal (base)
Location:       /tmp/scratch/smokeA.vdi
Storage format: VDI
Format variant: dynamic default
Capacity:       64 MBytes
Size on disk:   2 MBytes
Encryption:     disabled
Property:       AllocationBlockSize=1048576
In use by VMs:  vbf-smoke (UUID: 24aa0bbd-3d9c-4ba3-a41f-273c0ab57661)
"""

HDDS_MULTI_USE = """\
UUID:           11111111-1111-1111-1111-111111111111
Parent UUID:    base
State:          created
Type:           multiattach
Location:       /vms/shared.vdi
Storage format: VDI
Capacity:       1024 MBytes
Size on disk:   100 MBytes
In use by VMs:  alpha (UUID: 22222222-2222-2222-2222-222222222222) [snap1 (UUID: 33333333-3333-3333-3333-333333333333)]
                beta (UUID: 44444444-4444-4444-4444-444444444444)
"""

HDDS_CHAIN = """\
UUID:           aaaaaaaa-0000-0000-0000-000000000000
Parent UUID:    base
State:          created
Type:           normal (base)
Location:       /vms/base.vdi
Storage format: VDI
Capacity:       10240 MBytes
Size on disk:   500 MBytes

UUID:           bbbbbbbb-0000-0000-0000-000000000000
Parent UUID:    aaaaaaaa-0000-0000-0000-000000000000
State:          inaccessible
Type:           normal (differencing)
Location:       /vms/child.vdi
Storage format: VDI
Capacity:       10240 MBytes
Size on disk:   12 MBytes
"""

VMS_LIST = """\
"vbf-smoke" {24aa0bbd-3d9c-4ba3-a41f-273c0ab57661}
"my server (prod)" {99999999-9999-9999-9999-999999999999}
"""

VMINFO = """\
name="vbf-smoke"
UUID="24aa0bbd-3d9c-4ba3-a41f-273c0ab57661"
boot4="none"
storagecontrollername0="SATA Controller"
storagecontrollertype0="IntelAhci"
storagecontrollerinstance0="0"
storagecontrollermaxportcount0="30"
storagecontrollerportcount0="4"
storagecontrollerbootable0="on"
"SATA Controller-0-0"="none"
"SATA Controller-1-0"="/tmp/scratch/smokeA.vdi"
"SATA Controller-ImageUUID-1-0"="7894924d-30ae-41ec-a640-98cdba451f17"
"SATA Controller-nonrotational-1-0"="off"
"SATA Controller-discard-1-0"="off"
"SATA Controller-2-0"="none"
"SATA Controller-3-0"="none"
nic1="none"
rec_screen_video_rate_kbps=512
"""


# Real `list -l hdds` output for a medium whose description spans several
# lines: VBoxManage prints the continuations flush left, so they are
# indistinguishable from fields by shape alone — and one of them here is a
# verbatim "UUID:" line, which used to become the record's UUID.
HDDS_MULTILINE_DESCRIPTION = """\
UUID:           b662f6e9-820f-4fac-af56-39b5d52e53a2
Parent UUID:    base
State:          created
Description:    notes
UUID:           ffffffff-ffff-ffff-ffff-ffffffffffff

second paragraph
Type:           normal (base)
Location:       /vms/probe.vdi
Storage format: VDI
Format variant: dynamic default
Capacity:       16 MBytes
Size on disk:   2 MBytes
Encryption:     disabled
Property:       AllocationBlockSize=1048576

UUID:           aaaaaaaa-1111-1111-1111-111111111111
Parent UUID:    base
State:          created
Type:           normal (base)
Location:       /vms/next.vdi
Storage format: VDI
Capacity:       32 MBytes
"""

# An encrypted medium's CRYPT/KeyStore property is base64 wrapped at column 0.
HDDS_ENCRYPTED = """\
UUID:           b662f6e9-820f-4fac-af56-39b5d52e53a2
Parent UUID:    base
State:          created
Type:           normal (base)
Location:       /vms/secret.vdi
Storage format: VDI
Capacity:       16 MBytes
Size on disk:   2 MBytes
Encryption:     enabled
Cipher:         AES-XTS256-PLAIN64
Password ID:    probeid
Property:       AllocationBlockSize=1048576
                CRYPT/KeyId=probeid
                CRYPT/KeyStore=U0NORQABQUVTLVhUUzI1Ni1QTEFJTjY0AAAAAAAAAAAAAAAAAABQQktERjItU0hB
MjU2AAAAAAAAAAAAAAAAAAAAAAAAAEAAAACI9H8zjVNVkVXlj+O+WUKJODlrRFMF
OSx4W5i4xqenHw==
"""


class ParseMediaListTest(unittest.TestCase):
    def test_long_listing(self):
        records = parse_media_list(HDDS_LONG)
        self.assertEqual(len(records), 2)
        vmdk, vdi = records
        self.assertEqual(vmdk.uuid, "a8438587-84e2-480b-9a0b-b23227c77268")
        self.assertEqual(vmdk.fmt, "VMDK")
        self.assertEqual(vmdk.variant, "fixed default")
        self.assertEqual(vmdk.size_on_disk, "32 MBytes")
        self.assertEqual(vmdk.in_use, [])
        self.assertEqual(vdi.capacity, "64 MBytes")
        self.assertEqual(vdi.state, "created")
        self.assertEqual(vdi.encryption, "disabled")
        self.assertEqual(vdi.in_use, ["vbf-smoke (UUID: 24aa0bbd-3d9c-4ba3-a41f-273c0ab57661)"])

    def test_property_continuation_does_not_leak_keys(self):
        # The wrapped "Partitions=" lines must not become bogus records/fields.
        records = parse_media_list(HDDS_LONG)
        self.assertEqual(records[0].location, "/tmp/scratch/smokeB.vmdk")

    def test_multiple_vm_users_on_continuation_lines(self):
        (rec,) = parse_media_list(HDDS_MULTI_USE)
        self.assertEqual(len(rec.in_use), 2)
        self.assertEqual(
            parse_in_use_entry(rec.in_use[0]),
            ("alpha", "22222222-2222-2222-2222-222222222222"),
        )
        self.assertEqual(
            parse_in_use_entry(rec.in_use[1]),
            ("beta", "44444444-4444-4444-4444-444444444444"),
        )

    def test_parent_uuid_and_state(self):
        base, child = parse_media_list(HDDS_CHAIN)
        self.assertEqual(base.parent_uuid, "base")
        self.assertEqual(child.parent_uuid, base.uuid)
        self.assertEqual(child.state, "inaccessible")

    def test_short_listing_still_parses(self):
        short = (
            "UUID:           123\n"
            "Parent UUID:    base\n"
            "Location:       /a.vdi\n"
            "Storage format: VDI\n"
            "Capacity:       512 MBytes\n"
        )
        (rec,) = parse_media_list(short)
        self.assertEqual(rec.size_on_disk, "")
        self.assertEqual(rec.fmt, "VDI")

    def test_multiline_description_keeps_the_real_uuid(self):
        # A "UUID:" line inside the description must not retarget the record:
        # every operation (compact, resize, closemedium --delete) uses it.
        base, nxt = parse_media_list(HDDS_MULTILINE_DESCRIPTION)
        self.assertEqual(base.uuid, "b662f6e9-820f-4fac-af56-39b5d52e53a2")
        self.assertEqual(base.location, "/vms/probe.vdi")
        self.assertEqual(base.capacity, "16 MBytes")
        self.assertEqual(nxt.uuid, "aaaaaaaa-1111-1111-1111-111111111111")

    def test_multiline_description_survives_a_blank_line(self):
        # The blank line used to end the block, dropping the medium entirely.
        base, _nxt = parse_media_list(HDDS_MULTILINE_DESCRIPTION)
        self.assertEqual(
            base.description,
            "notes\nUUID:           ffffffff-ffff-ffff-ffff-ffffffffffff"
            "\n\nsecond paragraph",
        )

    def test_unindented_base64_property_wrap(self):
        (rec,) = parse_media_list(HDDS_ENCRYPTED)
        self.assertEqual(rec.uuid, "b662f6e9-820f-4fac-af56-39b5d52e53a2")
        self.assertEqual(rec.encryption, "enabled")
        self.assertEqual(rec.size_on_disk, "2 MBytes")

    def test_values_have_no_separator_newlines(self):
        base, child = parse_media_list(HDDS_CHAIN)
        self.assertEqual(base.size_on_disk, "500 MBytes")
        self.assertEqual(child.state, "inaccessible")

    def test_stderr_noise_does_not_swallow_the_first_record(self):
        # Both runners merge stderr into stdout, so warnings land in the text.
        noisy = "WARNING: The vboxdrv kernel module is not loaded.\n" + HDDS_CHAIN
        records = parse_media_list(noisy)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].location, "/vms/base.vdi")

    def test_empty_output(self):
        self.assertEqual(parse_media_list(""), [])
        self.assertEqual(parse_media_list("\n\n"), [])


class SizeHelpersTest(unittest.TestCase):
    def test_parse_capacity(self):
        self.assertEqual(parse_capacity_mb("64 MBytes"), 64)
        self.assertEqual(parse_capacity_mb("  10240 MBytes"), 10240)
        self.assertIsNone(parse_capacity_mb(""))
        self.assertIsNone(parse_capacity_mb("unknown"))

    def test_format_mbytes(self):
        self.assertEqual(format_mbytes(None), "")
        self.assertEqual(format_mbytes(64), "64 MB")
        self.assertEqual(format_mbytes(1536), "1.5 GB")
        self.assertEqual(format_mbytes(10240), "10.0 GB")
        self.assertEqual(format_mbytes(2 * 1024 * 1024), "2.00 TB")

    def test_medium_type_word(self):
        self.assertEqual(medium_type_word("normal (base)"), "normal")
        self.assertEqual(medium_type_word("multiattach"), "multiattach")
        self.assertEqual(medium_type_word(""), "")


class EncryptionAndTotalsTest(unittest.TestCase):
    def test_cipher_and_password_id_are_captured(self):
        (rec,) = parse_media_list(HDDS_ENCRYPTED)
        self.assertEqual(rec.encryption, "enabled")
        self.assertEqual(rec.cipher, "AES-XTS256-PLAIN64")
        self.assertEqual(rec.password_id, "probeid")
        self.assertEqual(encryption_word(rec), "AES-XTS256-PLAIN64")

    def test_encryption_word_is_blank_when_not_encrypted(self):
        base, _child = parse_media_list(HDDS_CHAIN)
        self.assertEqual(base.encryption, "")
        self.assertEqual(encryption_word(base), "")
        vmdk, _vdi = parse_media_list(HDDS_LONG)
        self.assertEqual(vmdk.encryption, "disabled")
        self.assertEqual(encryption_word(vmdk), "", "'disabled' on every row is noise")

    def test_encryption_word_falls_back_to_the_state(self):
        rec = parse_media_list(HDDS_ENCRYPTED)[0]
        rec.cipher = ""
        self.assertEqual(encryption_word(rec), "enabled")

    def test_totals_line_reports_provisioning(self):
        records = parse_media_list(HDDS_CHAIN)
        line = totals_line(records)
        self.assertIn("2 media", line)
        self.assertIn("20.0 GB provisioned", line)   # 2 x 10240 MB
        self.assertIn("512 MB on disk", line)        # 500 + 12
        self.assertIn("2% allocated", line)
        # Never claims the unallocated remainder is reclaimable.
        self.assertNotIn("reclaim", line.lower())

    def test_totals_line_counts_filtered_rows_and_unknowns(self):
        records = parse_media_list(HDDS_CHAIN)
        self.assertIn("(1 shown)", totals_line(records, shown=1))
        records[0].size_on_disk = ""
        self.assertIn("1 unknown", totals_line(records))

    def test_totals_line_when_empty(self):
        self.assertEqual(totals_line([]), "No media.")


HDDBACKENDS = """\
Supported hard disk backends:

Backend 0: id='VMDK' description='VMDK' capabilities=0x0a7f extensions='vmdk (HardDisk)' properties=(
  name='RawDrive' desc='' type=string flags=0x00 default='', 
  name='Partitions' desc='' type=string flags=0x00 default='', 
  name='BootSector' desc='' type=byte flags=0x00 default='', 
  name='Relative' desc='' type=int flags=0x00 default='')
Backend 1: id='VDI' description='VDI' capabilities=0x0e77 extensions='vdi (HardDisk)' properties=(
  name='AllocationBlockSize' desc='' type=int flags=0x04 default='1048576')
Backend 2: id='VHD' description='VHD' capabilities=0x0a77 extensions='vhd (HardDisk)' properties=()
Backend 3: id='Parallels' description='Parallels' capabilities=0x0274 extensions='hdd (HardDisk)' properties=()
Backend 4: id='DMG' description='DMG' capabilities=0x0240 extensions='dmg (DVD)' properties=()
Backend 7: id='VHDX' description='VHDX' capabilities=0x0240 extensions='vhdx (HardDisk)' properties=()
Backend 10: id='RAW' description='RAW' capabilities=0x0262 extensions='iso (DVD),cdr (DVD),img (Floppy),ima (Floppy)' properties=()
Backend 11: id='iSCSI' description='iSCSI' capabilities=0x01a0 extensions='' properties=(
  name='TargetName' desc='' type=string flags=0x01 default='', 
  name='LUN' desc='' type=string flags=0x01 default='0')
"""


class HddBackendsTest(unittest.TestCase):
    def setUp(self):
        self.backends = parse_hddbackends(HDDBACKENDS)
        self.by_id = {b.id: b for b in self.backends}

    def test_all_backends_parsed(self):
        self.assertEqual(
            [b.id for b in self.backends],
            ["VMDK", "VDI", "VHD", "Parallels", "DMG", "VHDX", "RAW", "iSCSI"],
        )

    def test_creatable_disk_formats_exclude_read_only_and_non_disk(self):
        # DMG/VHDX have no create bit; RAW registers no HardDisk extension,
        # which is exactly why it must never be offered as a disk target.
        self.assertEqual(
            [b.id for b in disk_backends(self.backends)],
            ["VMDK", "VDI", "VHD", "Parallels"],
        )

    def test_variants_follow_the_capability_bits(self):
        self.assertEqual(self.by_id["VMDK"].variants(), ["Standard", "Fixed", "Split2G"])
        self.assertEqual(self.by_id["VDI"].variants(), ["Standard", "Fixed"])
        self.assertEqual(self.by_id["Parallels"].variants(), ["Standard"])

    def test_extensions(self):
        self.assertEqual(self.by_id["VMDK"].extension_for("HardDisk"), ".vmdk")
        self.assertEqual(self.by_id["Parallels"].extension_for("HardDisk"), ".hdd")
        self.assertEqual(self.by_id["RAW"].extension_for("HardDisk"), "")
        self.assertEqual(self.by_id["RAW"].extension_for("Floppy"), ".img")

    def test_differencing_capability(self):
        self.assertTrue(self.by_id["VDI"].capabilities & CAP_DIFFERENCING)
        self.assertFalse(self.by_id["RAW"].capabilities & CAP_DIFFERENCING)

    def test_property_schema_is_captured(self):
        self.assertEqual(
            [p["name"] for p in self.by_id["VMDK"].properties],
            ["RawDrive", "Partitions", "BootSector", "Relative"],
        )
        (alloc,) = self.by_id["VDI"].properties
        self.assertEqual(alloc["type"], "int")
        self.assertEqual(alloc["default"], "1048576")

    def test_multiline_properties_do_not_swallow_the_next_backend(self):
        self.assertEqual(len(self.by_id["iSCSI"].properties), 2)
        self.assertEqual(self.by_id["VDI"].id, "VDI")

    def test_empty_and_garbage_input(self):
        self.assertEqual(parse_hddbackends(""), [])
        self.assertEqual(parse_hddbackends("no backends here\n"), [])


# Real `list -l hdds` for a medium whose file has vanished. VBoxManage adds an
# "Access Error:" field plus an unindented "VD: error ..." continuation, and
# both used to fold into State — which silently disabled the inaccessible
# highlight and let compact-all try to compact a broken disk.
HDDS_INACCESSIBLE = """\
UUID:           6c3768af-c821-4218-afca-17377e656665
Parent UUID:    base
State:          inaccessible
Access Error:   Could not open the medium '/vms/gone.vdi'.
VD: error VERR_FILE_NOT_FOUND opening image file '/vms/gone.vdi' (VERR_FILE_NOT_FOUND)
Type:           normal (base)
Location:       /vms/gone.vdi
Storage format: VDI
Format variant: dynamic default
Capacity:       0 MBytes
Size on disk:   0 MBytes
Encryption:     disabled
Property:       AllocationBlockSize=
"""


class InaccessibleMediumTest(unittest.TestCase):
    def setUp(self):
        (self.rec,) = parse_media_list(HDDS_INACCESSIBLE)

    def test_state_is_only_the_state(self):
        self.assertEqual(self.rec.state, "inaccessible")
        self.assertEqual(state_word(self.rec), "inaccessible")

    def test_access_error_is_its_own_field(self):
        self.assertIn("Could not open the medium", self.rec.access_error)
        self.assertIn("VERR_FILE_NOT_FOUND", self.rec.access_error)
        self.assertEqual(self.rec.location, "/vms/gone.vdi")
        self.assertEqual(self.rec.fmt, "VDI")

    def test_is_inaccessible(self):
        self.assertTrue(is_inaccessible(self.rec))
        base, _child = parse_media_list(HDDS_CHAIN)
        self.assertFalse(is_inaccessible(base))

    def test_state_word_survives_an_unknown_trailing_block(self):
        # Insurance for any future field VBoxManage prints flush left there.
        rec = self.rec
        rec.state = "inaccessible\nSomething Else:  surprise"
        self.assertEqual(state_word(rec), "inaccessible")
        self.assertTrue(is_inaccessible(rec))


class MediumHelpersTest(unittest.TestCase):
    def test_child_names(self):
        base, child = parse_media_list(HDDS_CHAIN)
        self.assertEqual(child_names([base, child], base.uuid), ["child.vdi"])
        self.assertEqual(child_names([base, child], child.uuid), [])

    def test_child_names_ignores_the_base_placeholder(self):
        # Every base medium reports `Parent UUID: base`; that literal must
        # never match as a real parent.
        base, child = parse_media_list(HDDS_CHAIN)
        self.assertEqual(real_parent_uuid(base), "")
        self.assertEqual(real_parent_uuid(child), base.uuid)
        self.assertEqual(child_names([base, child], "base"), [])
        self.assertEqual(child_names([base, child], ""), [])

    def test_is_differencing(self):
        base, child = parse_media_list(HDDS_CHAIN)
        self.assertFalse(is_differencing(base))
        self.assertTrue(is_differencing(child))


class ByteSizeTest(unittest.TestCase):
    def test_decimal_hex_and_suffixes(self):
        self.assertEqual(parse_byte_size("4096"), 4096)
        self.assertEqual(parse_byte_size("0x1000"), 4096)
        self.assertEqual(parse_byte_size("8K"), 8192)
        self.assertEqual(parse_byte_size("1.5M"), 1572864)
        self.assertEqual(parse_byte_size(" 2 g "), 2 * 1024 ** 3)
        self.assertEqual(parse_byte_size("512b"), 512)

    def test_rejects_nonsense(self):
        for bad in ("", "   ", "abc", "0x", "1..5M", "-4096", "4096 sectors"):
            self.assertIsNone(parse_byte_size(bad), bad)



class VmParsingTest(unittest.TestCase):
    def test_parse_vms_list(self):
        vms = parse_vms_list(VMS_LIST)
        self.assertEqual(vms[0], ("vbf-smoke", "24aa0bbd-3d9c-4ba3-a41f-273c0ab57661"))
        self.assertEqual(vms[1][0], "my server (prod)")

    def test_parse_vms_list_ignores_noise(self):
        self.assertEqual(parse_vms_list("garbage\n\n"), [])

    def test_machinereadable_quotes(self):
        info = parse_machinereadable(VMINFO)
        self.assertEqual(info["storagecontrollername0"], "SATA Controller")
        self.assertEqual(info["SATA Controller-1-0"], "/tmp/scratch/smokeA.vdi")
        self.assertEqual(info["rec_screen_video_rate_kbps"], "512")

    def test_storage_controllers(self):
        info = parse_machinereadable(VMINFO)
        self.assertEqual(vm_storage_controllers(info), [("SATA Controller", 4)])

    def test_find_attachment(self):
        info = parse_machinereadable(VMINFO)
        slot = find_attachment(info, "7894924D-30AE-41EC-A640-98CDBA451F17")
        self.assertEqual(slot, ("SATA Controller", 1, 0))

    def test_find_attachment_ignores_per_slot_flags(self):
        # "SATA Controller-nonrotational-1-0" must never match as a slot.
        info = parse_machinereadable(VMINFO)
        self.assertIsNone(find_attachment(info, "off"))

    def test_find_free_port(self):
        info = parse_machinereadable(VMINFO)
        self.assertEqual(find_free_port(info, "SATA Controller", 4), 0)
        info["SATA Controller-0-0"] = "/some.vdi"
        self.assertEqual(find_free_port(info, "SATA Controller", 4), 2)

    def test_slot_occupant(self):
        info = parse_machinereadable(VMINFO)
        self.assertEqual(slot_occupant(info, "SATA Controller", 0, 0), "")
        self.assertEqual(
            slot_occupant(info, "SATA Controller", 1, 0), "/tmp/scratch/smokeA.vdi"
        )
        # Unknown slots read as empty, never as occupied by "".
        self.assertEqual(slot_occupant(info, "SATA Controller", 9, 0), "")

    def test_find_free_port_falls_back_on_a_full_controller(self):
        info = parse_machinereadable(VMINFO)
        for port in range(4):
            info[f"SATA Controller-{port}-0"] = f"/disk{port}.vdi"
        # The fallback is port 0 and it is *not* free — callers must check.
        self.assertEqual(find_free_port(info, "SATA Controller", 4), 0)
        self.assertTrue(slot_occupant(info, "SATA Controller", 0, 0))


if __name__ == "__main__":
    unittest.main()
