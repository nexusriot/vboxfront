"""MainWindow actions. Remove (unregister, trash or delete) and Undo delete.

Mixed into `window.MainWindow`; methods use its widgets, runner and helpers.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QMessageBox
import os
import time

from .domain import child_names, parse_in_use_entry
from .media_dialogs import RemoveDialog
from .models import TrashedFile
from .persistence import (
    add_library_path,
    move_to_trash,
    remove_library_path,
    restore_trashed,
    trash_enabled,
)


class RemovalActionsMixin:
    def remove_selected(self):
        media = self._require_selection_multi()
        if not media:
            return
        kind = self.current_kind()
        removable, blocked = [], []
        for m in media:
            if m.in_use:
                names = ", ".join(
                    e[0] for e in (parse_in_use_entry(x) for x in m.in_use) if e
                )
                blocked.append(
                    f"{os.path.basename(m.location) or m.uuid}: attached to {names}"
                )
                continue
            children = child_names(self.media[kind], m.uuid)
            if children:
                # closemedium answers VBOX_E_OBJECT_IN_USE for these; say which
                # child holds it rather than passing the COM error along.
                blocked.append(
                    f"{os.path.basename(m.location) or m.uuid}: "
                    f"{', '.join(children)} build{'s' if len(children) == 1 else ''} on it"
                )
                continue
            removable.append(m)
        if blocked and not removable:
            QMessageBox.warning(
                self, "Cannot remove",
                "Nothing here can be removed yet:\n\n" + "\n".join(blocked),
            )
            return
        if blocked:
            ret = QMessageBox.question(
                self, "Some cannot be removed",
                f"{len(blocked)} of {len(media)} media cannot be removed:\n\n"
                + "\n".join(blocked)
                + f"\n\nRemove the other {len(removable)}?",
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        dlg = RemoveDialog(removable, self)
        if not dlg.exec():
            return
        # With the trash on, closemedium is run *without* `--delete` and the
        # file is moved aside afterwards instead: VBoxManage's own delete
        # unlinks, and nothing can be undone after that.
        use_trash = dlg.delete_file and trash_enabled()
        batch: list[TrashedFile] = []
        entries = []
        for m in removable:
            args = ["closemedium", kind, m.uuid]
            if dlg.delete_file and not use_trash:
                args.append("--delete")
            # Per-command, not a shared pending slot: in a batch the shared one
            # would be claimed by whichever command finished.
            entries.append((
                args, None,
                self._removal_followup(kind, m.location, batch if use_trash else None),
            ))
        # After the queue starts, never before: a refused queue would leave an
        # undo entry for a delete that is not going to happen. The follow-ups
        # hold the same list, and none of them can run before the event loop
        # turns again.
        self._snapshot_registry("remove media")
        if self._run_queue(entries, "remove the selected media") and use_trash:
            self._trash.append(batch)
            self._trash_store.save(self._trash)

    def _removal_followup(self, kind: str, location: str,
                          batch: list[TrashedFile] | None):
        """Bookkeeping for one medium, once *its* closemedium has succeeded.

        The library entry goes either way. The backing file is only moved
        aside when the delete was asked for and the trash is on, and only
        here — before the command, VirtualBox still holds the medium, and the
        unregister could still fail with the file already gone.
        """
        def done():
            remove_library_path(kind, location)
            if batch is None or not location:
                return
            if not os.path.exists(location):
                self.runner.note(f"[trash] {location} was already gone")
                return
            try:
                dest = move_to_trash(location)
            except OSError as e:
                self.runner.note(
                    f"[trash] could not move {location}: {e} — the medium is "
                    "unregistered but the file was left where it is"
                )
                return
            batch.append(TrashedFile(kind, location, dest, time.time()))
            self._trash_store.save(self._trash)
            self.runner.note(
                f"[trash] moved {location} → {dest} "
                "(Media → Undo delete puts it back)"
            )
            self._auto_purge_trash()
            self._sync_undo_action()
        return done

    def undo_delete(self):
        """Put back the files the most recent delete moved aside."""
        if not self._require_idle():
            return
        batch = self._take_trash_batch()
        if not batch:
            self._sync_undo_action()
            return
        restored, failed = [], []
        for entry in batch:
            try:
                restore_trashed(entry)
            except OSError as e:
                self.runner.note(
                    f"[trash] could not restore {entry.original}: {e} — "
                    f"it is still at {entry.trashed}"
                )
                failed.append(entry)
                continue
            # The medium was unregistered by the remove, so putting the file
            # back is only half of it: the library entry is what re-opens it
            # on the next refresh and brings the row back.
            add_library_path(entry.kind, entry.original)
            self.runner.note(f"[trash] restored {entry.trashed} → {entry.original}")
            restored.append(entry)
        if failed:
            # Still in the trash, so still undoable: a second try can work
            # once whatever blocked this one is out of the way.
            self._trash.append(failed)
        self._trash_store.save(self._trash)
        self._sync_undo_action()
        if restored:
            self.statusBar().showMessage(
                f"Restored {len(restored)} file(s) from the trash."
            )
            self.refresh_media()
        if failed:
            QMessageBox.warning(
                self, "Undo delete",
                f"{len(failed)} of {len(batch)} files could not be put back; "
                "the output panel says why. They are still in the trash.",
            )

    def _take_trash_batch(self) -> list[TrashedFile]:
        """The newest batch that actually holds something, removed from the stack.

        A Remove whose files could not be moved (or that never ran) leaves an
        empty batch behind; those are skipped rather than counted as an undo
        that did nothing.
        """
        while self._trash and not self._trash[-1]:
            self._trash.pop()
        return self._trash.pop() if self._trash else []

    def _sync_undo_action(self):
        self.act_undo_delete.setEnabled(any(self._trash))
