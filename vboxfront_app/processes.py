"""VBoxFront processes."""

from __future__ import annotations

from PyQt6.QtCore import QDateTime, QProcess, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
import lzma
import os
import re
import shlex

from .constants import STDIN_CHUNK
from .domain import eta_text, format_bytes
from .persistence import vboxmanage_path
from .theme import color_css, muted_color
from .widgets import ElidingLabel


class CommandRunner(QWidget):
    """A panel that runs VBoxManage commands and streams output."""

    finished = pyqtSignal(int)  # exit code
    skip_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.before_run = None
        self.proc: QProcess | None = None
        self._stdin_data: bytes | None = None
        self._stdin_stream = None
        self._stdin_total = 0
        self._stdin_sent = 0
        self._stdin_failed = False
        self._secret_files: list[str] = []
        self._progress_tail = ""
        self._started_at = QDateTime.currentDateTime()
        self._percent = 0
        self.last_command = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        self.cmd_label = ElidingLabel("Idle.")
        self.cmd_label.setStyleSheet(color_css(muted_color()))
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setMaximumWidth(180)
        self.progress.setVisible(False)
        self.elapsed_label = QLabel("")
        self.elapsed_label.setStyleSheet(color_css(muted_color()))
        self.copy_btn = QPushButton("Copy command")
        self.copy_btn.setEnabled(False)
        self.copy_btn.setToolTip("Copy the command line to the clipboard")
        self.copy_btn.clicked.connect(self.copy_command)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel)
        header.addWidget(self.cmd_label, 1)
        header.addWidget(self.elapsed_label)
        header.addWidget(self.progress)
        header.addWidget(self.copy_btn)
        header.addWidget(self.cancel_btn)
        layout.addLayout(header)

        # What is still queued behind the running command. A batch of thirty
        # compacts used to be a single number in the status bar: nothing said
        # what was left, and the only way out was killing the one in flight.
        queue_row = QHBoxLayout()
        self.queue_label = QLabel("")
        self.queue_label.setStyleSheet(color_css(muted_color()))
        self.skip_btn = QPushButton("Skip the rest")
        self.skip_btn.setToolTip(
            "Drop everything still queued. The command now running is left "
            "to finish — Cancel stops that one."
        )
        self.skip_btn.clicked.connect(self.skip_queue)
        queue_row.addWidget(self.queue_label, 1)
        queue_row.addWidget(self.skip_btn)
        self.queue_row_host = QWidget()
        self.queue_row_host.setLayout(queue_row)
        self.queue_row_host.setVisible(False)
        layout.addWidget(self.queue_row_host)

        self.queue_list = QListWidget()
        self.queue_list.setMaximumHeight(90)
        self.queue_list.setVisible(False)
        layout.addWidget(self.queue_list)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("Command output will appear here…")
        layout.addWidget(self.output)

        # A clone of a large fixed image is a coffee break, and the window
        # said nothing about how long was left. Ticking on a timer rather than
        # on output, because a long operation can be silent for minutes.
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)

    def set_status(self, text: str):
        self.cmd_label.setFullText(text)

    def is_busy(self) -> bool:
        return self.proc is not None and self.proc.state() != QProcess.ProcessState.NotRunning

    def run(self, args: list[str], stdin_data: bytes | None = None,
            secret_files: list[str] | None = None,
            stdin_stream=None, stdin_total: int = 0) -> bool:
        """Launch one command. `secret_files` are removed when it exits.

        `stdin_stream` is a binary file object whose contents are fed to the
        child a chunk at a time as the pipe drains, for `convertfromraw stdin`
        — a whole image cannot go through `stdin_data`, which is one buffer in
        memory. `stdin_total`, when known, turns the bytes written into a real
        percentage instead of waiting for the child to print one.
        """
        if self.is_busy():
            return False
        if self.before_run:
            try:
                self.before_run(args)
            except (OSError, ValueError) as e:
                self.last_command = " ".join(shlex.quote(a) for a in ["VBoxManage", *args])
                self.output.appendPlainText(f"[error] could not prepare command: {e}")
                if stdin_stream is not None:
                    stdin_stream.close()
                self._shred_secrets(secret_files or [])
                self.finished.emit(126)
                return False
        vbm = vboxmanage_path()
        if not vbm:
            # Name the command that could not be launched: `finished` fires from
            # here, and a stale last_command would file this failure against
            # whatever ran before it.
            self.last_command = " ".join(shlex.quote(a) for a in ["VBoxManage", *args])
            self.output.appendPlainText(f"$ {self.last_command}")
            self.output.appendPlainText("[error] VBoxManage not found in PATH.\n")
            self._shred_secrets(secret_files or [])
            if stdin_stream is not None:
                stdin_stream.close()
            self.finished.emit(127)
            return False
        self.proc = QProcess(self)
        self._stdin_data = stdin_data
        self._stdin_stream = stdin_stream
        self._stdin_total = stdin_total
        self._stdin_sent = 0
        self._stdin_failed = False
        self._secret_files = list(secret_files or [])
        self._progress_tail = ""
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._on_output)
        self.proc.bytesWritten.connect(self._pump_stdin)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)
        self.proc.started.connect(self._on_started)

        pretty = " ".join(shlex.quote(a) for a in [vbm, *args])
        self.last_command = pretty
        self.copy_btn.setEnabled(True)
        self.set_status(f"Running: {pretty}")
        self.output.appendPlainText(f"$ {pretty}")
        if stdin_data:
            self.output.appendPlainText("[password sent via stdin]")
        if stdin_stream is not None:
            self.output.appendPlainText(
                f"[streaming {format_bytes(stdin_total) or 'the image'} "
                "into standard input]"
            )
        if self._secret_files:
            self.output.appendPlainText(
                f"[{len(self._secret_files)} password file(s), mode 0600, "
                "removed when the command exits]"
            )
        self.cancel_btn.setEnabled(True)
        self.progress.setValue(0)
        self._started_at = QDateTime.currentDateTime()
        self._percent = 0
        self._elapsed_timer.start()
        self.proc.start(vbm, args)
        return True

    def cancel(self):
        if self.is_busy():
            self.output.appendPlainText("[cancelled by user]")
            self.proc.kill()

    def show_queue(self, pending: list[list[str]]):
        """Draw what is still waiting, or hide the row when nothing is."""
        self.queue_list.clear()
        vbm = os.path.basename(vboxmanage_path() or "VBoxManage")
        for args in pending:
            self.queue_list.addItem(
                " ".join(shlex.quote(a) for a in [vbm, *args])
            )
        self.queue_label.setText(
            f"{len(pending)} command(s) queued" if pending else ""
        )
        self.queue_row_host.setVisible(bool(pending))
        self.queue_list.setVisible(bool(pending))

    def skip_queue(self):
        self.skip_requested.emit()

    def note(self, text: str):
        self.output.appendPlainText(text)

    def discard_secrets(self):
        """Remove any password files the running command has not used up.

        Normally removal happens when QProcess.finished arrives; on the way
        out of the application it never does, so quitting mid-command would
        leave a 0600 password file in the temp directory.
        """
        self._shred_secrets()

    def copy_command(self):
        if self.last_command:
            QApplication.clipboard().setText(self.last_command)

    def _shred_secrets(self, paths: list[str] | None = None):
        """Unlink temporary password files; the legacy name does not imply secure erasure."""
        for path in (paths if paths is not None else self._secret_files):
            try:
                os.remove(path)
            except OSError:
                pass  # nothing useful to do; it is a temp file either way
        if paths is None:
            self._secret_files = []

    def _on_started(self):
        if not self.proc:
            return
        if self._stdin_data:
            self.proc.write(self._stdin_data)
            self._stdin_data = None
        if self._stdin_stream is not None:
            # Closing comes when the stream runs out, not here.
            self._pump_stdin()
            return
        # Always close it: nothing further is ever written, and a command that
        # tries to read stdin would otherwise block forever on an open pipe
        # with only Cancel to get out of it.
        self.proc.closeWriteChannel()

    def _pump_stdin(self, *_args):
        """Keep the child's stdin fed without holding the image in memory.

        Driven by `bytesWritten`, so the next chunk only goes in once the last
        one has drained — the event loop gets a turn between writes and the
        window stays usable through a multi-gigabyte import.
        """
        if not self.proc or self._stdin_stream is None:
            return
        while self.proc.bytesToWrite() < 2 * STDIN_CHUNK:
            try:
                chunk = self._stdin_stream.read(STDIN_CHUNK)
            except (OSError, EOFError, ValueError, lzma.LZMAError) as e:
                self.output.appendPlainText(f"[error] reading the image: {e}")
                self._stdin_failed = True
                self._close_stdin_stream()
                self.proc.closeWriteChannel()
                return
            if not chunk:
                if self._stdin_total and self._stdin_sent != self._stdin_total:
                    self.output.appendPlainText("[error] image size does not match the declared size")
                    self._stdin_failed = True
                self._close_stdin_stream()
                self.proc.closeWriteChannel()
                return
            if self._stdin_total and self._stdin_sent + len(chunk) > self._stdin_total:
                self.output.appendPlainText("[error] image exceeds the declared size")
                self._stdin_failed = True
                self._close_stdin_stream()
                self.proc.closeWriteChannel()
                return
            self.proc.write(chunk)
            self._stdin_sent += len(chunk)
            if self._stdin_total:
                self.progress.setVisible(True)
                self.progress.setValue(
                    min(100, int(100 * self._stdin_sent / self._stdin_total))
                )

    def _close_stdin_stream(self):
        stream, self._stdin_stream = self._stdin_stream, None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    def _on_output(self):
        if not self.proc:
            return
        data = bytes(self.proc.readAllStandardOutput()).decode(errors="replace")
        if not data:
            return
        self.output.moveCursor(self.output.textCursor().MoveOperation.End)
        self.output.insertPlainText(data)
        self.output.moveCursor(self.output.textCursor().MoveOperation.End)
        # VBoxManage long operations stream "0%...10%...100%" without newlines;
        # keep a small tail so a percentage split across chunks still matches.
        combined = self._progress_tail + data
        matches = re.findall(r"(\d{1,3})%", combined)
        if matches:
            pct = min(int(matches[-1]), 100)
            self.progress.setVisible(True)
            self.progress.setValue(pct)
            self._percent = pct
            self._tick_elapsed()
        self._progress_tail = combined[-8:]

    def _tick_elapsed(self):
        if not self.is_busy():
            return
        elapsed = self._started_at.msecsTo(QDateTime.currentDateTime()) / 1000
        self.elapsed_label.setText(eta_text(self._percent, elapsed))

    def _on_error(self, error):
        # FailedToStart fires *instead of* finished, so reset state here.
        if error == QProcess.ProcessError.FailedToStart:
            self.output.appendPlainText(
                "[error] Failed to start VBoxManage (not executable or missing).\n"
            )
            self._close_stdin_stream()
            self._shred_secrets()
            self._reset_ui()
            proc, self.proc = self.proc, None
            if proc:
                proc.deleteLater()
            self.finished.emit(126)

    def _on_finished(self, code: int, _status):
        self._on_output()
        if self._stdin_stream is not None:
            try:
                if self._stdin_stream.read(1):
                    self.output.appendPlainText("[error] command ended before consuming the image")
                    self._stdin_failed = True
            except (OSError, EOFError, ValueError, lzma.LZMAError) as e:
                self.output.appendPlainText(f"[error] reading the image: {e}")
                self._stdin_failed = True
        if (_status == QProcess.ExitStatus.CrashExit or self._stdin_failed
                or (self._stdin_total and self._stdin_sent != self._stdin_total)):
            code = code or 1
        self.output.appendPlainText(f"[exit {code}]\n")
        self._close_stdin_stream()
        self._shred_secrets()
        self._reset_ui()
        proc, self.proc = self.proc, None
        if proc:
            proc.deleteLater()
        self.finished.emit(code)

    def _reset_ui(self):
        self.set_status("Idle.")
        self.cancel_btn.setEnabled(False)
        self.progress.setVisible(False)
        self._elapsed_timer.stop()
        self.elapsed_label.setText("")

    def elapsed_seconds(self) -> float:
        return self._started_at.msecsTo(QDateTime.currentDateTime()) / 1000


