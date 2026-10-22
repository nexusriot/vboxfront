"""Cancellable archive measurement on a worker thread with a stat-keyed cache."""
from __future__ import annotations
import os
from PyQt6.QtCore import QThread, pyqtSignal
from .compression import measure_uncompressed


def source_stamp(path):
    try:
        s = os.stat(path)
        return (os.path.realpath(path), s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    except OSError:
        return None


class MeasurementCache:
    def __init__(self):
        self.values = {}

    def get(self, path):
        return self.values.get(source_stamp(path))

    def put(self, stamp, value):
        if stamp is not None and value is not None:
            self.values[stamp] = value


cache = MeasurementCache()
_workers = set()  # Keep cancelled workers alive until QThread.finished.


class ArchiveMeasureTask(QThread):
    progress = pyqtSignal(object)
    measured = pyqtSignal(object)

    def __init__(self, path):
        super().__init__()
        self.path = path
        self.stamp = source_stamp(path)

    def start(self, *args):
        _workers.add(self)
        self.finished.connect(self._release)
        super().start(*args)

    def _release(self):
        _workers.discard(self)
        self.deleteLater()

    def run(self):
        def report(total):
            if self.isInterruptionRequested():
                return False
            # Do not enqueue one GUI event per tiny chunk indefinitely.
            if total // (32 << 20) != getattr(self, '_last_report', -1):
                self._last_report = total // (32 << 20)
                self.progress.emit(total)
            return True
        size = measure_uncompressed(self.path, report)
        if self.isInterruptionRequested() or source_stamp(self.path) != self.stamp:
            size = None
        self.measured.emit(size)
