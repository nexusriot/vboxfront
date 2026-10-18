"""Parser tests against real VBoxManage 7.2.12 output samples."""

import json
import os
import tempfile
import unittest

from vboxfront import (
    CAP_DIFFERENCING,
    MediumRecord,
    is_destructive_command,
    snapshot_only,
    discard_findings,
    parse_uuid_collision,
    parse_filter,
    record_matches,
    medium_details,
    plan_script,
    eta_text,
    format_duration,
    registry_fingerprint,
    registered_at,
    allocation_estimate,
    controller_types,
    format_bytes,
    free_bytes_for,
    health_findings,
    health_report,
    max_device_index,
    parse_host_drives,
    parse_in_use_snapshot,
    reclaim_line,
    records_to_csv,
    records_to_json,
    records_to_tsv,
    TRASH_DIR_NAME,
    scan_for_images,
    snapshot_word,
    space_warning,
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


SNAPSHOT_LISTING = """\
UUID:           11111111-1111-1111-1111-111111111111
Parent UUID:    base
State:          created
Type:           normal (base)
Location:       /vms/snap.vdi
Storage format: VDI
Capacity:       1024 MBytes
Size on disk:   64 MBytes
Property:       AllocationBlockSize=1048576
In use by VMs:  win11 (UUID: 22222222-2222-2222-2222-222222222222) [Before update (UUID: 33333333-3333-3333-3333-333333333333)]
"""


class SnapshotParsingTest(unittest.TestCase):
    def test_snapshot_name_comes_out_of_the_listing(self):
        # No `snapshot list` call is needed: the name is already there.
        entry = ("win11 (UUID: 22222222-2222-2222-2222-222222222222) "
                 "[Before update (UUID: 33333333-3333-3333-3333-333333333333)]")
        self.assertEqual(parse_in_use_snapshot(entry), "Before update")
        self.assertEqual(parse_in_use_entry(entry)[0], "win11")

    def test_a_snapshot_name_may_contain_brackets(self):
        entry = ("vm (UUID: 22222222-2222-2222-2222-222222222222) "
                 "[clean (v2) [rc] (UUID: 33333333-3333-3333-3333-333333333333)]")
        self.assertEqual(parse_in_use_snapshot(entry), "clean (v2) [rc]")

    def test_an_attachment_without_a_snapshot_reports_none(self):
        self.assertEqual(
            parse_in_use_snapshot("vm (UUID: 22222222-2222-2222-2222-222222222222)"),
            "",
        )

    def test_snapshot_word_deduplicates(self):
        rec = parse_media_list(SNAPSHOT_LISTING)[0]
        rec.in_use = rec.in_use * 2
        self.assertEqual(snapshot_word(rec), "Before update")

    def test_properties_are_captured(self):
        rec = parse_media_list(SNAPSHOT_LISTING)[0]
        self.assertEqual(rec.properties, ["AllocationBlockSize=1048576"])

    def test_wrapped_property_block_yields_one_entry_per_property(self):
        # The long listing's VMDK carries four properties; a repeat of the same
        # label continues the field rather than opening a new one, so the label
        # has to be trimmed back off each continuation.
        vmdk = next(r for r in parse_media_list(HDDS_LONG) if r.fmt == "VMDK")
        self.assertIn("BootSector=", vmdk.properties)
        self.assertTrue(all(not p.startswith("Property:") for p in vmdk.properties))


class HostDriveTest(unittest.TestCase):
    def test_names_are_taken_from_the_listing(self):
        text = ("UUID:         aaaa\nName:         /dev/sr0\n\n"
                "UUID:         bbbb\nName:         /dev/sr1\n")
        self.assertEqual(parse_host_drives(text), ["/dev/sr0", "/dev/sr1"])

    def test_no_drives_is_not_an_error(self):
        # `list hostdvds` is empty on a machine with no optical drive, and on
        # one where this user cannot enumerate them.
        self.assertEqual(parse_host_drives(""), [])


class ControllerTypeTest(unittest.TestCase):
    def test_types_are_paired_with_names(self):
        info = parse_machinereadable(
            'storagecontrollername0="IDE"\nstoragecontrollertype0="PIIX4"\n'
            'storagecontrollername1="SATA"\nstoragecontrollertype1="IntelAhci"\n'
        )
        self.assertEqual(controller_types(info), {"IDE": "PIIX4", "SATA": "IntelAhci"})

    def test_only_ide_and_floppy_have_a_second_device(self):
        for ctype in ("PIIX3", "PIIX4", "ICH6", "I82078", "piix4"):
            self.assertEqual(max_device_index(ctype), 1, ctype)
        for ctype in ("IntelAhci", "LsiLogic", "USB", "NVMe", "VirtioSCSI", ""):
            self.assertEqual(max_device_index(ctype), 0, ctype)


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.records = parse_media_list(SNAPSHOT_LISTING)

    def test_csv_has_a_header_and_numeric_sizes(self):
        lines = records_to_csv(self.records).splitlines()
        self.assertTrue(lines[0].startswith("name,format,capacity_mb,size_on_disk_mb"))
        # Numbers, not "1.0 GB": the point of exporting is to add them up.
        self.assertIn(",1024,64,", lines[1])

    def test_csv_quotes_a_value_containing_the_separator(self):
        self.records[0].description = "a, b"
        self.records[0].location = "/vms/a,b.vdi"
        self.assertIn('"/vms/a,b.vdi"', records_to_csv(self.records))

    def test_json_round_trips(self):
        rows = json.loads(records_to_json(self.records))
        self.assertEqual(rows[0]["uuid"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(rows[0]["snapshot"], "Before update")
        self.assertEqual(rows[0]["capacity_mb"], 1024)

    def test_tsv_flattens_embedded_tabs_and_newlines(self):
        self.records[0].access_error = "line one\nline two"
        body = records_to_tsv(self.records).splitlines()[1]
        self.assertIn("line one line two", body)
        self.assertEqual(len(body.split("\t")),
                         len(records_to_tsv(self.records).splitlines()[0].split("\t")))

    def test_missing_sizes_export_as_empty_not_none(self):
        self.records[0].capacity = ""
        self.assertNotIn("None", records_to_csv(self.records))


class SpaceTest(unittest.TestCase):
    def test_format_bytes(self):
        self.assertEqual(format_bytes(512), "512 B")
        self.assertEqual(format_bytes(1536), "1.5 KB")
        self.assertEqual(format_bytes(3 * 1024 ** 3), "3.0 GB")
        self.assertEqual(format_bytes(None), "")

    def test_free_space_walks_up_to_a_folder_that_exists(self):
        with tempfile.TemporaryDirectory() as d:
            deep = os.path.join(d, "not", "there", "yet", "disk.vdi")
            self.assertIsInstance(free_bytes_for(deep), int)

    def test_only_a_fixed_image_has_to_fit_whole(self):
        self.assertGreater(allocation_estimate(1024, "Fixed"), 1024 * 1024 ** 2)
        self.assertLessEqual(allocation_estimate(1024, "Standard"), 8 * 1024 ** 2)

    def test_a_fitting_target_produces_no_warning(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(space_warning(os.path.join(d, "x.vdi"), 1024), "")

    def test_an_impossible_target_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            warning = space_warning(os.path.join(d, "x.vdi"), 10 ** 18)
            self.assertIn("free", warning)

    def test_nothing_needed_is_never_a_warning(self):
        self.assertEqual(space_warning("/nonexistent/x.vdi", 0), "")


class OrphanScanTest(unittest.TestCase):
    def test_only_unknown_images_are_reported(self):
        with tempfile.TemporaryDirectory() as d:
            known = os.path.join(d, "known.vdi")
            orphan = os.path.join(d, "sub", "orphan.vmdk")
            iso = os.path.join(d, "boot.iso")
            os.makedirs(os.path.dirname(orphan))
            for path in (known, orphan, iso, os.path.join(d, "notes.txt")):
                with open(path, "w") as f:
                    f.write("x")
            found = scan_for_images([d], [known])
            self.assertEqual(found, [("dvd", iso), ("disk", orphan)]
                             if iso < orphan else [("disk", orphan), ("dvd", iso)])

    def test_a_symlinked_duplicate_is_not_an_orphan(self):
        # Comparing real paths keeps a symlinked VM folder from reporting every
        # disk in it a second time.
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "real.vdi")
            with open(real, "w") as f:
                f.write("x")
            os.symlink(real, os.path.join(d, "link.vdi"))
            self.assertEqual(scan_for_images([d], [real]), [])

    def test_subfolders_can_be_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "sub"))
            with open(os.path.join(d, "sub", "deep.vdi"), "w") as f:
                f.write("x")
            self.assertEqual(scan_for_images([d], [], recursive=False), [])
            self.assertEqual(len(scan_for_images([d], [], recursive=True)), 1)

    def test_the_delete_trash_is_not_scanned(self):
        # Offering to re-adopt an image the user has just deleted is the
        # opposite of what this scan is for.
        with tempfile.TemporaryDirectory() as d:
            trash = os.path.join(d, TRASH_DIR_NAME)
            os.makedirs(trash)
            with open(os.path.join(trash, "deleted.vdi"), "w") as f:
                f.write("x")
            with open(os.path.join(d, "live.vdi"), "w") as f:
                f.write("x")
            self.assertEqual(scan_for_images([d], []),
                             [("disk", os.path.join(d, "live.vdi"))])

    def test_a_missing_folder_is_skipped_quietly(self):
        self.assertEqual(scan_for_images(["/nonexistent-folder"], []), [])


class UuidCollisionTest(unittest.TestCase):
    """Why an image sits unregistered, in VBoxManage's own words.

    Captured verbatim from 7.2.12 by copying a registered .vdi and asking it
    to open the copy — the exact situation Image UUID tools exists for.
    """

    REFUSAL = (
        "VBoxManage: error: Cannot register the hard disk "
        "'/vms/copy.vdi' {c28bb89a-3214-4853-bf87-e26ceb0fdb31} because a "
        "hard disk '/vms/probe.vdi' with UUID "
        "{c28bb89a-3214-4853-bf87-e26ceb0fdb31} already exists\n"
        "VBoxManage: error: Details: code NS_ERROR_INVALID_ARG\n"
    )

    def test_the_conflicting_file_and_uuid_are_named(self):
        self.assertEqual(
            parse_uuid_collision(self.REFUSAL),
            ("/vms/probe.vdi", "c28bb89a-3214-4853-bf87-e26ceb0fdb31"),
        )

    def test_a_dvd_refusal_parses_too(self):
        text = ("Cannot register the DVD image '/iso/copy.iso' {1} because a "
                "DVD image '/iso/first.iso' with UUID "
                "{11111111-2222-3333-4444-555555555555} already exists")
        self.assertEqual(
            parse_uuid_collision(text),
            ("/iso/first.iso", "11111111-2222-3333-4444-555555555555"),
        )

    def test_a_clean_open_is_not_a_collision(self):
        self.assertIsNone(parse_uuid_collision("UUID: 1234\nLocation: /x.vdi"))

    def test_an_unrelated_error_is_not_a_collision(self):
        self.assertIsNone(parse_uuid_collision(
            "VBoxManage: error: Could not find file for the medium '/x.vdi'"
        ))


class StructuredFilterTest(unittest.TestCase):
    """`fmt:vdi size:>10G vm:alpha` alongside plain substring matching."""

    def _record(self, **over):
        base = dict(uuid="aaaa-1", location="/vms/web.vdi", fmt="VDI",
                    capacity="20480 MBytes", size_on_disk="15000 MBytes",
                    medium_type="normal (base)", state="created",
                    in_use=["alpha (UUID: 11111111-1111-1111-1111-111111111111)"])
        base.update(over)
        return MediumRecord(**base)

    def _cells(self, rec):
        return [os.path.basename(rec.location), rec.fmt, rec.capacity]

    def test_terms_and_plain_words_are_separated(self):
        terms, words = parse_filter("fmt:vdi size:>10G backup")
        self.assertEqual(terms, [("fmt", "", "vdi"), ("size", ">", "10G")])
        self.assertEqual(words, ["backup"])

    def test_an_unknown_field_stays_a_plain_word(self):
        # A pasted Windows path must keep filtering the way it always did.
        terms, words = parse_filter("C:/VMs/disk.vdi")
        self.assertEqual(terms, [])
        self.assertEqual(words, ["c:/vms/disk.vdi"])

    def test_every_term_has_to_match(self):
        rec = self._record()
        terms, words = parse_filter("fmt:vdi vm:alpha")
        self.assertTrue(record_matches(rec, terms, words, self._cells(rec)))
        terms, words = parse_filter("fmt:vdi vm:beta")
        self.assertFalse(record_matches(rec, terms, words, self._cells(rec)))

    def test_size_comparisons_use_the_allocated_size(self):
        rec = self._record()
        for query, expected in (("size:>10G", True), ("size:>20G", False),
                                ("size:<20G", True), ("size:<1G", False)):
            terms, words = parse_filter(query)
            self.assertEqual(
                record_matches(rec, terms, words, self._cells(rec)),
                expected, query,
            )

    def test_capacity_is_its_own_field(self):
        rec = self._record()
        terms, words = parse_filter("capacity:>19G")
        self.assertTrue(record_matches(rec, terms, words, self._cells(rec)))
        terms, words = parse_filter("size:>19G")
        self.assertFalse(record_matches(rec, terms, words, self._cells(rec)))

    def test_an_unreadable_size_fails_a_size_term(self):
        # "size:>10G" should not quietly include a medium whose size is
        # unknown, which is exactly the inaccessible ones.
        rec = self._record(size_on_disk="")
        terms, words = parse_filter("size:>10G")
        self.assertFalse(record_matches(rec, terms, words, self._cells(rec)))

    def test_a_plain_word_still_matches_any_cell(self):
        rec = self._record()
        terms, words = parse_filter("web")
        self.assertTrue(record_matches(rec, terms, words, self._cells(rec)))
        terms, words = parse_filter("absent")
        self.assertFalse(record_matches(rec, terms, words, self._cells(rec)))

    def test_an_empty_filter_matches_everything(self):
        rec = self._record()
        terms, words = parse_filter("")
        self.assertTrue(record_matches(rec, terms, words, self._cells(rec)))

    def test_state_and_type_are_filterable(self):
        rec = self._record(state="inaccessible")
        terms, words = parse_filter("state:inaccessible")
        self.assertTrue(record_matches(rec, terms, words, self._cells(rec)))
        terms, words = parse_filter("type:normal")
        self.assertTrue(record_matches(rec, terms, words, self._cells(rec)))


class MediumDetailsTest(unittest.TestCase):
    def test_the_rows_come_from_the_record_with_no_command(self):
        (rec,) = parse_media_list(HDDS_ENCRYPTED)
        rows = dict(medium_details(rec))
        self.assertEqual(rows["UUID"], rec.uuid)
        self.assertEqual(rows["Location"], "/vms/secret.vdi")
        self.assertEqual(rows["Encryption"], "AES-XTS256-PLAIN64")
        self.assertEqual(rows["Password ID"], "probeid")

    def test_empty_fields_are_left_out_entirely(self):
        rec = MediumRecord(uuid="u", location="/x.vdi", fmt="VDI",
                           capacity="1 MBytes", size_on_disk="1 MBytes")
        labels = [label for label, _value in medium_details(rec)]
        self.assertNotIn("Description", labels)
        self.assertNotIn("In use by", labels)
        self.assertNotIn("Access error", labels)

    def test_an_access_error_is_carried_through(self):
        rec = MediumRecord(uuid="u", location="/x.vdi", fmt="VDI",
                           capacity="1 MBytes", size_on_disk="1 MBytes",
                           state="inaccessible",
                           access_error="VERR_FILE_NOT_FOUND")
        rows = dict(medium_details(rec))
        self.assertEqual(rows["State"], "inaccessible")
        self.assertIn("VERR_FILE_NOT_FOUND", rows["Access error"])


class PlanScriptTest(unittest.TestCase):
    def test_a_plan_is_a_runnable_script_with_every_argument_quoted(self):
        script = plan_script([
            ["modifymedium", "disk", "my disk", "--compact"],
            ["closemedium", "disk", "x"],
        ])
        self.assertTrue(script.startswith("#!/bin/sh\n"))
        self.assertIn("set -e", script)
        self.assertIn("'my disk'", script)
        self.assertEqual(script.count("closemedium"), 1)

    def test_an_empty_plan_is_still_a_valid_script(self):
        self.assertTrue(plan_script([]).startswith("#!/bin/sh"))


class EtaTest(unittest.TestCase):
    def test_durations_read_as_people_write_them(self):
        self.assertEqual(format_duration(45), "45s")
        self.assertEqual(format_duration(190), "3m10s")
        self.assertEqual(format_duration(3900), "1h05m")
        self.assertEqual(format_duration(-5), "0s")

    def test_an_estimate_appears_once_there_is_something_to_estimate_from(self):
        self.assertEqual(eta_text(50, 60), "1m00s · ~1m00s left")
        self.assertEqual(eta_text(25, 30), "30s · ~1m30s left")

    def test_nothing_is_guessed_too_early_to_be_meaningful(self):
        # Below 5%, or in the first seconds, the number swings wildly enough
        # that it is worse than no number.
        self.assertEqual(eta_text(2, 60), "1m00s")
        self.assertEqual(eta_text(50, 1), "1s")
        self.assertEqual(eta_text(0, 60), "1m00s")

    def test_a_finished_operation_shows_only_the_elapsed_time(self):
        self.assertEqual(eta_text(100, 125), "2m05s")


class RegistryFingerprintTest(unittest.TestCase):
    def _record(self, **over):
        base = dict(uuid="aaaa", location="/vms/a.vdi", fmt="VDI",
                    capacity="100 MBytes", size_on_disk="10 MBytes")
        base.update(over)
        return MediumRecord(**base)

    def test_the_same_registry_fingerprints_the_same_whatever_the_order(self):
        a, b = self._record(), self._record(uuid="bbbb", location="/vms/b.vdi")
        self.assertEqual(
            registry_fingerprint({"disk": [a, b]}),
            registry_fingerprint({"disk": [b, a]}),
        )

    def test_a_changed_capacity_changes_the_fingerprint(self):
        self.assertNotEqual(
            registry_fingerprint({"disk": [self._record()]}),
            registry_fingerprint({"disk": [self._record(capacity="200 MBytes")]}),
        )

    def test_a_new_attachment_changes_the_fingerprint(self):
        self.assertNotEqual(
            registry_fingerprint({"disk": [self._record()]}),
            registry_fingerprint({"disk": [self._record(in_use=["vm (UUID: x)"])]}),
        )

    def test_a_removed_medium_changes_the_fingerprint(self):
        a = self._record()
        self.assertNotEqual(
            registry_fingerprint({"disk": [a]}),
            registry_fingerprint({"disk": []}),
        )

    def test_a_description_alone_does_not_cry_wolf(self):
        # Cosmetic fields are deliberately outside the digest: drift is about
        # what an action keys off.
        self.assertEqual(
            registry_fingerprint({"disk": [self._record()]}),
            registry_fingerprint({"disk": [self._record(description="note")]}),
        )


class HealthTest(unittest.TestCase):
    def test_a_clean_registry_reports_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "snap.vdi")
            with open(path, "w") as f:
                f.write("x")
            records = parse_media_list(SNAPSHOT_LISTING)
            records[0].location = path
            # The fixture's medium is held by a snapshot, which is itself a
            # finding; a genuinely clean registry has nothing holding it.
            records[0].in_use = []
            self.assertEqual(health_findings({"disk": records}), [])
        self.assertEqual(health_report([]), "No problems found.\n")

    def test_an_inaccessible_medium_is_an_error(self):
        records = parse_media_list(HDDS_LONG)
        for rec in records:
            rec.state = "inaccessible"
            rec.access_error = "VERR_FILE_NOT_FOUND opening image file"
        findings = health_findings({"disk": records})
        self.assertTrue(findings)
        self.assertTrue(all(f.level == "error" for f in findings))
        self.assertIn("VERR_FILE_NOT_FOUND", health_report(findings))

    def test_a_missing_file_is_caught_before_virtualbox_notices(self):
        # VBoxSVC caches State and only re-checks when something opens the
        # medium, so a registered-but-deleted image still says "created".
        records = parse_media_list(SNAPSHOT_LISTING)
        findings = health_findings({"disk": records})
        self.assertIn("missing-file", [f.code for f in findings])
        missing = next(f for f in findings if f.code == "missing-file")
        self.assertEqual(missing.problem, "the backing file is missing")

    def test_a_snapshot_only_medium_is_reported_as_unreleasable(self):
        # storageattach edits the current state only, so there is no slot to
        # clear — the old behaviour was a "could not locate" that read like a
        # bug in the app.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "snap.vdi")
            with open(path, "w") as f:
                f.write("x")
            records = parse_media_list(SNAPSHOT_LISTING)
            records[0].location = path
            findings = health_findings({"disk": records})
        codes = [f.code for f in findings]
        self.assertEqual(codes, ["snapshot-only"])
        self.assertIn("Before update", findings[0].problem)

    def test_a_current_state_attachment_is_not_snapshot_only(self):
        records = parse_media_list(SNAPSHOT_LISTING)
        records[0].in_use = [
            "win11 (UUID: 22222222-2222-2222-2222-222222222222)"
        ]
        self.assertFalse(snapshot_only(records[0]))

    def test_a_mixed_attachment_is_not_snapshot_only(self):
        # Held by a snapshot *and* the current state: the current-state slot
        # is detachable, so the medium is not stuck.
        records = parse_media_list(SNAPSHOT_LISTING)
        records[0].in_use.append(
            "win11 (UUID: 22222222-2222-2222-2222-222222222222)"
        )
        self.assertFalse(snapshot_only(records[0]))

    def test_an_unattached_medium_is_not_snapshot_only(self):
        records = parse_media_list(SNAPSHOT_LISTING)
        records[0].in_use = []
        self.assertFalse(snapshot_only(records[0]))

    def test_a_disk_attached_without_discard_is_flagged(self):
        # The usual reason a compact gives nothing back: without --discard the
        # guest's TRIM never reaches the image, so freed blocks stay
        # allocated and there is nothing for modifymedium --compact to find.
        records = parse_media_list(SNAPSHOT_LISTING)
        uuid = records[0].uuid
        info = parse_machinereadable(
            'name="win11"\n'
            'storagecontrollername0="SATA"\n'
            'storagecontrollerportcount0="2"\n'
            f'"SATA-ImageUUID-0-0"="{uuid}"\n'
            '"SATA-discard-0-0"="off"\n'
        )
        findings = discard_findings({"disk": records}, {"vm": info})
        self.assertEqual([f.code for f in findings], ["no-discard"])
        self.assertIn("win11", findings[0].problem)

    def test_discard_on_is_not_flagged(self):
        records = parse_media_list(SNAPSHOT_LISTING)
        uuid = records[0].uuid
        info = parse_machinereadable(
            'name="win11"\n'
            'storagecontrollername0="SATA"\n'
            'storagecontrollerportcount0="2"\n'
            f'"SATA-ImageUUID-0-0"="{uuid}"\n'
            '"SATA-discard-0-0"="on"\n'
        )
        self.assertEqual(discard_findings({"disk": records}, {"vm": info}), [])

    def test_an_unattached_disk_is_not_flagged(self):
        records = parse_media_list(SNAPSHOT_LISTING)
        info = parse_machinereadable(
            'name="win11"\n'
            'storagecontrollername0="SATA"\n'
            'storagecontrollerportcount0="2"\n'
            '"SATA-0-0"="none"\n'
        )
        self.assertEqual(discard_findings({"disk": records}, {"vm": info}), [])

    def test_one_disk_in_two_vms_is_reported_once_each(self):
        records = parse_media_list(SNAPSHOT_LISTING)
        uuid = records[0].uuid

        def vm(name):
            return parse_machinereadable(
                f'name="{name}"\n'
                'storagecontrollername0="SATA"\n'
                'storagecontrollerportcount0="2"\n'
                f'"SATA-ImageUUID-0-0"="{uuid}"\n'
                '"SATA-discard-0-0"="off"\n'
            )

        findings = discard_findings({"disk": records},
                                    {"a": vm("alpha"), "b": vm("beta")})
        self.assertEqual(len(findings), 2)
        self.assertEqual(
            sorted(f.problem for f in findings),
            ["attached to alpha without --discard",
             "attached to beta without --discard"],
        )

    def test_an_unregistered_parent_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "child.vdi")
            with open(path, "w") as f:
                f.write("x")
            records = parse_media_list(SNAPSHOT_LISTING)
            records[0].location = path
            records[0].parent_uuid = "44444444-4444-4444-4444-444444444444"
            problems = [f.problem for f in health_findings({"disk": records})]
            self.assertIn("its parent image is not registered", problems)

    def test_findings_carry_the_kind_so_they_can_be_revealed(self):
        records = parse_media_list(SNAPSHOT_LISTING)
        finding = health_findings({"dvd": records})[0]
        self.assertEqual(finding.kind, "dvd")
        self.assertEqual(finding.uuid, records[0].uuid)


