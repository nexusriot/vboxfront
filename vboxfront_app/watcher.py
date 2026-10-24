"""Notice VirtualBox.xml changing, so the listing can follow without F5."""
from __future__ import annotations

import os
import sys

from PyQt6.QtCore import QFileSystemWatcher, QObject, QTimer, pyqtSignal


def virtualbox_home() -> str:
    """Where VBoxSVC keeps VirtualBox.xml, following VirtualBox's own rules."""
    explicit = os.environ.get("VBOX_USER_HOME")
    if explicit:
        return explicit
    home = os.path.expanduser("~")
    legacy = os.path.join(home, ".VirtualBox")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "VirtualBox")
    if sys.platform.startswith("win"):
        return legacy
    xdg = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"),
                       "VirtualBox")
    # VirtualBox's own rule (GetVBoxUserHomeDirectory): an existing
    # ~/.VirtualBox from an older install wins, rather than being migrated.
    if os.path.isdir(legacy):
        return legacy
    return xdg


def registry_path() -> str:
    return os.path.join(virtualbox_home(), "VirtualBox.xml")


class RegistryWatcher(QObject):
    """Emits `changed` once a burst of writes to the registry file settles.

    VBoxSVC saves by writing a new file and renaming it over the old one, which
    drops a plain file watch after the first save, so the directory is watched
    as well and the file re-added whenever it reappears. A directory event
    only counts when the file itself changed: VirtualBox.xml-prev and the log
    files live in the same folder.
    """

    changed = pyqtSignal()

    def __init__(self, path: str, parent=None, delay_ms: int = 1500):
        super().__init__(parent)
        self.path = path
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._poke)
        self._watcher.directoryChanged.connect(self._poke)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self._settled)
        self._stamp = self._read_stamp()
        self._enabled = False

    def _read_stamp(self):
        try:
            info = os.stat(self.path)
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size, info.st_ino)

    def _watch(self):
        directory = os.path.dirname(self.path)
        if os.path.isdir(directory) and directory not in self._watcher.directories():
            self._watcher.addPath(directory)
        if os.path.exists(self.path) and self.path not in self._watcher.files():
            self._watcher.addPath(self.path)

    def set_enabled(self, on: bool):
        self._enabled = on
        if on:
            self._stamp = self._read_stamp()
            self._watch()
        else:
            self._timer.stop()
            paths = self._watcher.files() + self._watcher.directories()
            if paths:
                self._watcher.removePaths(paths)

    def is_enabled(self) -> bool:
        return self._enabled

    def _poke(self, *_args):
        if not self._enabled:
            return
        self._watch()
        self._timer.start()

    def _settled(self):
        stamp = self._read_stamp()
        if stamp is None or stamp == self._stamp:
            return
        self._stamp = stamp
        self.changed.emit()
