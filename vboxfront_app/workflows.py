"""Multi-step workflows hosted by MainWindow: flatten, verify, golden image, browse.

Each one is built from commands the app already runs one at a time; what this
mixin adds is the sequencing, the checks before it, and what to say after.
"""
from __future__ import annotations

import os

from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

from .commands import attach_args
from .disk_dialogs import FlattenDialog
from .domain import (
    first_free_disk_slot,
    is_differencing,
    is_inaccessible,
    parse_capacity_mb,
    parse_vms_list,
    real_parent_uuid,
)
from .feature_dialogs import GoldenImageDialog, VerifyDialog
from .models import MediumRecord
from .mounts import ImageMounts
from .persistence import vboxmanage_path
from .reports import chain_of, chain_problems
from .verify import VerifySource, verdict

FLAT_FILE_CHOICE = "A flat image file (RAW export)…"


def _name(rec: MediumRecord) -> str:
    return os.path.basename(rec.location) or rec.uuid


def _encrypted(rec: MediumRecord) -> bool:
    return rec.encryption not in ("", "disabled")


def medium_source(kind: str, rec: MediumRecord) -> VerifySource:
    capacity = parse_capacity_mb(rec.capacity) or 0
    return VerifySource(_name(rec), rec.uuid, kind, expected_bytes=capacity * 1024 * 1024)