class ReclaimTest(unittest.TestCase):
    def test_reclaimed_space_is_reported_as_measured(self):
        line = reclaim_line("a.vdi", 2048, 1024)
        self.assertIn("reclaimed 1.0 GB", line)
        self.assertIn("2.0 GB", line)

    def test_no_change_says_so_rather_than_claiming_zero_reclaimed(self):
        self.assertIn("nothing to reclaim", reclaim_line("a.vdi", 512, 512))

    def test_growth_is_not_reported_as_a_negative_reclaim(self):
        self.assertIn("grew by", reclaim_line("a.vdi", 512, 600))

    def test_an_unknown_size_reports_nothing(self):
        self.assertEqual(reclaim_line("a.vdi", None, 100), "")
        self.assertEqual(reclaim_line("a.vdi", 100, None), "")


class DescriptionBlankLineTest(unittest.TestCase):
    """A Description whose blank line is followed by a "UUID:" line.

    VBoxManage prints description continuations flush left, so the shape alone
    cannot tell them from the start of the next record. Taking the blank line
    as proof used to drop the real medium (it never reached its Location) and
    put a phantom in its place whose UUID carried an embedded newline — which
    then went into whatever command the user ran on that row.
    """

    LISTING = """\
UUID:           aaaaaaaa-1111-1111-1111-111111111111
Parent UUID:    base
State:          created
Description:    cloned from

UUID:           deadbeef-0000-0000-0000-000000000000
and resized afterwards
Type:           normal (base)
Location:       /vms/probe.vdi
Storage format: VDI
Capacity:       100 MBytes
Size on disk:   1 MBytes
"""

    def test_the_medium_is_not_replaced_by_a_phantom(self):
        records = parse_media_list(self.LISTING)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].uuid, "aaaaaaaa-1111-1111-1111-111111111111")
        self.assertEqual(records[0].location, "/vms/probe.vdi")

    def test_no_uuid_ever_contains_a_newline(self):
        for record in parse_media_list(self.LISTING):
            self.assertNotIn("\n", record.uuid)

    def test_the_quoted_uuid_stays_in_the_description(self):
        (record,) = parse_media_list(self.LISTING)
        self.assertIn("deadbeef-0000-0000-0000-000000000000", record.description)
        self.assertIn("and resized afterwards", record.description)

    def test_a_complete_record_still_ends_at_the_blank_line(self):
        # The guard must not weld two real records together: once a record has
        # a Location it is one VBoxManage could have finished printing.
        two = self.LISTING + """
UUID:           bbbbbbbb-2222-2222-2222-222222222222
Parent UUID:    base
State:          created
Type:           normal (base)
Location:       /vms/next.vdi
Storage format: VDI
Capacity:       32 MBytes
"""
        records = parse_media_list(two)
        self.assertEqual(
            [r.uuid for r in records],
            ["aaaaaaaa-1111-1111-1111-111111111111",
             "bbbbbbbb-2222-2222-2222-222222222222"],
        )


