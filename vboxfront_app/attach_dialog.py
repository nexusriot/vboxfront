"""VBoxFront attach dialog."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QSpinBox,
)
import os

from .commands import attach_args
from .constants import MEDIUM_TYPES
from .domain import (
    controller_types,
    find_free_port,
    free_slots,
    max_device_index,
    parse_host_drives,
    parse_machinereadable,
    slot_occupant,
    vm_storage_controllers,
)
from .models import MediumRecord
from .theme import color_css, muted_color, warning_color


class AttachDialog(QDialog):
    """Attach a medium to a VM's storage controller (storageattach)."""

    def __init__(self, kind: str, medium: MediumRecord, vms: list[tuple[str, str]],
                 running: set[str], capture, parent=None,
                 extra: list[MediumRecord] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Attach to VM")
        self._kind = kind
        self._medium = medium
        #: Every medium this dialog attaches, the focused one first. More than
        #: one fills consecutive free slots rather than asking per medium.
        self._media = [medium] + [m for m in (extra or []) if m.uuid != medium.uuid]
        self._slots: list[tuple[int, int]] = []
        self._vms = vms
        self._capture = capture
        self._vminfo: dict[str, str] = {}
        self._controllers: list[tuple[str, int]] = []
        self._types: dict[str, str] = {}
        self._request = 0

        layout = QFormLayout(self)
        if len(self._media) > 1:
            names = ", ".join(
                os.path.basename(m.location) or m.uuid for m in self._media[:4]
            )
            layout.addRow(QLabel(
                f"<b>{len(self._media)} media</b> — {names}"
                + ("…" if len(self._media) > 4 else "")
            ))
        else:
            layout.addRow(QLabel(f"<b>{os.path.basename(medium.location)}</b>"))

        # What actually goes into --medium. Usually this image, but the same
        # command mounts an empty drive, the Guest Additions ISO or a physical
        # drive, and there is nowhere else in the app to do that.
        self.medium_combo = QComboBox()
        self.medium_combo.addItem(
            f"This image ({os.path.basename(medium.location) or medium.uuid})",
            medium.uuid,
        )
        if kind in ("dvd", "floppy"):
            self.medium_combo.addItem("Empty drive", "emptydrive")
        if kind == "dvd":
            self.medium_combo.addItem("Guest Additions ISO", "additions")
        self.medium_combo.currentIndexChanged.connect(self._on_medium_changed)
        # Empty drives, the Additions ISO and host drives are single-medium
        # ideas; a batch is attaching the images that were selected.
        if kind in ("dvd", "floppy") and len(self._media) == 1:
            layout.addRow("Medium:", self.medium_combo)
            self._load_host_drives()

        self.vm_combo = QComboBox()
        for name, uuid in vms:
            label = f"{name} (running)" if uuid in running else name
            self.vm_combo.addItem(label, uuid)
        self.vm_combo.currentIndexChanged.connect(self._on_vm_changed)
        layout.addRow("Virtual machine:", self.vm_combo)

        self.ctl_combo = QComboBox()
        self.ctl_combo.currentIndexChanged.connect(self._on_ctl_changed)
        layout.addRow("Controller:", self.ctl_combo)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(0, 29)
        self.port_spin.valueChanged.connect(self._on_slot_changed)
        layout.addRow("First port:" if len(self._media) > 1 else "Port:",
                      self.port_spin)

        self.device_spin = QSpinBox()
        # Narrowed per controller in _on_ctl_changed: only IDE and floppy
        # controllers have a device 1.
        self.device_spin.setRange(0, 1)
        self.device_spin.valueChanged.connect(self._on_slot_changed)
        self.device_spin.setEnabled(len(self._media) == 1)
        layout.addRow("Device:", self.device_spin)

        self.status = QLabel("")
        self.status.setStyleSheet(color_css(muted_color()))
        layout.addRow(self.status)

        self.slot_status = QLabel("")
        layout.addRow(self.slot_status)

        # Disk-only storage attributes; each stays out of the command unless
        # asked for. --discard is the one that makes compaction pay off, since
        # without it the guest's TRIM never reaches the image.
        self.mtype_combo = QComboBox(self)
        self.mtype_combo.addItem("unchanged", "")
        for medium_type in MEDIUM_TYPES:
            self.mtype_combo.addItem(medium_type, medium_type)
        self.discard_check = QCheckBox("Pass guest TRIM through (--discard)", self)
        self.nonrotational_check = QCheckBox("Report as an SSD (--nonrotational)", self)
        self.hotpluggable_check = QCheckBox("Hot-pluggable slot (--hotpluggable)", self)
        for widget in (self.mtype_combo, self.discard_check,
                       self.nonrotational_check, self.hotpluggable_check):
            widget.setVisible(kind == "disk")
        if kind == "disk":
            layout.addRow("Medium type:", self.mtype_combo)
            layout.addRow(self.discard_check)
            layout.addRow(self.nonrotational_check)
            layout.addRow(self.hotpluggable_check)

        # Optical/removable flags. --passthrough hands the guest the real drive
        # (needed for burning); --tempeject lets the guest eject without the
        # change being saved; --forceunmount detaches a medium the guest has
        # locked, which is the way out of "the medium is locked" on a running VM.
        self.passthrough_check = QCheckBox("Pass the physical drive through (--passthrough)", self)
        self.tempeject_check = QCheckBox("Guest may eject temporarily (--tempeject)", self)
        self.forceunmount_check = QCheckBox("Force the current medium out (--forceunmount)", self)
        for widget in (self.passthrough_check, self.tempeject_check, self.forceunmount_check):
            widget.setVisible(kind in ("dvd", "floppy"))
        if kind == "dvd":
            layout.addRow(self.passthrough_check)
            layout.addRow(self.tempeject_check)
        if kind in ("dvd", "floppy"):
            layout.addRow(self.forceunmount_check)
        self._on_medium_changed()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Attach")
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

        if vms:
            self._on_vm_changed(0)

    def _load_host_drives(self):
        """Offer the host's physical drives, when it has any this user can see.

        `list hostdvds` comes back empty on a machine with no optical drive —
        and on one where the user cannot enumerate them — so the entries are
        added only if something is actually there.
        """
        subject = "hostdvds" if self._kind == "dvd" else "hostfloppies"

        def done(code: int, output: str):
            if code != 0:
                return
            for name in parse_host_drives(output):
                self.medium_combo.addItem(f"Host drive: {name}", f"host:{name}")

        self._capture(["list", subject], done)

    def _on_medium_changed(self, *_args):
        # Passthrough is a property of a real drive; VirtualBox rejects it for
        # an image, and an empty drive has nothing to pass through.
        real = str(self.medium_combo.currentData() or "").startswith("host:")
        self.passthrough_check.setEnabled(real)
        if not real:
            self.passthrough_check.setChecked(False)

    def _on_vm_changed(self, _index):
        vm_uuid = self.vm_combo.currentData()
        if not vm_uuid:
            return
        self._request += 1
        request = self._request
        self.ctl_combo.clear()
        self._controllers = []
        self._vminfo = {}
        self.slot_status.setText("")
        self.status.setText("Reading VM storage configuration…")

        def done(code: int, output: str):
            # A newer selection may have superseded this request meanwhile.
            if request != self._request:
                return
            if code != 0:
                self.status.setText(f"showvminfo failed (exit {code}).")
                return
            self._vminfo = parse_machinereadable(output)
            self._controllers = vm_storage_controllers(self._vminfo)
            self._types = controller_types(self._vminfo)
            for name, ports in self._controllers:
                self.ctl_combo.addItem(f"{name} ({ports} ports)", name)
            self.status.setText("" if self._controllers else "This VM has no storage controllers.")

        self._capture(["showvminfo", vm_uuid, "--machinereadable"], done)

    def _on_ctl_changed(self, index):
        if index < 0 or index >= len(self._controllers):
            return
        name, ports = self._controllers[index]
        self.port_spin.setMaximum(max(ports - 1, 0))
        self.port_spin.setValue(find_free_port(self._vminfo, name, ports))
        # SATA and friends only have device 0; offering 1 there produced a slot
        # nothing but VBoxManage's error could reject.
        self.device_spin.setMaximum(max_device_index(self._types.get(name, "")))
        if len(self._media) > 1:
            # Start at the first free port, then plan forward from there.
            self.port_spin.setValue(find_free_port(self._vminfo, name, ports))
        self._on_slot_changed()

    def _on_slot_changed(self, *_args):
        if len(self._media) > 1:
            self._replan()
            return
        occupant = self._occupant()
        self.slot_status.setText(
            f"Slot in use by {occupant} — attaching here replaces it."
            if occupant else ""
        )
        self.slot_status.setStyleSheet(color_css(warning_color()) if occupant else "")

    def _replan(self):
        """Work out which free slots a batch would fill, and say so.

        Occupied slots are stepped over rather than replaced: storageattach
        would overwrite one silently, and a batch is precisely where nobody is
        watching each command go by.
        """
        controller = self.ctl_combo.currentData()
        index = self.ctl_combo.currentIndex()
        if not controller or index < 0 or index >= len(self._controllers):
            self._slots = []
            return
        _name, ports = self._controllers[index]
        self._slots = free_slots(
            self._vminfo, controller, ports,
            max_device_index(self._types.get(controller, "")),
            len(self._media), self.port_spin.value(),
        )
        if len(self._slots) < len(self._media):
            self.slot_status.setText(
                f"Only {len(self._slots)} free slot(s) at or after port "
                f"{self.port_spin.value()} — {len(self._media)} media selected."
            )
            self.slot_status.setStyleSheet(color_css(warning_color()))
            return
        shown = ", ".join(f"{p}:{d}" for p, d in self._slots[:6])
        self.slot_status.setText(
            f"Will fill port:device {shown}"
            + ("…" if len(self._slots) > 6 else "")
        )
        self.slot_status.setStyleSheet(color_css(muted_color()))

    def _occupant(self) -> str:
        controller = self.ctl_combo.currentData()
        if not controller:
            return ""
        return slot_occupant(
            self._vminfo, controller, self.port_spin.value(), self.device_spin.value()
        )

    def _ok(self):
        if self.vm_combo.currentData() is None:
            QMessageBox.warning(self, "No VM", "No virtual machine selected.")
            return
        if self.ctl_combo.currentData() is None:
            QMessageBox.warning(self, "No controller", "This VM has no storage controller to attach to.")
            return
        if len(self._media) > 1:
            self._replan()
            if len(self._slots) < len(self._media):
                QMessageBox.warning(
                    self, "Not enough free slots",
                    f"“{self.ctl_combo.currentData()}” has {len(self._slots)} "
                    f"free slot(s) at or after port {self.port_spin.value()}, "
                    f"and {len(self._media)} media are selected. Pick another "
                    "controller, start earlier, or attach fewer.",
                )
                return
            self.accept()
            return
        occupant = self._occupant()
        if occupant:
            ret = QMessageBox.warning(
                self,
                "Slot in use",
                f"Port {self.port_spin.value()}, device {self.device_spin.value()} "
                f"of “{self.ctl_combo.currentData()}” already holds:\n\n{occupant}\n\n"
                "storageattach replaces it silently, detaching that medium from "
                "the VM. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        self.accept()

    def commands(self) -> list[list[str]]:
        """One storageattach per selected medium, into its allocated slot."""
        if len(self._media) == 1:
            return [self.args()]
        return [
            self._args_for(medium, port, device)
            for medium, (port, device) in zip(self._media, self._slots)
        ]

    def _args_for(self, medium: MediumRecord, port: int, device: int) -> list[str]:
        disk = self._kind == "disk"
        removable = self._kind in ("dvd", "floppy")
        return attach_args(
            self.vm_combo.currentData(),
            self.ctl_combo.currentData(),
            port,
            device,
            self._kind,
            medium.uuid,
            mtype=self.mtype_combo.currentData() if disk else "",
            discard=disk and self.discard_check.isChecked(),
            nonrotational=disk and self.nonrotational_check.isChecked(),
            hotpluggable=disk and self.hotpluggable_check.isChecked(),
            passthrough=(removable and self.passthrough_check.isEnabled()
                         and self.passthrough_check.isChecked()),
            tempeject=removable and self.tempeject_check.isChecked(),
            forceunmount=removable and self.forceunmount_check.isChecked(),
        )

    def args(self) -> list[str]:
        disk = self._kind == "disk"
        removable = self._kind in ("dvd", "floppy")
        return attach_args(
            self.vm_combo.currentData(),
            self.ctl_combo.currentData(),
            self.port_spin.value(),
            self.device_spin.value(),
            self._kind,
            self.medium_combo.currentData() or self._medium.uuid,
            mtype=self.mtype_combo.currentData() if disk else "",
            discard=disk and self.discard_check.isChecked(),
            nonrotational=disk and self.nonrotational_check.isChecked(),
            hotpluggable=disk and self.hotpluggable_check.isChecked(),
            passthrough=(removable and self.passthrough_check.isEnabled()
                         and self.passthrough_check.isChecked()),
            tempeject=removable and self.tempeject_check.isChecked(),
            forceunmount=removable and self.forceunmount_check.isChecked(),
        )
