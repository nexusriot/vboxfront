"""MainWindow actions. Attaching media to VMs and detaching them, singly or as a selection.

Mixed into `window.MainWindow`; methods use its widgets, runner and helpers.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QInputDialog, QMessageBox
import os

from .attach_dialog import AttachDialog
from .commands import detach_args
from .domain import (
    find_attachment,
    parse_in_use_entry,
    parse_machinereadable,
    parse_vms_list,
    snapshot_only,
    snapshot_word,
)
from .models import MediumRecord


class AttachActionsMixin:
    def attach_selected(self):
        media = self._require_selection_multi()
        if not media:
            return
        m = self.selected_medium() or media[0]
        # Pin the kind now: the VM list is fetched asynchronously and the user
        # can switch tabs before the dialog opens, which would otherwise attach
        # this medium with the *new* tab's --type.
        kind = self.current_kind()

        def vms_done(code: int, output: str):
            if code != 0:
                self.runner.note(output)
                return
            vms = parse_vms_list(output)
            if not vms:
                QMessageBox.information(self, "No VMs", "No virtual machines are registered.")
                return

            def running_done(_code: int, running_out: str):
                running = {uuid for _n, uuid in parse_vms_list(running_out)}
                dlg = AttachDialog(kind, m, vms, running, self._capture, self,
                                   extra=media)
                if not dlg.exec():
                    return
                commands = dlg.commands()
                if len(commands) == 1:
                    if self._require_idle():
                        self._run_single(commands[0], title="attach")
                    return
                self._run_queue([(c, None, None) for c in commands],
                                "attach the selected media")

            self._capture(["list", "runningvms"], running_done)

        self._capture(["list", "vms"], vms_done)

    def detach_selected(self):
        media = self._require_selection_multi()
        if not media:
            return
        if len(media) > 1:
            self._detach_batch(media)
            return
        m = media[0]
        attachments = [e for e in (parse_in_use_entry(x) for x in m.in_use) if e]
        if not attachments:
            QMessageBox.information(self, "Detach", "This medium is not attached to any VM.")
            return
        if snapshot_only(m):
            # storageattach only edits the current state, so there is no slot
            # to clear. Saying that beats "could not locate the attachment
            # slot", which reads like a bug in the app.
            QMessageBox.information(
                self, "Held by a snapshot",
                f"{os.path.basename(m.location) or m.uuid} is attached only "
                f"inside the snapshot “{snapshot_word(m)}”, not in any VM's "
                "current state.\n\nVBoxManage can only detach from the "
                "current state, so nothing here can release it — deleting or "
                "restoring that snapshot in the VirtualBox GUI is what frees "
                "the medium.",
            )
            return
        if len(attachments) == 1:
            vm_name, vm_uuid = attachments[0]
            ret = QMessageBox.question(
                self, "Detach", f"Detach {os.path.basename(m.location)} from “{vm_name}”?"
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        else:
            names = [name for name, _uuid in attachments]
            name, ok = QInputDialog.getItem(
                self, "Detach", "Detach from which VM?", names, 0, False
            )
            if not ok:
                return
            vm_name, vm_uuid = attachments[names.index(name)]

        def vminfo_done(code: int, output: str):
            if code != 0:
                self.runner.note(output)
                return
            info = parse_machinereadable(output)
            slot = find_attachment(info, m.uuid)
            if not slot:
                QMessageBox.warning(
                    self, "Detach",
                    f"Could not locate the attachment slot in “{vm_name}”.",
                )
                return
            ctl, port, device = slot
            if self._require_idle():
                self._run_single(detach_args(vm_uuid, ctl, port, device), title="detach")

        self._capture(["showvminfo", vm_uuid, "--machinereadable"], vminfo_done)

    def _detach_batch(self, media: list[MediumRecord]):
        """Detach every attachment of every selected medium.

        One `showvminfo` per VM rather than per attachment: the slot of each
        medium is found in the same dump, and a VM holding six of them should
        not be read six times.
        """
        snapshot_held = [m for m in media if snapshot_only(m)]
        pairs = []
        for m in media:
            if m in snapshot_held:
                continue
            for entry in m.in_use:
                parsed = parse_in_use_entry(entry)
                if parsed:
                    pairs.append((m, parsed[0], parsed[1]))
        if snapshot_held:
            # Separated out before the slot lookups rather than reported as
            # "could not locate" afterwards: there is no slot to find.
            self.runner.note(
                "[detach] held only by a snapshot, which storageattach cannot "
                "reach: "
                + ", ".join(os.path.basename(m.location) or m.uuid
                            for m in snapshot_held)
            )
        if not pairs:
            QMessageBox.information(
                self, "Detach", "None of the selected media are attached to a VM."
            )
            return
        listing = "\n".join(
            f"{os.path.basename(m.location) or m.uuid} ← {vm}" for m, vm, _u in pairs[:12]
        )
        ret = QMessageBox.question(
            self, "Detach",
            f"Detach {len(pairs)} attachment(s)?\n\n{listing}"
            + ("\n…" if len(pairs) > 12 else ""),
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

        def infos_ready(infos: dict[str, dict[str, str]]):
            entries, missing = [], []
            for m, vm_name, vm_uuid in pairs:
                info = infos.get(vm_uuid)
                slot = find_attachment(info, m.uuid) if info else None
                if not slot:
                    missing.append(f"{os.path.basename(m.location) or m.uuid} in {vm_name}")
                    continue
                ctl, port, device = slot
                entries.append((detach_args(vm_uuid, ctl, port, device), None, None))
            if missing:
                self.runner.note(
                    "[detach] could not locate: " + "; ".join(missing)
                )
            if not entries:
                QMessageBox.warning(
                    self, "Detach", "No attachment slot could be located."
                )
                return
            self._run_queue(entries, "detach the selected media")

        self._collect_vminfo(sorted({u for _m, _n, u in pairs}), infos_ready)