class DestructiveCommandTest(unittest.TestCase):
    """What the history dialog demands the word RUN for."""

    def test_clone_into_an_existing_image_is_destructive(self):
        # Every other clonemedium refuses a target that exists, so a replay can
        # only fail; --existing writes into the image that is already there.
        self.assertTrue(is_destructive_command(
            "VBoxManage clonemedium disk src.vdi dst.vdi --existing"
        ))

    def test_a_plain_clone_is_not(self):
        self.assertFalse(is_destructive_command(
            "VBoxManage clonemedium disk src.vdi dst.vdi --format VDI"
        ))

    def test_mediumio_writing_to_a_file_is_destructive(self):
        # mediumio overwrites --output without asking; the dialog confirms it
        # the first time round and a replay would skip that.
        self.assertTrue(is_destructive_command(
            "VBoxManage mediumio --disk=x cat --output=/home/me/notes.bin --size=512"
        ))

    def test_a_hex_dump_to_the_panel_is_not(self):
        self.assertFalse(is_destructive_command(
            "VBoxManage mediumio --disk=x cat --hex --size=512"
        ))

    def test_output_to_stdout_is_not(self):
        self.assertFalse(is_destructive_command(
            "VBoxManage mediumio --disk=x cat --output=- --size=512"
        ))

    def test_reading_commands_stay_replayable_without_a_typed_guard(self):
        for command in (
            "VBoxManage list -l hdds",
            "VBoxManage showmediuminfo disk 7894924d-30ae-41ec-a640-98cdba451f17",
            "VBoxManage internalcommands repairhd -dry-run -format VDI /vms/a.vdi",
        ):
            self.assertFalse(is_destructive_command(command), command)


class RegisteredAtTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vboxfront-registered-")
        self.path = os.path.join(self.dir, "disk.vdi")
        with open(self.path, "wb") as handle:
            handle.write(b"x")
        self.record = MediumRecord(
            uuid="aaaaaaaa-1111-1111-1111-111111111111", location=self.path,
            fmt="VDI", capacity="100 MBytes", size_on_disk="1 MBytes",
        )

    def test_finds_the_medium_backed_by_a_path(self):
        found = registered_at(self.path, [self.record])
        self.assertIsNotNone(found)
        self.assertEqual(found.uuid, self.record.uuid)

    def test_excludes_the_uuid_it_is_told_to(self):
        self.assertIsNone(
            registered_at(self.path, [self.record], exclude=self.record.uuid)
        )

    def test_matches_through_a_symlink(self):
        link = os.path.join(self.dir, "link.vdi")
        os.symlink(self.path, link)
        self.assertIsNotNone(registered_at(link, [self.record]))

    def test_an_unknown_path_is_not_registered(self):
        self.assertIsNone(
            registered_at(os.path.join(self.dir, "other.vdi"), [self.record])
        )


if __name__ == "__main__":
    unittest.main()
