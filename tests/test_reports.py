"""Space report and chain helpers: pure functions over a parsed listing."""

import unittest

from vboxfront_app.domain import parse_media_list
from vboxfront_app.models import MediumRecord
from vboxfront_app.reports import (
    NOT_ATTACHED,
    chain_is_complete,
    chain_of,
    chain_problems,
    compact_candidates,
    space_report,
    space_report_json,
    space_report_text,
)
from tests.test_window import HDDS_OUTPUT


def rec(uuid, parent="", size=100, fmt="VDI", variant="dynamic default", in_use=(),
        location=None, state="created", mtype="normal (base)"):
    return MediumRecord(uuid=uuid, location=location or f"/vm/{uuid}.vdi", fmt=fmt,
                        capacity="1000 MBytes", size_on_disk=f"{size} MBytes",
                        state=state, medium_type=mtype, parent_uuid=parent or "base",
                        variant=variant, in_use=list(in_use))


VM_A = "alpha (UUID: 11111111-1111-1111-1111-111111111111)"
VM_B = "beta (UUID: 22222222-2222-2222-2222-222222222222)"


class ChainTest(unittest.TestCase):
    def test_chain_runs_base_first_to_the_selected_image(self):
        records = [rec("c", "b", mtype="normal (differencing)"), rec("a"),
                   rec("b", "a", mtype="normal (differencing)")]
        self.assertEqual([r.uuid for r in chain_of(records, "c")], ["a", "b", "c"])
        self.assertTrue(chain_is_complete(chain_of(records, "c")))

    def test_missing_parent_and_inaccessible_link_are_named(self):
        records = [rec("c", "zz", mtype="normal (differencing)"),
                   rec("d", "c", state="inaccessible", mtype="normal (differencing)")]
        chain = chain_of(records, "d")
        self.assertFalse(chain_is_complete(chain))
        problems = chain_problems(chain)
        self.assertIn("not registered", problems[0])
        # c's file does not exist either; d is reported as inaccessible, not
        # twice over.
        self.assertEqual(problems[1:], ["the file of c.vdi is missing", "d.vdi is inaccessible"])

    def test_a_cycle_does_not_hang(self):
        records = [rec("a", "b"), rec("b", "a")]
        self.assertEqual(len(chain_of(records, "a")), 2)


class SpaceReportTest(unittest.TestCase):
    def section(self, sections, title):
        return next(s for s in sections if s.title == title)

    def test_by_vm_counts_shared_disks_under_each_vm(self):
        disks = [rec("a", size=300, in_use=[VM_A, VM_B]), rec("b", size=50, in_use=[VM_A]),
                 rec("c", size=7)]
        by_vm = {r.label: r for r in self.section(space_report({"disk": disks}), "By VM").rows}
        self.assertEqual(by_vm["alpha"].mb, 350)
        self.assertEqual(by_vm["beta"].mb, 300)
        self.assertEqual(by_vm[NOT_ATTACHED].mb, 7)
        self.assertEqual(by_vm["beta"].note, "shares disks with other VMs")

    def test_chains_sum_every_link_and_skip_lone_images(self):
        disks = [rec("a", size=1000), rec("b", "a", 10, mtype="normal (differencing)"),
                 rec("c", "b", 5, mtype="normal (differencing)"), rec("lone", size=1)]
        rows = self.section(space_report({"disk": disks}), "Differencing chains").rows
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].mb, rows[0].count), (1015, 3))

    def test_compact_candidates_are_dynamic_accessible_vdis_largest_first(self):
        disks = [rec("small", size=5), rec("fixed", size=900, variant="fixed default"),
                 rec("vmdk", size=800, fmt="VMDK"), rec("big", size=600),
                 rec("bad", size=700, state="inaccessible")]
        self.assertEqual([r.uuid for r in compact_candidates(disks)], ["big", "small"])

    def test_trash_appears_in_totals_and_text_and_json_render(self):
        media = {"disk": parse_media_list(HDDS_OUTPUT), "dvd": [], "floppy": []}
        sections = space_report(media, trash_bytes=3 * 1024 ** 3)
        totals = {r.label: r.mb for r in self.section(sections, "Totals").rows}
        self.assertEqual(totals["Trash (indexed deletes)"], 3072)
        self.assertIn("Compact candidates", space_report_text(sections))
        self.assertEqual(space_report_json(sections)[0]["title"], "Totals")


if __name__ == "__main__":
    unittest.main()


class HelperTest(unittest.TestCase):
    def test_verdict_names_size_and_content_differences(self):
        from vboxfront_app.verify import VerifyResult, verdict
        same = verdict([VerifyResult("a", "x", 10), VerifyResult("b", "x", 10)])
        self.assertTrue(same[0])
        self.assertFalse(verdict([VerifyResult("a", "x", 10), VerifyResult("b", "x", 11)])[0])
        content = verdict([VerifyResult("a", "x", 10), VerifyResult("b", "y", 10)])
        self.assertIn("Contents differ", content[1])
        error = verdict([VerifyResult("a", error="boom"), VerifyResult("b", "y", 10)])
        self.assertEqual(error, (False, "Could not read both images — a: boom"))

    def test_first_free_disk_slot_skips_floppy_and_full_controllers(self):
        from vboxfront_app.domain import first_free_disk_slot
        info = {
            "storagecontrollername0": "Floppy", "storagecontrollertype0": "I82078",
            "storagecontrollerportcount0": "1",
            "storagecontrollername1": "IDE", "storagecontrollertype1": "PIIX4",
            "storagecontrollerportcount1": "1",
            "IDE-0-0": "x.vdi", "IDE-0-1": "y.iso",
            "storagecontrollername2": "SATA", "storagecontrollertype2": "IntelAhci",
            "storagecontrollerportcount2": "2", "SATA-0-0": "z.vdi",
        }
        self.assertEqual(first_free_disk_slot(info), ("SATA", 1, 0))
        self.assertIsNone(first_free_disk_slot({}))

    def test_properties_undo_restores_only_what_changes_autoreset_first(self):
        from vboxfront_app.commands import properties_undo_args
        m = rec("u", mtype="immutable (base)")
        m.auto_reset, m.description = "on", "old"
        self.assertEqual(
            properties_undo_args("disk", m, "normal", "new", False),
            [["modifymedium", "disk", "u", "--autoreset", "on"],
             ["modifymedium", "disk", "u", "--type", "immutable", "--description", "old"]])
        self.assertEqual(properties_undo_args("disk", m, "immutable", "old", True), [])
