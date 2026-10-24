"""VBoxFront media dialogs."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)
import os

from .commands import (
    autoreset_args,
    change_password_args,
    check_password_args,
    decrypt_args,
    encrypt_args,
    mediumio_cat_args,
    mediumproperty_args,
    properties_args,
    properties_undo_args,
    repairhd_args,
    sethdparentuuid_args,
    sethduuid_args,
    setlocation_args,
    write_secret_file,
)
from .constants import ENCRYPTION_CIPHERS, HEX_DUMP_WARN_BYTES, KIND_META, MEDIUM_TYPES, MIXED_TYPE
from .domain import (
    format_mbytes,
    is_differencing,
    is_uuid,
    medium_type_word,
    parse_byte_size,
    same_file,
    state_word,
)
from .models import MediumBackend, MediumRecord
from .persistence import remember_folder, remembered_folder, trash_dir_for, trash_enabled
from .theme import color_css, warning_color


class RemoveDialog(QDialog):
    """Unregister a medium, optionally deleting the backing file."""

    def __init__(self, medium: MediumRecord | list[MediumRecord], parent=None):
        super().__init__(parent)
        media = medium if isinstance(medium, list) else [medium]
        self.setWindowTitle("Remove medium" if len(media) == 1 else "Remove media")
        self.delete_file = False
        self._media = media
        # Read once: what the second confirmation promises has to match what
        # the removal will actually do, and the setting cannot change while
        # this dialog is up.
        self._trash = trash_enabled()

        layout = QFormLayout(self)
        if len(media) == 1:
            one = media[0]
            layout.addRow(QLabel(f"<b>{os.path.basename(one.location)}</b>"))
            layout.addRow("Location:", QLabel(one.location or "?"))
            layout.addRow("UUID:", QLabel(one.uuid))
        else:
            layout.addRow(QLabel(f"<b>{len(media)} media</b>"))
            listing = QListWidget()
            listing.setMaximumHeight(140)
            for rec in media:
                listing.addItem(rec.location or rec.uuid)
            layout.addRow("", listing)

        self.mode = QComboBox()
        # Safe default first: unregister but keep the file on disk.
        self.mode.addItems([
            "Unregister only (keep file)",
            "Delete file (moved to trash)" if self._trash
            else "Delete file from disk",
        ])
        layout.addRow("Action:", self.mode)
        layout.addRow(
            QLabel(
                "<i>Unregister removes the medium from VirtualBox's media "
                "registry and from VBoxFront's library. Delete also moves the "
                "file into a trash folder, which <b>Media → Undo delete</b> "
                "puts back, including after restarting the application.</i>"
                if self._trash else
                "<i>Unregister removes the medium from VirtualBox's media "
                "registry and from VBoxFront's library. Delete also erases "
                "the file permanently (the trash is off in Settings).</i>"
            )
        )

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Remove")
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _ok(self):
        delete = self.mode.currentIndex() == 1
        if delete:
            ret = QMessageBox.warning(
                self,
                "Move to trash?" if self._trash else "Delete file?",
                self._confirmation(),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        self.delete_file = delete
        self.accept()

    def _confirmation(self) -> str:
        """What the second confirmation asks — recoverable or not.

        Naming the destination is the point of the trash wording: "somewhere
        safe" is not something anyone can check afterwards.
        """
        count = len(self._media)
        plural = f"{count} disk image files" if count > 1 else "the disk image file"
        if not self._trash:
            return f"This will permanently delete {plural}. Continue?"
        if count == 1 and self._media[0].location:
            where = f"\n\n{trash_dir_for(self._media[0].location)}"
        else:
            where = "\n\nEach one goes to the trash folder for its own location."
        return (
            f"This moves {plural} to the trash, where Media → Undo delete "
            f"can put {'them' if count > 1 else 'it'} back for the rest of this "
            f"session.{where}\n\nContinue?"
        )


class ContentsDialog(QDialog):
    """Read a byte range out of an image without mounting it.

    `mediumio cat` opens the image through VirtualBox's own backend stack, so
    this works on any supported format, on differencing chains, and on
    encrypted media (which refuse to be read without their password) — none of
    which a host-side loop mount can manage.
    """

    def __init__(self, kind: str, medium: MediumRecord, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Image contents")
        self._kind = kind
        self._medium = medium
        self.command: tuple[list[str], bytes | None] | None = None

        encrypted = medium.encryption not in ("", "disabled")
        layout = QFormLayout(self)
        layout.addRow(QLabel(f"<b>{os.path.basename(medium.location)}</b>"))
        layout.addRow("Capacity:", QLabel(medium.capacity or "?"))

        self.offset_edit = QLineEdit("0")
        self.offset_edit.setToolTip("Decimal, 0x-hex, or a K/M/G suffix")
        layout.addRow("Offset:", self.offset_edit)
        self.size_edit = QLineEdit("512")
        self.size_edit.setToolTip("Decimal, 0x-hex, or a K/M/G suffix")
        layout.addRow("Length:", self.size_edit)

        self.hex_radio = QRadioButton("Hex dump into the output panel")
        self.save_radio = QRadioButton("Save the range to a file")
        self.hex_radio.setChecked(True)
        layout.addRow(self.hex_radio)
        layout.addRow(self.save_radio)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        layout.addRow("Output file:", path_row)

        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setEnabled(encrypted)
        self.password.setPlaceholderText(
            "" if encrypted else "not encrypted — no password needed"
        )
        layout.addRow("Password:", self.password)
        layout.addRow(QLabel(
            "<i>Reads only; the image is opened read-only. A hex dump of a "
            "large range will be large — the first sectors are usually what "
            "you want.</i>"
        ))

        self.hex_radio.toggled.connect(self._sync_fields)
        self._sync_fields()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Read")
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _sync_fields(self):
        saving = self.save_radio.isChecked()
        self.path_edit.setEnabled(saving)

    def _browse(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save range as", remembered_folder("extract"), "All files (*)"
        )
        if path:
            self.path_edit.setText(path)

    def _ok(self):
        offset = parse_byte_size(self.offset_edit.text() or "0")
        size = parse_byte_size(self.size_edit.text())
        if offset is None:
            QMessageBox.warning(self, "Bad offset", "Offset must be a byte count.")
            return
        if not size:
            QMessageBox.warning(self, "Bad length", "Length must be a byte count above zero.")
            return
        output = ""
        if self.save_radio.isChecked():
            output = self.path_edit.text().strip()
            if not output:
                QMessageBox.warning(self, "Missing file", "Choose an output file.")
                return
            if same_file(output, self._medium.location):
                QMessageBox.warning(
                    self, "Same file",
                    "That is the image being read. Choose another output file.",
                )
                return
            # mediumio overwrites its --output silently, so ask first.
            if os.path.exists(output):
                ret = QMessageBox.question(
                    self, "Overwrite?", f"{output} already exists. Overwrite?"
                )
                if ret != QMessageBox.StandardButton.Yes:
                    return
        if self.hex_radio.isChecked() and size > HEX_DUMP_WARN_BYTES:
            ret = QMessageBox.question(
                self, "Large dump",
                f"{format_mbytes(size // (1024 * 1024)) or f'{size} bytes'} of "
                f"hex is about {size // 16:,} lines in the output panel, which "
                "will be slow.\n\nDump it anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        if self.password.isEnabled() and not self.password.text():
            QMessageBox.warning(
                self, "Password needed",
                "This medium is encrypted; VBoxManage refuses to read it "
                "without the password.",
            )
            return
        if output:
            remember_folder("extract", output)
        self.command = mediumio_cat_args(
            self._kind, self._medium.uuid, offset=offset, size=size,
            hex_dump=self.hex_radio.isChecked(), output=output,
            password=self.password.text() if self.password.isEnabled() else "",
        )
        self.accept()


class ResolveDialog(QDialog):
    """Rescue a medium VirtualBox can no longer read.

    Two different faults look identical in the listing. If the *file* is gone
    from where the registry expects it, the registry is what needs fixing
    (`modifymedium --setlocation`). If the file is there but unreadable, the
    image itself may be damaged, and `internalcommands repairhd` can try —
    with a dry run first, since a repair rewrites the image in place.
    """

    def __init__(self, kind: str, medium: MediumRecord, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Resolve medium")
        self._kind = kind
        self._medium = medium
        self.command: list[str] | None = None

        # Ask the filesystem, not the record: VBoxSVC caches an open medium, so
        # `State:` can still read "created" for a file that has already moved
        # out from under it (and vice versa after a repair).
        file_present = bool(medium.location) and os.path.exists(medium.location)
        repairable = kind == "disk" and file_present

        layout = QFormLayout(self)
        layout.addRow(QLabel(f"<b>{os.path.basename(medium.location)}</b>"))
        layout.addRow("State:", QLabel(state_word(medium) or "unknown"))
        layout.addRow(
            "File:",
            QLabel("present" if file_present else "missing at the registered path"),
        )
        if medium.access_error:
            reason = QLabel(medium.access_error)
            reason.setWordWrap(True)
            reason.setStyleSheet(color_css(warning_color()))
            layout.addRow("VirtualBox says:", reason)

        self.relocate_radio = QRadioButton("Point the registry at another file")
        self.dryrun_radio = QRadioButton("Check the image for damage (dry run)")
        self.repair_radio = QRadioButton("Repair the image (rewrites it in place)")
        self.relocate_radio.setChecked(True)
        for radio in (self.relocate_radio, self.dryrun_radio, self.repair_radio):
            layout.addRow(radio)
        self.dryrun_radio.setEnabled(repairable)
        self.repair_radio.setEnabled(repairable)
        if not repairable:
            hint = ("repairhd needs the file to exist"
                    if kind == "disk" else "repairhd only handles hard disks")
            self.dryrun_radio.setToolTip(hint)
            self.repair_radio.setToolTip(hint)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(medium.location)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        layout.addRow("New location:", path_row)

        layout.addRow(QLabel(
            "<i>Relocating only updates VirtualBox's registry; the file is not "
            "moved or touched. Repair uses <tt>VBoxManage internalcommands</tt>, "
            "which Oracle ships unsupported — take a copy of the image first.</i>"
        ))

        self.relocate_radio.toggled.connect(self._sync_fields)
        self._sync_fields()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _sync_fields(self):
        self.path_edit.setEnabled(self.relocate_radio.isChecked())

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate the image file",
            os.path.dirname(self._medium.location) or remembered_folder("locate"),
            KIND_META[self._kind]["filter"],
        )
        if path:
            self.path_edit.setText(path)

    def _ok(self):
        if self.relocate_radio.isChecked():
            path = self.path_edit.text().strip()
            if not path:
                QMessageBox.warning(self, "Missing path", "Choose the image file.")
                return
            if not os.path.exists(path):
                QMessageBox.warning(
                    self, "No such file",
                    f"{path} does not exist. Relocating to it would leave the "
                    "medium inaccessible.",
                )
                return
            if same_file(path, self._medium.location):
                QMessageBox.warning(
                    self, "Same file",
                    "That is the location already registered — pick the file's "
                    "new home, or repair the image instead.",
                )
                return
            self.command = setlocation_args(self._kind, self._medium.uuid, path)
        elif self.dryrun_radio.isChecked():
            self.command = repairhd_args(
                self._medium.location, self._medium.fmt, dry_run=True
            )
        else:
            ret = QMessageBox.warning(
                self,
                "Repair image?",
                "repairhd rewrites the image in place and is an unsupported "
                "development tool. Copy the file first if it holds anything you "
                "cannot lose.\n\nRun the repair?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
            self.command = repairhd_args(self._medium.location, self._medium.fmt)
        self.accept()


class UuidToolsDialog(QDialog):
    """Stamp a new UUID (or parent UUID) into an image file.

    This is the way out of "cannot register the hard disk because a medium with
    the same UUID already exists" after copying a .vdi: the copy carries the
    original's UUID until `sethduuid` gives it a fresh one. It writes to the
    file, so it must not be aimed at an image VirtualBox has registered — the
    registry would still hold the old UUID.
    """

    def __init__(self, registered: dict[str, str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Image UUID tools")
        self._registered = registered
        self.command: list[str] | None = None

        layout = QFormLayout(self)
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("image file to stamp")
        self.path_edit.textChanged.connect(self._sync_fields)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        layout.addRow("Image file:", path_row)

        self.new_radio = QRadioButton("Assign a freshly generated UUID")
        self.own_radio = QRadioButton("Assign this UUID")
        self.parent_radio = QRadioButton("Assign this parent UUID")
        self.new_radio.setChecked(True)
        for radio in (self.new_radio, self.own_radio, self.parent_radio):
            layout.addRow(radio)
            radio.toggled.connect(self._sync_fields)

        self.uuid_edit = QLineEdit()
        self.uuid_edit.setPlaceholderText("00000000-0000-0000-0000-000000000000")
        layout.addRow("UUID:", self.uuid_edit)

        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        layout.addRow(self.warning)
        layout.addRow(QLabel(
            "<i>Uses <tt>VBoxManage internalcommands</tt>, which Oracle ships "
            "unsupported. Aim it at an unregistered copy: stamping a registered "
            "image desynchronises it from the registry.</i>"
        ))

        self._sync_fields()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Image file", remembered_folder("locate"),
            KIND_META["disk"]["filter"],
        )
        if path:
            self.path_edit.setText(path)

    def _registered_as(self) -> str:
        path = self.path_edit.text().strip()
        for location, uuid in self._registered.items():
            if same_file(path, location):
                return uuid
        return ""

    def _sync_fields(self, *_args):
        self.uuid_edit.setEnabled(not self.new_radio.isChecked())
        uuid = self._registered_as()
        self.warning.setText(
            f"<b>This image is registered</b> (UUID {uuid}). Stamping it will "
            "leave the registry pointing at a UUID the file no longer has — "
            "remove it from VirtualBox first."
            if uuid else ""
        )
        self.warning.setStyleSheet(color_css(warning_color()) if uuid else "")

    def _ok(self):
        path = self.path_edit.text().strip()
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "No such file", "Choose an existing image file.")
            return
        if not self.new_radio.isChecked() and not is_uuid(self.uuid_edit.text()):
            QMessageBox.warning(self, "Bad UUID", "Enter a full 36-character UUID.")
            return
        if self._registered_as():
            ret = QMessageBox.warning(
                self, "Registered image",
                "VirtualBox has this image registered under its current UUID. "
                "Stamping it anyway will make the registry entry inaccessible."
                "\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        uuid = self.uuid_edit.text().strip().strip("{}")
        if self.parent_radio.isChecked():
            self.command = sethdparentuuid_args(path, uuid)
        else:
            self.command = sethduuid_args(path, "" if self.new_radio.isChecked() else uuid)
        self.accept()


class PropertiesDialog(QDialog):
    """Edit medium type and description (modifymedium --type/--description).

    Takes a selection as readily as one medium. Where the selection disagrees
    the field says so and is sent only if it is edited — a batch must not
    quietly stamp the first medium's description onto the rest.
    """

    def __init__(self, medium: MediumRecord | list[MediumRecord], parent=None):
        super().__init__(parent)
        media = medium if isinstance(medium, list) else [medium]
        self._media = media
        self._medium = media[0]
        medium = self._medium
        self.setWindowTitle(
            "Medium properties" if len(media) == 1 else "Properties of the selection"
        )
        self.new_type: str | None = None
        self.new_description: str | None = None
        self.new_autoreset: bool | None = None

        types = {medium_type_word(m.medium_type) for m in media}
        descriptions = {m.description for m in media}
        self._mixed_type = len(types) > 1
        self._mixed_description = len(descriptions) > 1

        layout = QFormLayout(self)
        if len(media) == 1:
            layout.addRow(QLabel(f"<b>{os.path.basename(medium.location)}</b>"))
        else:
            names = ", ".join(
                os.path.basename(m.location) or m.uuid for m in media[:4]
            )
            layout.addRow(QLabel(
                f"<b>{len(media)} media</b> — {names}"
                + ("…" if len(media) > 4 else "")
            ))

        self.type_combo = QComboBox()
        if self._mixed_type:
            self.type_combo.addItem(MIXED_TYPE)
        self.type_combo.addItems(MEDIUM_TYPES)
        current = medium_type_word(medium.medium_type)
        if self._mixed_type:
            self.type_combo.setCurrentText(MIXED_TYPE)
        elif current in MEDIUM_TYPES:
            self.type_combo.setCurrentText(current)
        # VirtualBox refuses any type change on a differencing medium
        # ("Cannot change the type of medium ... because it is a differencing
        # medium"), so say so here instead of surfacing that as a COM error.
        # A differencing medium refuses any type change, so a selection
        # containing one cannot have its types set in a single pass either.
        self.type_combo.setEnabled(not any(is_differencing(m) for m in media))
        layout.addRow("Type:", self.type_combo)

        # --autoreset governs whether an immutable disk is rolled back on every
        # VM start; it is meaningless on the other types.
        self.autoreset = QCheckBox("Reset on every VM start (immutable disks)")
        resets = {m.auto_reset.lower() == "on" for m in media}
        self.autoreset.setChecked(len(resets) == 1 and resets.pop())
        self._autoreset_was = self.autoreset.isChecked()
        self._mixed_autoreset = len({m.auto_reset.lower() for m in media}) > 1
        layout.addRow("Auto-reset:", self.autoreset)

        self.desc_edit = QPlainTextEdit()
        if self._mixed_description:
            self.desc_edit.setPlaceholderText(
                "(these media have different descriptions — type to set them "
                "all, leave empty to keep each one's own)"
            )
        else:
            self.desc_edit.setPlainText(medium.description)
        self.desc_edit.setMaximumHeight(90)
        layout.addRow("Description:", self.desc_edit)
        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        layout.addRow(self.hint)

        self.type_combo.currentTextChanged.connect(self._sync_fields)
        self._sync_fields()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _sync_fields(self, *_args):
        immutable = self.type_combo.currentText() == "immutable"
        self.autoreset.setEnabled(immutable and self.type_combo.isEnabled())
        if not self.type_combo.isEnabled():
            self.hint.setText(
                "<i>This is a differencing image; VirtualBox will not change "
                "the type of one. Its type follows the chain it belongs to.</i>"
            )
        else:
            self.hint.setText(
                "<i>Type changes require the disk to be detached from VMs.</i>"
            )

    def _ok(self):
        chosen = self.type_combo.currentText()
        if (self.type_combo.isEnabled() and chosen != MIXED_TYPE
                and (self._mixed_type
                     or chosen != medium_type_word(self._medium.medium_type))):
            self.new_type = chosen
        desc = self.desc_edit.toPlainText()
        if self._mixed_description:
            # Only an edit counts: an empty box means "leave each one alone",
            # not "blank every description in the selection".
            if desc:
                self.new_description = desc
        elif desc != self._medium.description:
            self.new_description = desc
        if (self.autoreset.isEnabled()
                and (self._mixed_autoreset
                     or self.autoreset.isChecked() != self._autoreset_was)):
            self.new_autoreset = self.autoreset.isChecked()
        self.accept()

    def args(self, kind: str) -> list[str] | None:
        return properties_args(kind, self._medium.uuid, self.new_type, self.new_description)

    def autoreset_command(self) -> list[str] | None:
        """A separate call: --autoreset is disk-only and independent of the
        type/description edit, which may itself be a no-op."""
        if self.new_autoreset is None:
            return None
        return autoreset_args(self._medium.uuid, self.new_autoreset)

    def commands(self, kind: str) -> list[list[str]]:
        """Every call the edit needs, for one medium or for the selection."""
        return [c for medium in self._media for c in self.commands_for(kind, medium)]

    def commands_for(self, kind: str, medium: MediumRecord) -> list[list[str]]:
        """The calls the edit needs for one medium of the selection."""
        commands = []
        edit = properties_args(kind, medium.uuid, self.new_type,
                               self.new_description)
        if edit:
            commands.append(edit)
        if self.new_autoreset is not None and kind == "disk":
            commands.append(autoreset_args(medium.uuid, self.new_autoreset))
        return commands

    def undo_commands_for(self, kind: str, medium: MediumRecord) -> list[list[str]]:
        return properties_undo_args(kind, medium, self.new_type, self.new_description,
                                    self.new_autoreset)


class EncryptDialog(QDialog):
    """Set, change or remove disk encryption (needs the Extension Pack)."""

    #: A third exec() result, distinct from Accepted/Rejected: the user asked to
    #: verify the current password rather than to change anything.
    CHECK_ONLY = 2

    def __init__(self, medium: MediumRecord, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Disk encryption")
        self._medium = medium
        #: Set when "Check current password" is pressed, so the caller runs
        #: checkmediumpwd instead of closing the dialog.
        self.check_command: tuple[list[str], bytes] | None = None

        encrypted = medium.encryption not in ("", "disabled")
        layout = QFormLayout(self)
        layout.addRow(QLabel(f"<b>{os.path.basename(medium.location)}</b>"))
        state = medium.encryption or "unknown"
        if medium.cipher:
            state += f" — {medium.cipher}"
        if medium.password_id:
            state += f" (password ID {medium.password_id})"
        layout.addRow("Current state:", QLabel(state))

        self.encrypt_radio = QRadioButton("Encrypt (set password)")
        self.change_radio = QRadioButton("Change password (stay encrypted)")
        self.decrypt_radio = QRadioButton("Decrypt (remove encryption)")
        self.encrypt_radio.setChecked(not encrypted)
        self.decrypt_radio.setChecked(encrypted)
        self.encrypt_radio.setEnabled(not encrypted)
        self.change_radio.setEnabled(encrypted)
        self.decrypt_radio.setEnabled(encrypted)
        for radio in (self.encrypt_radio, self.change_radio, self.decrypt_radio):
            layout.addRow(radio)

        self.old_password = QLineEdit()
        self.old_password.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addRow("Current password:", self.old_password)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addRow("New password:", self.password)
        self.confirm = QLineEdit()
        self.confirm.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addRow("Confirm:", self.confirm)

        self.check_btn = QPushButton("Check current password")
        self.check_btn.setToolTip(
            "Run checkmediumpwd to confirm the current password before using it"
        )
        self.check_btn.clicked.connect(self._check_password)
        layout.addRow("", self.check_btn)

        self.pwid = QLineEdit(os.path.basename(medium.location) or medium.uuid)
        layout.addRow("Password ID:", self.pwid)
        self.cipher = QComboBox()
        self.cipher.addItems(ENCRYPTION_CIPHERS)
        if medium.cipher in ENCRYPTION_CIPHERS:
            self.cipher.setCurrentText(medium.cipher)
        layout.addRow("Cipher:", self.cipher)
        layout.addRow(QLabel("<i>Requires the VirtualBox Extension Pack.</i>"))

        for radio in (self.encrypt_radio, self.change_radio, self.decrypt_radio):
            radio.toggled.connect(self._sync_fields)
        self._sync_fields()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _check_password(self):
        if not self.old_password.text():
            QMessageBox.warning(
                self, "Missing password", "Enter the password to check."
            )
            return
        self.check_command = check_password_args(
            self._medium.uuid, self.old_password.text()
        )
        self.done(self.CHECK_ONLY)

    def _sync_fields(self):
        setting = self.encrypt_radio.isChecked() or self.change_radio.isChecked()
        self.check_btn.setEnabled(not self.encrypt_radio.isChecked())
        self.old_password.setEnabled(not self.encrypt_radio.isChecked())
        self.password.setEnabled(setting)
        self.confirm.setEnabled(setting)
        self.pwid.setEnabled(setting)
        self.cipher.setEnabled(setting)

    def _ok(self):
        # Validation only — the secrets stay in the widgets until
        # command_parts() is asked for them, so a command that never launches
        # leaves nothing behind on disk.
        if not self.encrypt_radio.isChecked() and not self.old_password.text():
            QMessageBox.warning(
                self, "Missing password", "Enter the medium's current password."
            )
            return
        if not self.decrypt_radio.isChecked():
            if not self.password.text():
                QMessageBox.warning(self, "Missing password", "Enter a new password.")
                return
            if self.password.text() != self.confirm.text():
                QMessageBox.warning(self, "Mismatch", "Passwords do not match.")
                return
            if not self.pwid.text().strip():
                QMessageBox.warning(self, "Missing ID", "Enter a password identifier.")
                return
        self.accept()

    def command_parts(self) -> tuple[list[str], bytes | None, list[str]]:
        """(argv, stdin payload, secret files to remove once it exits).

        Call this immediately before launching, never at accept time: changing
        a password needs two secrets, and two secrets cannot share one stdin
        stream, so they have to be spilled to 0600 files that must not outlive
        the command.
        """
        uuid = self._medium.uuid
        if self.encrypt_radio.isChecked():
            args, stdin_data = encrypt_args(
                uuid, self.password.text(), self.pwid.text().strip(),
                self.cipher.currentText(),
            )
            return args, stdin_data, []
        if self.decrypt_radio.isChecked():
            args, stdin_data = decrypt_args(uuid, self.old_password.text())
            return args, stdin_data, []
        old_file = write_secret_file(self.old_password.text())
        new_file = write_secret_file(self.password.text())
        args = change_password_args(
            uuid, old_file, new_file, self.pwid.text().strip(),
            self.cipher.currentText(),
        )
        return args, None, [old_file, new_file]


class MediumPropertyDialog(QDialog):
    """View and edit the format-specific properties of one medium.

    The values come out of the listing that is already on screen (`list -l`
    prints a `Property:` line per set property) and the *names* come from the
    format's own schema in `list hddbackends`, so a property that exists but
    has never been set still gets a field, with its default as the placeholder.
    Nothing is read back with `mediumproperty get`: that would be one process
    per property for data the refresh already carries.
    """

    def __init__(self, kind: str, medium: MediumRecord,
                 backends: list[MediumBackend] | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Medium properties (format)")
        self._kind = kind
        self._medium = medium
        self._original: dict[str, str] = {}
        self._edits: dict[str, QLineEdit] = {}

        for entry in medium.properties:
            name, sep, value = entry.partition("=")
            if sep:
                self._original[name.strip()] = value.strip()

        # `Storage format` comes back lower-cased ("vdi") while backend ids are
        # upper-case, so the lookup has to normalise or it never matches.
        self._schema = {}
        for backend in backends or []:
            if backend.id.upper() == medium.fmt.upper():
                self._schema = {prop["name"]: prop for prop in backend.properties}
                break

        outer = QVBoxLayout(self)
        outer.addWidget(QLabel(
            f"<b>{os.path.basename(medium.location) or medium.uuid}</b> "
            f"({medium.fmt or '?'})"
        ))
        self.form_host = QWidget()
        self.form = QFormLayout(self.form_host)
        outer.addWidget(self.form_host)

        for name in sorted(set(self._original) | set(self._schema)):
            self._add_row(name)
        if not self._edits:
            self.form.addRow(QLabel(
                "<i>This format has no editable properties, and none are set.</i>"
            ))

        hint = QLabel(
            "<i>Clearing a field deletes the property; leaving it empty when it "
            "was never set does nothing. Properties are format-specific — "
            "VirtualBox rejects a name the backend does not know. Some are only "
            "read when the image is created (VDI's AllocationBlockSize among "
            "them): the command succeeds and the value stays as it was.</i>"
        )
        hint.setWordWrap(True)
        outer.addWidget(hint)

        row = QHBoxLayout()
        add = QPushButton("Add property…")
        add.clicked.connect(self._add_custom)
        row.addWidget(add)
        row.addStretch(1)
        outer.addLayout(row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _add_row(self, name: str):
        edit = QLineEdit(self._original.get(name, ""))
        prop = self._schema.get(name, {})
        if prop.get("default"):
            edit.setPlaceholderText(f"default {prop['default']}")
        label = f"{name} ({prop['type']}):" if prop.get("type") else f"{name}:"
        self.form.addRow(label, edit)
        self._edits[name] = edit

    def _add_custom(self):
        name, ok = QInputDialog.getText(self, "Add property", "Property name:")
        name = name.strip()
        if not ok or not name or name in self._edits:
            return
        self._add_row(name)

    def commands(self) -> list[list[str]]:
        """One mediumproperty call per changed field, in a stable order."""
        commands = []
        for name, edit in self._edits.items():
            value = edit.text().strip()
            before = self._original.get(name, "")
            if value == before:
                continue
            if value:
                commands.append(
                    mediumproperty_args(self._kind, self._medium.uuid, "set", name, value)
                )
            elif before:
                commands.append(
                    mediumproperty_args(self._kind, self._medium.uuid, "delete", name)
                )
        return commands
