"""Parser tests against real VBoxManage 7.2.12 output samples."""

import unittest

from vboxfront import (
    find_attachment,
    find_free_port,
    format_mbytes,
    medium_type_word,
    parse_capacity_mb,
    parse_in_use_entry,
    parse_machinereadable,
    parse_media_list,
    parse_vms_list,
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


if __name__ == "__main__":
    unittest.main()
