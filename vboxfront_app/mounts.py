"""Read-only browsing of an image's files through vboximg-mount.

vboximg-mount ships with VirtualBox and exposes a medium through FUSE: the
raw disk as `vhdd`, each partition as `vol<N>`, and — with `-g` — every guest
filesystem it can read as an `fs<N>` directory, with no root, no loop device
and no host filesystem driver. Read-only unless `--rw` is passed, which this
app never does.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

from PyQt6.QtCore import QObject, QProcess, QTimer, pyqtSignal

from .persistence import vboxmanage_path


def vboximg_mount_path() -> str | None:
    """vboximg-mount beside the configured VBoxManage, else from PATH."""
    vbm = vboxmanage_path()
    if vbm:
        beside = os.path.join(os.path.dirname(os.path.realpath(vbm)), "vboximg-mount")
        if os.access(beside, os.X_OK):
            return beside
    return shutil.which("vboximg-mount")


def unmount_command(mountpoint: str) -> list[str] | None:
    if sys.platform == "darwin":
        return ["umount", mountpoint]
    tool = shutil.which("fusermount3") or shutil.which("fusermount")
    return [tool, "-u", mountpoint] if tool else None


def mount_args(medium: str, mountpoint: str) -> list[str]:
    return ["--image", medium, "-g", mountpoint]


def mount_root() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return os.path.join(base, "vboxfront-mounts")


def explain_failure(output: str) -> str:
    """vboximg-mount's error, with the cause spelled out where it is known."""
    if "Error parsing volumes" in output:
        return (output + "\n\nvboximg-mount found no partition table it can read — "
                "typically a blank disk that has never been partitioned.")
    return output


@dataclass
class Mount:
    label: str
    medium: str
    mountpoint: str
    process: QProcess


class ImageMounts(QObject):
    """The images this window has mounted, and the work of (un)mounting them.

    The process is kept rather than started detached: whether vboximg-mount
    stays in the foreground or forks into the background, the mount is judged
    by the mountpoint itself, polled until it appears or the process gives up.
    """

    #: mounted(label, mountpoint) / failed(label, message) / note(text)
    mounted = pyqtSignal(str, str)
    failed = pyqtSignal(str, str)
    note = pyqtSignal(str)

    POLL_MS = 200
    TIMEOUT_MS = 20_000

    #: Overridable for tests, where there is no FUSE.
    is_mounted = staticmethod(os.path.ismount)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mounts: dict[str, Mount] = {}

    def mount(self, label: str, medium: str) -> str | None:
        tool = vboximg_mount_path()
        if not tool:
            self.failed.emit(label, "vboximg-mount was not found beside VBoxManage or "
                                    "on PATH. It ships with VirtualBox on Linux and macOS.")
            return None
        os.makedirs(mount_root(), mode=0o700, exist_ok=True)
        stem = os.path.splitext(os.path.basename(label))[0] or "image"
        mountpoint = tempfile.mkdtemp(prefix=f"{stem}-", dir=mount_root())
        args = mount_args(medium, mountpoint)
        self.note.emit("$ " + " ".join(shlex.quote(a) for a in [tool, *args]))
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        entry = Mount(label, medium, mountpoint, proc)
        self.mounts[mountpoint] = entry
        waited = 0

        def poll():
            nonlocal waited
            if self.is_mounted(mountpoint):
                self.mounted.emit(label, mountpoint)
                return
            exited = proc.state() == QProcess.ProcessState.NotRunning
            if exited and (proc.exitCode() != 0 or waited >= 1000) or waited >= self.TIMEOUT_MS:
                output = bytes(proc.readAll()).decode(errors="replace").strip()
                self._forget(mountpoint)
                if proc.state() != QProcess.ProcessState.NotRunning:
                    proc.kill()
                self.failed.emit(label, explain_failure(output)
                                 or "vboximg-mount did not mount the image")
                return
            waited += self.POLL_MS
            QTimer.singleShot(self.POLL_MS, poll)

        proc.start(tool, args)
        QTimer.singleShot(self.POLL_MS, poll)
        return mountpoint

    def _forget(self, mountpoint: str):
        self.mounts.pop(mountpoint, None)
        try:
            os.rmdir(mountpoint)
        except OSError:
            pass

    def unmount(self, mountpoint: str) -> str:
        """Unmount one image; "" on success, else why not."""
        entry = self.mounts.get(mountpoint)
        if entry is None:
            return ""
        if self.is_mounted(mountpoint):
            command = unmount_command(mountpoint)
            if not command:
                return "neither fusermount3 nor fusermount is installed"
            self.note.emit("$ " + " ".join(shlex.quote(a) for a in command))
            try:
                done = subprocess.run(command, capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as e:
                return str(e)
            if done.returncode != 0:
                return (done.stderr or done.stdout).strip() or f"exit {done.returncode}"
        proc = entry.process
        if proc.state() != QProcess.ProcessState.NotRunning and not proc.waitForFinished(3000):
            proc.kill()
            proc.waitForFinished(1000)
        self._forget(mountpoint)
        return ""

    def unmount_all(self) -> list[str]:
        return [f"{self.mounts[m].label}: {why}" for m in list(self.mounts)
                if (why := self.unmount(m))]
