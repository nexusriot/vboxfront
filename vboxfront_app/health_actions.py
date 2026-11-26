"""MainWindow actions. Media health, the attach-flag scan and the orphan scan.

Mixed into `window.MainWindow`; methods use its widgets, runner and helpers.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QMessageBox

from .app_dialogs import HealthDialog, OrphanScanDialog
from .commands import repairhd_args
from .constants import KINDS
from .domain import discard_findings, parse_vms_list
from .models import HealthFinding
from .persistence import add_library_path, load_library


class HealthActionsMixin:
    def open_health(self, extra: list[HealthFinding] | None = None):
        dlg = HealthDialog(self.media, self)
        if extra:
            # Findings from the attach-flag scan, which needed a command and
            # so could not be there when the dialog first opened.
            dlg.add_findings(extra)
            dlg.repopulate()
        if not dlg.exec():
            return
        if dlg.reveal:
            kind, uuid = dlg.reveal
            self.tabs.setCurrentIndex(KINDS.index(kind))
            self.panes[kind].select_uuid(uuid)
            return
        if dlg.prune:
            # Plain closemedium: the file is already gone, so there is nothing
            # for --delete to remove and asking for it would only fail.
            self.runner.note(
                f"[health] unregistering {len(dlg.prune)} medium(s) whose "
                "backing file is gone"
            )
            self._snapshot_registry("health cleanup")
            # No trash batch: the file is already gone, so there is nothing
            # to move aside — the follow-up only prunes the library entry.
            self._run_queue(
                [(["closemedium", kind, uuid], None,
                  self._removal_followup(kind, self._location_of(kind, uuid), None))
                 for kind, uuid in dlg.prune],
                "unregister media whose file is gone",
            )
            return
        if dlg.want_discard_scan:
            self.scan_attach_flags()
            return
        if getattr(dlg, "want_discard_fix", False):
            self.fix_discard_flags()
            return
        if getattr(dlg, "repair_chain", None):
            kind, uuid = dlg.repair_chain
            self.tabs.setCurrentIndex(KINDS.index(kind))
            self.panes[kind].select_uuid(uuid)
            self._repair_chain(uuid=uuid)
            return
        if dlg.dry_run_paths:
            # -dry-run reads and reports; it never writes, so a batch of them is
            # safe to queue unattended.
            self.runner.note(dlg.dry_run_caveat())
            self._run_queue(
                [(repairhd_args(path, fmt="VDI", dry_run=True), None, None)
                 for path in dlg.dry_run_paths],
                "check the VDI images",
            )

    def _location_of(self, kind: str, uuid: str) -> str:
        for rec in self.media.get(kind, []):
            if rec.uuid == uuid:
                return rec.location
        return ""

    def scan_attach_flags(self):
        """Read every VM and report disks attached without `--discard`.

        The one health check that cannot come from the registry: whether a
        slot passes the guest's TRIM through lives in `showvminfo`, not in
        `list -l`. Reopens the health dialog with the answer folded in.
        """
        def vms_done(code: int, output: str):
            if code != 0:
                self.runner.note(output)
                return
            vms = parse_vms_list(output)
            if not vms:
                QMessageBox.information(
                    self, "Attach flags", "No virtual machines are registered."
                )
                return

            def infos_ready(infos: dict[str, dict[str, str]]):
                findings = discard_findings(self.media, infos)
                if not findings:
                    QMessageBox.information(
                        self, "Attach flags",
                        f"Checked {len(infos)} VM(s): every attached disk "
                        "passes the guest's TRIM through.",
                    )
                    return
                self.open_health(findings)

            self._collect_vminfo([uuid for _name, uuid in vms], infos_ready)

        self._capture(["list", "vms"], vms_done)

    def open_orphan_scan(self):
        known = [rec.location for records in self.media.values() for rec in records]
        for kind in KINDS:
            known += load_library(kind)
        dlg = OrphanScanDialog(known, self, capture=self._capture)
        if not dlg.exec() or not dlg.chosen:
            return
        for kind, path in dlg.chosen:
            add_library_path(kind, path)
            self.runner.note(f"[library] added {kind} {path}")
        self.refresh_media()