class CaptureRunner(QProcess):
    """One-shot QProcess that buffers output and emits a callback.

    Deadlined, because a wedged VBoxManage otherwise strands the refresh chain
    forever: `_refreshing` stays set, every later refresh quietly folds into
    `_refresh_pending`, and the window just stops updating with nothing on
    screen to say why. VirtualBox can genuinely get into that state —
    `mediumio stream` deadlocks on 7.2.12 and takes VBoxSVC with it.
    """

    #: Generous on purpose: `list -l` walks every registered medium, and a
    #: large registry on slow storage is not a hang.
    TIMEOUT_MS = 120_000

    def __init__(self, parent, on_done):
        super().__init__(parent)
        self._buf = bytearray()
        self._on_done = on_done
        self.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.readyReadStandardOutput.connect(self._read)
        self.finished.connect(self._done)
        self.errorOccurred.connect(self._error)
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.setInterval(self.TIMEOUT_MS)
        self._deadline.timeout.connect(self._expired)
        self._deadline.start()

    def _expired(self):
        if self.state() == QProcess.ProcessState.NotRunning:
            return
        self.kill()
        on_done, self._on_done = self._on_done, None
        if on_done:
            on_done(124, "[error] VBoxManage did not answer within "
                         f"{self.TIMEOUT_MS // 1000}s; giving up on it. The "
                         "VirtualBox service may be wedged.\n")

    def _read(self):
        self._buf += bytes(self.readAllStandardOutput())

    def _done(self, code, _status):
        self._deadline.stop()
        self._read()
        if _status == QProcess.ExitStatus.CrashExit:
            code = code or 1
        on_done, self._on_done = self._on_done, None
        if on_done:
            on_done(code, self._buf.decode(errors="replace"))
        self.deleteLater()

    def _error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self._deadline.stop()
            on_done, self._on_done = self._on_done, None
            if on_done:
                on_done(126, "[error] Failed to start VBoxManage.\n")
            self.deleteLater()
