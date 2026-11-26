"""MainWindow actions. Running commands: the batch queue and its per-command follow-ups, VM info
collection, registry snapshots, and what happens when the runner finishes.

Mixed into `window.MainWindow`; methods use its widgets, runner and helpers.
"""

from __future__ import annotations

from PyQt6.QtCore import QDateTime
from PyQt6.QtWidgets import QApplication
import os
import shlex

from .app_dialogs import PlanDialog
from .constants import LONG_OPERATION_SECONDS
from .domain import format_duration, is_destructive_command, parse_machinereadable
from .persistence import (
    add_library_path,
    append_history,
    plan_mode,
    snapshot_dir,
    write_registry_snapshot,
)
from .batches import BatchReport


class CommandQueueMixin:
    def _snapshot_registry(self, reason: str):
        """Write the registry aside before something destructive runs.

        Never a reason to stop: a snapshot that cannot be written is noted
        and the operation goes ahead, because refusing to delete a medium
        because a log file failed would be the worse behaviour.
        """
        path = write_registry_snapshot(self.media, reason)
        if path:
            self.runner.note(f"[snapshot] registry saved to {path}")
        else:
            self.runner.note(
                f"[snapshot] could not write a registry snapshot to "
                f"{snapshot_dir()}; continuing anyway"
            )

    def _notify_long_operation(self, code: int):
        """Flag up a command that ran long enough for the user to look away.

        No tray icon and no desktop notification service: the window itself
        asks for attention, which works the same everywhere and needs nothing
        installed.
        """
        verdict = "finished" if code == 0 else f"failed (exit {code})"
        elapsed = format_duration(self.runner.elapsed_seconds())
        self.runner.note(f"[done] {verdict} after {elapsed}")
        self.statusBar().showMessage(f"Command {verdict} after {elapsed}.")
        QApplication.alert(self)

    def _run_single(self, args: list[str], stdin_data: bytes | None = None,
                    secret_files: list[str] | None = None, title: str = "",
                    **kwargs) -> bool:
        """Run one visible command; in plan mode, show it first if it changes data.

        "Changes data" is `is_destructive_command`, the rule the history replay
        already uses: resize, move, re-point, attach/detach, encryption, FAT
        format, UUID stamping, a real repair, an extract that writes a file.
        Read-only commands (Info, a hex dump, a dry run, a password check) and
        commands that only create a new file run straight away.

        Declining clears the follow-up the caller may have parked for this
        command, and removes any password files made for it, which would
        otherwise only be removed when the command exits.
        """
        if plan_mode() and is_destructive_command(shlex.join(args)):
            note = ""
            if stdin_data or secret_files:
                note = ("<i>A password goes over standard input or through a "
                        "temporary file. It is not part of the script, and the "
                        "temporary file is removed when this closes.</i>")
            dlg = PlanDialog([args], title, self, note=note)
            if dlg.exec() != PlanDialog.RUN or not self._require_idle():
                for path in secret_files or []:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                self._followup = None
                self.statusBar().showMessage("Plan shown; nothing was run.")
                return False
        return self.runner.run(args, stdin_data, secret_files, **kwargs)

    def _run_queue(self, entries: list[tuple[list[str], bytes | None, object]],
                   title: str = "", before_each=None) -> bool:
        """Run commands one after another, stopping at the first failure.

        Each entry carries its own follow-up so a batch can do per-command
        bookkeeping (dropping a library entry for the medium *that* command
        removed, say) without a shared slot that the next command would claim.

        In plan mode the list is shown first and nothing runs until the user
        says so from there. before_each(args) prepares only the command about
        to run; returning False aborts the queue without launching it.
        """
        if not entries or not self._require_idle():
            return False
        if plan_mode():
            dlg = PlanDialog([args for args, _stdin, _follow in entries],
                             title, self)
            if dlg.exec() != PlanDialog.RUN:
                self.statusBar().showMessage("Plan shown; nothing was run.")
                return False
            if not self._require_idle():
                return False
        self._batch_report = BatchReport(entries, title)
        self._batch_current = None
        self._last_batch_prepare = before_each
        self._queue_before = before_each
        self._cmd_queue = list(entries)
        return self._start_queued_command()

    def _start_queued_command(self) -> bool:
        self._batch_current = self._batch_report.start() if self._batch_report else None
        args, stdin_data, followup = self._cmd_queue.pop(0)
        if self._queue_before and not self._queue_before(args):
            if self._batch_current:
                self._batch_current.state, self._batch_current.code = "failed", 1
                self._batch_report.skip_waiting()
                self._batch_current = None
            self._cmd_queue = []
            self._queue_before = None
            self._followup = None
            self.runner.note("[queue] aborted: could not prepare the next command")
            self._show_queue_status()
            self.refresh_media()
            return False
        self._followup = followup
        started = self.runner.run(args, stdin_data)
        self._show_queue_status()
        return started

    def _show_queue_status(self):
        self.runner.show_queue([a for a, _stdin, _follow in self._cmd_queue])
        if self._cmd_queue:
            self.statusBar().showMessage(f"Queue: {len(self._cmd_queue)} command(s) pending.")

    def skip_queue(self):
        """Drop everything still queued, leaving the running command alone."""
        dropped, self._cmd_queue = len(self._cmd_queue), []
        if self._batch_report:
            self._batch_report.skip_waiting()
        if dropped:
            self.runner.note(f"[queue] skipped {dropped} remaining command(s)")
            self.statusBar().showMessage(f"Skipped {dropped} queued command(s).")
        self._show_queue_status()

    def _collect_vminfo(self, vm_uuids: list[str], done):
        """Read `showvminfo` for several VMs in turn, then hand over the lot.

        Sequential rather than parallel for the same reason the refresh chain
        is: one VBoxSVC, and a burst of clients against it is how the service
        gets wedged.
        """
        infos: dict[str, dict[str, str]] = {}
        pending = list(vm_uuids)

        def step():
            if not pending:
                done(infos)
                return
            uuid = pending.pop(0)

            def got(code: int, output: str):
                if code == 0:
                    infos[uuid] = parse_machinereadable(output)
                step()

            if not self._capture(["showvminfo", uuid, "--machinereadable"], got):
                done(infos)

        step()

    def _on_runner_finished(self, code: int):
        code = self._finish_output(code)
        was_batch = self._batch_current is not None
        if self._batch_current:
            self._batch_current.code = code
            self._batch_current.state = "succeeded" if code == 0 else "failed"
            self._batch_current = None
        # Only what CommandRunner ran, which is only ever a mutating command:
        # the read-only refresh traffic goes through CaptureRunner and would
        # bury the history under `list`/`showvminfo` noise on every F5.
        if self.runner.last_command:
            append_history(
                self.runner.last_command,
                code,
                QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss"),
            )
        # Our own command just ran, so whatever the next refresh finds
        # changed is explained and should not be reported as drift.
        self._expect_change = True
        if self.runner.elapsed_seconds() >= LONG_OPERATION_SECONDS:
            self._notify_long_operation(code)
        followup, self._followup = self._followup, None
        if code == 0:
            if followup:
                followup()
            add, self._pending_library_add = self._pending_library_add, None
            if add and os.path.exists(add[1]):
                add_library_path(add[0], add[1])
                self.runner.note(f"[library] added {add[0]} {add[1]}")
        else:
            self._pending_library_add = None
            if self._cmd_queue:
                self.runner.note(f"[queue] aborted, {len(self._cmd_queue)} command(s) dropped")
                self._cmd_queue = []
                if self._batch_report:
                    self._batch_report.skip_waiting()
                self.runner.show_queue([])

        if self._cmd_queue:
            self._start_queued_command()
            return
        self._queue_before = None
        self.runner.show_queue([])
        if was_batch and self._batch_report:
            self.runner.note("[batch] " + self._batch_report.summary() + "; File → Batch results")
        self._sync_undo_change_action()
        # Most operations change the registry; refresh quietly.
        self.refresh_media()
