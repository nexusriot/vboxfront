"""MainWindow actions: settings transfer, per-medium history, TRIM fix,
chain repair, and backup bundles."""
from __future__ import annotations

import os

from PyQt6.QtWidgets import QFileDialog, QMessageBox

from . import bundles, settings_io
from .app_dialogs import CommandHistoryDialog
from .constants import KINDS
from .domain import history_for, parse_vms_list
from .locate import parent_first
from .persistence import add_library_path, load_history, load_library, plan_mode, remembered_folder, remember_folder
from .tools_dialogs import (
    BackupDialog,
    ChainRepairDialog,
    DiscardFixDialog,
    RestoreDialog,
    SettingsTransferDialog,
)


class ToolsActionsMixin:
    def _init_tools(self):
        file_menu = self.menuBar().actions()[0].menu()
        media_menu = self.menuBar().actions()[1].menu()
        self.act_export_settings = self._action("Export settings…", self.export_settings)
        self.act_import_settings = self._action("Import settings…", self.import_settings)
        self.act_restore_bundle = self._action("Restore bundle…", self.restore_bundle)
        self.act_verify_bundle = self._action("Verify bundle…", self.verify_bundle)
        anchor = self.act_settings
        for act in (self.act_export_settings, self.act_import_settings):
            file_menu.insertAction(anchor, act)
        file_menu.addSeparator()
        file_menu.addAction(self.act_restore_bundle)
        file_menu.addAction(self.act_verify_bundle)

        self.act_medium_history = self._action("History of this medium…",
                                               self.open_medium_history)
        self.act_backup = self._action("Back up to bundle…", self.backup_selected)
        self.act_repair_chain = self._action("Repair chain…", self.repair_chain_selected)
        self.act_repair_chain_file = self._action("Repair chain from file…",
                                                  self.repair_chain_from_file)
        self.act_fix_trim = self._action("Enable TRIM on attached disks…",
                                         self.fix_discard_flags)
        media_menu.insertAction(self.act_health, self.act_fix_trim)
        media_menu.insertAction(self.act_attach, self.act_repair_chain)
        media_menu.insertAction(self.act_attach, self.act_backup)
        media_menu.insertAction(self.act_attach, self.act_medium_history)
        media_menu.insertAction(self.act_orphans, self.act_repair_chain_file)

    def _tools_context_actions(self):
        return [self.act_medium_history, self.act_repair_chain, self.act_backup]

    def _known_locations(self) -> list[str]:
        known = [rec.location for records in self.media.values() for rec in records]
        for kind in KINDS:
            known += load_library(kind)
        return known

    # -- settings --------------------------------------------------------
    def export_settings(self):
        dlg = SettingsTransferDialog(parent=self)
        if not dlg.exec() or not dlg.sections():
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export settings",
            os.path.join(remembered_folder("settings"), "vboxfront-settings.json"),
            "JSON (*.json)")
        if not path:
            return
        try:
            settings_io.write_settings_file(path, dlg.sections())
        except OSError as e:
            QMessageBox.warning(self, "Export failed", f"{path}: {e.strerror or e}")
            return
        remember_folder("settings", path)
        self.statusBar().showMessage(f"Settings exported to {path}")

    def import_settings(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import settings", remembered_folder("settings"), "JSON (*.json)")
        if not path:
            return
        try:
            data = settings_io.read_settings_file(path)
        except settings_io.SettingsFileError as e:
            QMessageBox.warning(self, "Cannot import", str(e))
            return
        dlg = SettingsTransferDialog(data, self)
        if not dlg.exec() or not dlg.sections():
            return
        report = settings_io.import_settings(data, dlg.sections(), dlg.replace.isChecked())
        remember_folder("settings", path)
        # Bring the window in line with what was imported.
        self.act_plan_mode.setChecked(plan_mode())
        self.reload_saved_filters()
        self._refresh_check_vbm()
        self.runner.note("[settings] " + report.text().replace("\n", "\n[settings] "))
        QMessageBox.information(self, "Settings imported", report.text())
        self._library_seen.clear()
        self.refresh_media()

    # -- history ---------------------------------------------------------
    def open_medium_history(self):
        m = self._require_selection()
        if not m:
            return
        name = os.path.basename(m.location) or m.uuid
        dlg = CommandHistoryDialog(self, history_for(load_history(), m),
                                   f"History of {name}")
        if dlg.exec() and dlg.rerun and self._require_idle():
            self.runner.run(dlg.rerun)

    # -- TRIM ------------------------------------------------------------
    def fix_discard_flags(self):
        """Read every VM fresh, then offer to turn --discard on where it is off.

        Fresh, not the health scan's answer: a VM may have started since, and
        changing a running VM's storage fails half-way through a batch.
        """
        if not self._require_idle():
            return

        def vms_done(code: int, output: str):
            if code != 0:
                self.runner.note(output)
                return
            vms = parse_vms_list(output)
            if not vms:
                QMessageBox.information(self, "Enable TRIM", "No virtual machines are registered.")
                return

            def infos_ready(infos):
                dlg = DiscardFixDialog(self.media, infos, self)
                if not dlg.commands and dlg.list.count() == 0:
                    QMessageBox.information(
                        self, "Enable TRIM",
                        f"Checked {len(infos)} VM(s): every attached disk already "
                        "passes TRIM through.")
                    return
                if not dlg.exec() or not dlg.commands or not self._require_idle():
                    return
                self._run_queue([(c, None, None) for c in dlg.commands],
                                "enable TRIM pass-through")

            self._collect_vminfo([uuid for _name, uuid in vms], infos_ready)

        self._capture(["list", "vms"], vms_done)

    # -- chains ----------------------------------------------------------
    def repair_chain_selected(self):
        m = self._require_selection()
        if not m or not self._require_idle():
            return
        if self.current_kind() != "disk":
            QMessageBox.information(self, "Repair chain", "Only hard disks form chains.")
            return
        self._repair_chain(uuid=m.uuid)

    def repair_chain_from_file(self):
        if not self._require_idle():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Differencing image to repair", remembered_folder("locate"),
            "Disk images (*.vdi *.vmdk *.vhd);;All files (*)")
        if path:
            remember_folder("locate", path)
            self._repair_chain(path=path)

    def _repair_chain(self, uuid: str = "", path: str = ""):
        dlg = ChainRepairDialog(self.media["disk"], uuid, path, self._known_locations(), self)
        if not dlg.exec() or not self._require_idle():
            return
        if dlg.relink:
            self.runner.note("[chain] relinking; run Repair chain again afterwards to "
                             "register the result")
            self._run_single(dlg.relink, title="relink a differencing image")
            return
        self._apply_chain_fixes(dlg.commands, dlg.register)

    def _apply_chain_fixes(self, commands: list[list[str]], register: list[str]):
        def add_to_library():
            for path in parent_first(register):
                add_library_path("disk", path)
                self.runner.note(f"[library] added disk {path}")
            self._library_seen.clear()
            self.refresh_media()

        if not commands:
            add_to_library()
            return
        entries = [(c, None, None) for c in commands]
        entries[-1] = (commands[-1], None, add_to_library if register else None)
        self._snapshot_registry("chain repair")
        self._run_queue(entries, "re-point the chain's moved images")

    # -- bundles ---------------------------------------------------------
    def backup_selected(self):
        m = self._require_selection()
        if not m or not self._require_idle():
            return
        kind = self.current_kind()
        try:
            members = bundles.plan_members(self.media, kind, m.uuid)
        except bundles.BundleError as e:
            QMessageBox.warning(self, "Cannot back up", str(e))
            return

        def running_done(_code: int, output: str):
            running = bundles.running_users(
                members, [uuid for _name, uuid in parse_vms_list(output)])
            dlg = BackupDialog(members, running, self)
            if dlg.exec() and dlg.bundle_path:
                self.runner.note(f"[backup] wrote {dlg.bundle_path}")
                self.statusBar().showMessage(f"Backed up to {dlg.bundle_path}")

        self._capture(["list", "runningvms"], running_done)

    def _choose_bundle(self, title: str) -> tuple[str, dict] | None:
        path = QFileDialog.getExistingDirectory(self, title, remembered_folder("bundles"))
        if not path:
            return None
        try:
            return path, bundles.read_manifest(path)
        except bundles.BundleError as e:
            QMessageBox.warning(self, title, str(e))
            return None

    def verify_bundle(self):
        chosen = self._choose_bundle("Verify bundle")
        if chosen:
            RestoreDialog(chosen[0], chosen[1], {}, self, verify_only=True).exec()

    def restore_bundle(self):
        if not self._require_idle():
            return
        chosen = self._choose_bundle("Restore bundle")
        if not chosen:
            return
        registered = {rec.uuid.lower(): rec.location
                      for records in self.media.values() for rec in records}
        dlg = RestoreDialog(chosen[0], chosen[1], registered, self)
        if not dlg.exec():
            return
        restored = dlg.restored_paths()
        disks = parent_first([p for kind, p in restored if kind == "disk"])
        for kind, path in [("disk", p) for p in disks] + [
                (k, p) for k, p in restored if k != "disk"]:
            add_library_path(kind, path)
            self.runner.note(f"[restore] added {kind} {path}")
        self._library_seen.clear()
        self.refresh_media()
