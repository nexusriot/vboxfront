"""MainWindow actions. Refreshing the listing: the capture chain, library re-opens, drift and
reclaimed-space reports.

Mixed into `window.MainWindow`; methods use its widgets, runner and helpers.
"""

from __future__ import annotations

import os
import time

from .commands import list_media_args
from .constants import KINDS, KIND_META
from .domain import (
    parse_capacity_mb,
    parse_hddbackends,
    parse_media_list,
    parse_systemproperties,
    reclaim_line,
    registry_fingerprint,
)
from .locate import parent_first
from .persistence import get_settings, load_library, vboxmanage_path
from .processes import CaptureRunner


class RefreshMixin:
    def _capture(self, args: list[str], on_done) -> bool:
        vbm = vboxmanage_path()
        if not vbm:
            self.statusBar().showMessage("VBoxManage not found.")
            return False
        CaptureRunner(self, on_done).start(vbm, args)
        return True

    def refresh_media(self):
        if self._refreshing:
            self._refresh_pending = True
            return
        vbm = vboxmanage_path()
        if not vbm:
            self.statusBar().showMessage("VBoxManage not found.")
            return
        self._refreshing = True
        self.statusBar().showMessage("Refreshing media…")

        # Re-open library paths first: modern VirtualBox drops standalone
        # registrations when VBoxSVC exits, so `list` alone would miss them.
        self._chain = []
        if not self.backends:
            self._chain.append((["list", "hddbackends"], self._backends_done))
            # Asked alongside the backends, and only once: it is what makes
            # "where should this new disk go" a sensible answer instead of ~.
            self._chain.append((["list", "systemproperties"], self._sysprops_done))
        for kind in KINDS:
            paths = load_library(kind)
            if kind == "disk":
                # A child cannot be opened before its parent (7.2.22: "Parent
                # medium with UUID … is not found"), whatever order the
                # entries were added in.
                paths = parent_first(paths)
            for path in paths:
                # One process per library entry, every refresh, was the single
                # biggest cost of F5 on a large library — and almost all of it
                # was re-opening files that had not moved since. The stamp is
                # dropped whenever the probe fails, so a broken entry is
                # retried rather than remembered as fine.
                if self._library_unchanged(path):
                    continue
                self._chain.append((["showmediuminfo", kind, path],
                                    self._library_open_done(kind, path)))
        for kind in KINDS:
            self._chain.append((list_media_args(kind), self._list_done(kind)))
        self._advance_chain()

    def _backends_done(self, code: int, output: str):
        if code != 0:
            # Retried on the next refresh, but said once: every refresh
            # otherwise repeats it into the output panel.
            if not self._backends_warned:
                self._backends_warned = True
                self.runner.note(
                    "[formats] could not read `list hddbackends`; falling back "
                    "to the built-in format list"
                )
            return
        self.backends = parse_hddbackends(output)

    def _sysprops_done(self, code: int, output: str):
        """Cache VirtualBox's default machine folder for `image_folder()`."""
        if code != 0:
            return
        folder = parse_systemproperties(output).get("Default machine folder", "")
        if folder:
            get_settings().setValue("vbox/machinefolder", folder)

    def _library_stamp(self, path: str) -> tuple[float, int] | None:
        try:
            info = os.stat(path)
        except OSError:
            return None
        return (info.st_mtime, info.st_size)

    def _library_unchanged(self, path: str) -> bool:
        """True when this entry was opened before and the file has not moved.

        VBoxSVC forgets a standalone registration when it exits, so an
        unchanged file still has to be re-opened *once per VBoxSVC lifetime* —
        which is why the cache is cleared whenever a listing comes back
        without the medium in it (see `_finish_refresh`).
        """
        stamp = self._library_stamp(path)
        return stamp is not None and self._library_seen.get(path) == stamp

    def _library_open_done(self, kind: str, path: str):
        def done(code: int, _output: str):
            if code != 0:
                self.runner.note(f"[library] could not open {kind} {path} (exit {code})")
                self._library_seen.pop(path, None)
                return
            stamp = self._library_stamp(path)
            if stamp is not None:
                self._library_seen[path] = stamp
        return done

    def _list_done(self, kind: str):
        def done(code: int, output: str):
            if code != 0:
                self.statusBar().showMessage(f"list {KIND_META[kind]['list']} failed (exit {code})")
                self.runner.note(output)
                self.media[kind] = []
                return
            self.media[kind] = parse_media_list(output)
        return done

    def _advance_chain(self):
        if not self._chain:
            self._finish_refresh()
            return
        args, cb = self._chain.pop(0)

        def step_done(code: int, output: str):
            cb(code, output)
            self._advance_chain()

        if not self._capture(args, step_done):
            self._chain = []
            self._finish_refresh()

    def _finish_refresh(self):
        for kind in KINDS:
            self.panes[kind].populate(self.media[kind])
        counts = ", ".join(
            f"{len(self.media[kind])} {KIND_META[kind]['list']}" for kind in KINDS
        )
        self.statusBar().showMessage(f"Registered media: {counts}.")
        self._report_reclaimed()
        self._check_drift()
        self._forget_vanished_library_entries()
        self._watch_quiet_until = time.monotonic() + 3
        self._refreshing = False
        if self._refresh_pending:
            self._refresh_pending = False
            self.refresh_media()

    def _check_drift(self):
        """Say so when the registry changed without a command of ours.

        A stale row is how an action lands on the wrong medium, and this
        window is not the only thing that writes the registry: the VirtualBox
        GUI, a VM starting, or a second copy of this app all do.
        """
        fingerprint = registry_fingerprint(self.media)
        if (self._fingerprint and fingerprint != self._fingerprint
                and not self._expect_change):
            self.runner.note(
                "[registry] changed since the last refresh, and not by "
                "anything run here — the VirtualBox GUI, a VM starting, or "
                "another copy of VBoxFront. The list above is current."
            )
        self._fingerprint = fingerprint
        self._expect_change = False

    def _forget_vanished_library_entries(self):
        """Drop cached stamps for library paths the listing does not show.

        That is what a VBoxSVC restart looks like from here: the file is
        unchanged but the registration is gone, so the entry has to be
        re-opened on the next refresh rather than skipped as "seen".
        """
        present = {
            rec.location for records in self.media.values() for rec in records
        }
        for path in list(self._library_seen):
            if path not in present:
                self._library_seen.pop(path, None)

    def _report_reclaimed(self):
        """Say what a compact actually gave back.

        Measured after the fact rather than predicted before it: `list -l`
        cannot see blocks that were allocated and then freed inside the guest,
        which is exactly what compacting reclaims.
        """
        if not self._size_before:
            return
        by_uuid = {r.uuid: r for r in self.media["disk"]}
        for uuid, (name, before) in self._size_before.items():
            rec = by_uuid.get(uuid)
            after = parse_capacity_mb(rec.size_on_disk) if rec else None
            line = reclaim_line(name, before, after)
            if line:
                self.runner.note(line)
        self._size_before = {}