class WorkflowActionsMixin:
    def _init_workflows(self):
        self.image_mounts = ImageMounts(self)
        self.image_mounts.mounted.connect(self._image_mounted)
        self.image_mounts.failed.connect(self._image_mount_failed)
        self.image_mounts.note.connect(self.runner.note)
        media_menu = self.menuBar().actions()[1].menu()
        self.act_flatten = self._action('Flatten chain…', self.flatten_selected)
        self.act_verify = self._action('Compare contents…', self.verify_selected)
        self.act_golden = self._action('Golden image for VMs…', self.golden_image_selected)
        self.act_browse = self._action('Browse files (read-only)…', self.browse_selected)
        self.act_unmount = self._action('Unmount browsed images', self.unmount_browsed)
        self.act_unmount.setEnabled(False)
        anchor = self.act_attach
        for act in (self.act_flatten, self.act_verify, self.act_browse, self.act_unmount):
            media_menu.insertAction(anchor, act)
        media_menu.insertAction(self.act_detach, self.act_golden)
        media_menu.insertSeparator(self.act_attach)

    # ---- verify ------------------------------------------------------------

    def _run_clone(self, dlg, source: MediumRecord):
        """Run a single clone/flatten, then verify it when the dialog asked to."""
        if dlg.verify_check.isChecked():
            target = dlg.target_path
            sources = [medium_source('disk', source),
                       VerifySource(os.path.basename(target), target, 'disk',
                                    expected_bytes=medium_source('disk', source).expected_bytes)]
            self._followup = lambda: QTimer.singleShot(0, lambda: self._open_verify(sources))
        if not self.runner.run(dlg.args(source.uuid)):
            self._followup = None

    def _open_verify(self, sources: list[VerifySource]):
        vbm = vboxmanage_path()
        if not vbm:
            self.statusBar().showMessage("VBoxManage not found.")
            return None
        for source in sources:
            self.runner.note(f'[verify] {source.describe()}')
        dlg = VerifyDialog(vbm, sources, self)
        dlg.exec()
        if dlg.results is not None:
            self.runner.note(f'[verify] {verdict(dlg.results)[1]}')
        return dlg

    def verify_selected(self):
        m = self._require_selection()
        if not m or not self._require_idle():
            return
        kind = self.current_kind()
        if _encrypted(m):
            QMessageBox.information(self, 'Compare contents',
                                    'Encrypted media cannot be read without their password.')
            return
        others = [r for r in self.media[kind]
                  if r.uuid != m.uuid and not _encrypted(r) and not is_inaccessible(r)]
        labels = [f'{_name(r)}  ({r.uuid})' for r in others] + [FLAT_FILE_CHOICE]
        choice, ok = QInputDialog.getItem(self, 'Compare contents',
                                          f'Compare {_name(m)} with:', labels, 0, False)
        if not ok:
            return
        left = medium_source(kind, m)
        if choice == FLAT_FILE_CHOICE:
            path, _ = QFileDialog.getOpenFileName(
                self, 'Flat image', os.path.dirname(m.location),
                'Raw images (*.img *.raw *.bin);;All files (*)')
            if not path:
                return
            right = VerifySource(os.path.basename(path), path, is_file=True,
                                 expected_bytes=left.expected_bytes)
        else:
            right = medium_source(kind, others[labels.index(choice)])
        self._open_verify([left, right])

    # ---- flatten -------------------------------------------------------------

    def flatten_selected(self):
        m = self._require_selection()
        if not m or not self._require_idle():
            return
        if self.current_kind() != 'disk' or not is_differencing(m):
            QMessageBox.information(
                self, 'Flatten chain',
                'Select a differencing image — the last link of the chain to fold '
                'into one standalone disk.')
            return
        chain = chain_of(self.media['disk'], m.uuid)
        problems = chain_problems(chain)
        if problems:
            QMessageBox.warning(self, 'Chain cannot be read',
                                'Flattening reads every link, and ' + '; '.join(problems) + '.')
            return
        dlg = FlattenDialog(chain, self.backends, self, known=self.media['disk'])
        if not dlg.exec() or not self._require_idle():
            return
        if dlg.overwrite and not self._clear_target(dlg.target_path):
            return
        self.runner.note(
            f'[flatten] {len(chain)} image(s) → {dlg.target_path}; the chain itself is '
            'left as it is. Attach the result in its place to retire the chain.')
        self._run_clone(dlg, m)

    # ---- golden image ----------------------------------------------------------

    def golden_image_selected(self):
        m = self._require_selection()
        if not m or not self._require_idle():
            return
        if self.current_kind() != 'disk' or is_differencing(m) or real_parent_uuid(m):
            QMessageBox.information(self, 'Golden image',
                                    'Select a base hard disk (not a differencing image).')
            return
        if is_inaccessible(m):
            QMessageBox.warning(self, 'Golden image', f'{_name(m)} is inaccessible.')
            return
        multiattach = m.medium_type.lower().startswith('multiattach')
        if not multiattach and m.in_use:
            QMessageBox.warning(
                self, 'Golden image',
                f'{_name(m)} is attached to {", ".join(m.in_use)}. VirtualBox only '
                'changes the type of a medium nothing uses — detach it first.')
            return

        def got_vms(code, output):
            if code:
                self.runner.note(output)
                return
            vms = parse_vms_list(output)
            if not vms:
                QMessageBox.information(self, 'Golden image', 'There are no VMs to attach to.')
                return

            def got_running(_code, running_out):
                running = {uuid for _name_, uuid in parse_vms_list(running_out)}

                def got_infos(infos):
                    rows = [(name, uuid, first_free_disk_slot(infos.get(uuid, {})),
                             uuid in running) for name, uuid in vms]
                    self._golden_image_dialog(m, multiattach, rows)
                self._collect_vminfo([uuid for _n, uuid in vms], got_infos)
            self._capture(['list', 'runningvms'], got_running)
        self._capture(['list', 'vms'], got_vms)

    def _golden_image_dialog(self, m: MediumRecord, multiattach: bool, rows):
        dlg = GoldenImageDialog(_name(m), not multiattach, rows, self)
        if not dlg.exec() or not dlg.chosen:
            return
        entries = []
        if not multiattach:
            entries.append((['modifymedium', 'disk', m.uuid, '--type', 'multiattach'], None, None))
        for vm_uuid, _vm_name, (controller, port, device) in dlg.chosen:
            entries.append((attach_args(vm_uuid, controller, port, device, 'disk', m.uuid),
                            None, None))
        names = ', '.join(name for _u, name, _s in dlg.chosen)
        self._snapshot_registry('golden image')
        if self._run_queue(entries, f'golden image {_name(m)} for {names}'):
            self.runner.note(f'[golden] {_name(m)} is shared by {names}; each VM writes '
                             'to a differencing image of its own')

    # ---- browse ------------------------------------------------------------------

    def browse_selected(self):
        m = self._require_selection()
        if not m:
            return
        if _encrypted(m):
            QMessageBox.information(self, 'Browse files',
                                    'vboximg-mount cannot open encrypted media.')
            return
        if is_inaccessible(m):
            QMessageBox.warning(self, 'Browse files', f'{_name(m)} is inaccessible.')
            return
        if m.in_use and QMessageBox.question(
                self, 'Browse files',
                f'{_name(m)} is attached to {", ".join(m.in_use)}. If that VM is running, '
                'what you see may be mid-write and inconsistent. Mount read-only anyway?'
        ) != QMessageBox.StandardButton.Yes:
            return
        self.image_mounts.mount(_name(m), m.uuid)

    def _image_mounted(self, label: str, mountpoint: str):
        self.act_unmount.setEnabled(True)
        self.runner.note(
            f'[browse] {label} mounted read-only at {mountpoint} — fs<N> folders hold the '
            'filesystems vboximg-mount can read, vol<N> the raw partitions and vhdd the '
            'whole disk. A filesystem without a partition table shows as vhdd only. '
            'Media → Unmount browsed images when done.')
        QDesktopServices.openUrl(QUrl.fromLocalFile(mountpoint))

    def _image_mount_failed(self, label: str, message: str):
        self.act_unmount.setEnabled(bool(self.image_mounts.mounts))
        self.runner.note(f'[browse] could not mount {label}: {message}')
        QMessageBox.warning(self, 'Browse files', f'Could not mount {label}:\n\n{message}')

    def unmount_browsed(self):
        problems = self.image_mounts.unmount_all()
        for line in problems:
            self.runner.note(f'[browse] could not unmount {line}')
        self.act_unmount.setEnabled(bool(self.image_mounts.mounts))
        if not problems:
            self.runner.note('[browse] unmounted')
        return problems
