"""Prove two images hold the same data, whatever their container format.

A VDI and the VMDK cloned from it share no bytes on disk, so comparing files
says nothing. `mediumio cat` reads what the *guest* sees, through VirtualBox's
own backend stack, so hashing that stream on both sides compares contents.
A flat file (a RAW export) is hashed directly: its bytes are the guest's.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import shlex
import subprocess
import tempfile

from PyQt6.QtCore import QThread, pyqtSignal

from .commands import mediumio_cat_args

CHUNK = 1024 * 1024


@dataclass
class VerifySource:
    label: str
    #: A medium VirtualBox can read (UUID or path) — or, with is_file, a flat
    #: image read straight off the disk.
    medium: str
    kind: str = "disk"
    is_file: bool = False
    expected_bytes: int = 0

    def command(self) -> list[str]:
        args, _stdin = mediumio_cat_args(self.kind, self.medium, hex_dump=False)
        return args + ["--output=-"]

    def describe(self) -> str:
        if self.is_file:
            return f"read {self.medium}"
        return " ".join(shlex.quote(a) for a in ["VBoxManage", *self.command()])


@dataclass
class VerifyResult:
    label: str
    digest: str = ""
    size: int = 0
    error: str = ""


def verdict(results: list[VerifyResult]) -> tuple[bool, str]:
    """(identical, one-line explanation) for two finished hashes."""
    errors = [f"{r.label}: {r.error}" for r in results if r.error]
    if errors:
        return False, "Could not read both images — " + "; ".join(errors)
    left, right = results
    if left.size != right.size:
        return False, (f"Sizes differ: {left.label} has {left.size:,} bytes, "
                       f"{right.label} {right.size:,}.")
    if left.digest != right.digest:
        return False, f"Contents differ ({left.size:,} bytes each; SHA-256 does not match)."
    return True, f"Identical: {left.size:,} bytes, SHA-256 {left.digest}."


class VerifyTask(QThread):
    """Hash each source in turn on a worker thread; cancellable.

    Sequential, like the refresh chain: one VBoxSVC, and two long reads at
    once compete for the same disk anyway.
    """

    progress = pyqtSignal(int, int)  # source index, percent (-1 when unknown)
    done = pyqtSignal(list)  # list[VerifyResult]

    def __init__(self, vbm: str, sources: list[VerifySource], parent=None):
        super().__init__(parent)
        self.vbm = vbm
        self.sources = sources
        self._cancelled = False
        self._proc: subprocess.Popen | None = None

    def cancel(self):
        self._cancelled = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.kill()

    def run(self):
        results = []
        for index, source in enumerate(self.sources):
            if self._cancelled:
                results.append(VerifyResult(source.label, error="cancelled"))
                continue
            results.append(self._hash(index, source))
        self.done.emit(results)

    def _hash(self, index: int, source: VerifySource) -> VerifyResult:
        digest = hashlib.sha256()
        size = 0
        last = -2

        def tick():
            nonlocal last
            pct = min(100, size * 100 // source.expected_bytes) if source.expected_bytes else -1
            if pct != last:
                last = pct
                self.progress.emit(index, pct)

        try:
            if source.is_file:
                with open(source.medium, "rb") as handle:
                    while chunk := handle.read(CHUNK):
                        if self._cancelled:
                            return VerifyResult(source.label, error="cancelled")
                        digest.update(chunk)
                        size += len(chunk)
                        tick()
                return VerifyResult(source.label, digest.hexdigest(), size)
            # stderr to a file, not a pipe: a full pipe nobody reads while
            # stdout streams would stall VBoxManage for good.
            with tempfile.TemporaryFile() as errors, subprocess.Popen(
                    [self.vbm, *source.command()], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=errors) as proc:
                self._proc = proc
                if self._cancelled:
                    proc.kill()
                while chunk := proc.stdout.read(CHUNK):
                    digest.update(chunk)
                    size += len(chunk)
                    tick()
                code = proc.wait()
                errors.seek(0)
                stderr = errors.read().decode(errors="replace").strip()
        except OSError as e:
            return VerifyResult(source.label, error=str(e))
        finally:
            self._proc = None
        if self._cancelled:
            return VerifyResult(source.label, error="cancelled")
        if code != 0:
            return VerifyResult(source.label, error=stderr.splitlines()[-1] if stderr
                                else f"exit {code}")
        return VerifyResult(source.label, digest.hexdigest(), size)
