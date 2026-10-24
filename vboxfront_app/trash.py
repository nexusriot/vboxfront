"""Persistent trash index and restore/delete operations."""
from __future__ import annotations
from dataclasses import asdict
import json
import os
import time

from .models import TrashedFile
from .persistence import get_settings, restore_trashed, add_library_path


def trashed_when(entry: TrashedFile) -> float:
    """When an entry went into the trash, best known."""
    if entry.trashed_at:
        return entry.trashed_at
    try:
        return os.lstat(entry.trashed).st_ctime
    except OSError:
        return 0.0


def allocated_bytes(path: str) -> int | None:
    """Bytes a file really occupies, or None when it is gone."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return getattr(info, 'st_blocks', (info.st_size + 511) // 512) * 512


class TrashStore:
    """QSettings index; files remain on their original filesystem."""
    KEY = 'trash/batches'

    def load(self) -> list[list[TrashedFile]]:
        try:
            rows = json.loads(get_settings().value(self.KEY, '[]', str))
            return [[TrashedFile(**e) for e in batch] for batch in rows]
        except (ValueError, TypeError, KeyError):
            return []

    def save(self, batches: list[list[TrashedFile]]):
        settings = get_settings()
        settings.setValue(self.KEY, json.dumps([[asdict(e) for e in b] for b in batches if b]))
        settings.sync()

    def restore(self, entry: TrashedFile):
        restore_trashed(entry)
        add_library_path(entry.kind, entry.original)
        self.discard(entry)

    def delete(self, entry: TrashedFile):
        # Never recurse through a user-configured trash directory.
        if os.path.lexists(entry.trashed):
            if not os.path.isfile(entry.trashed) and not os.path.islink(entry.trashed):
                raise IsADirectoryError(entry.trashed)
            os.unlink(entry.trashed)
        self.discard(entry)

    def discard(self, entry: TrashedFile):
        batches = [[e for e in b if e != entry] for b in self.load()]
        self.save(batches)

    def purge_candidates(self, max_age_days: int, max_bytes: int,
                         now: float | None = None) -> list[TrashedFile]:
        """Entries the purge rule would delete, oldest first.

        The newest batch is never a candidate: it is what Undo delete puts
        back, and a cap smaller than one large disk would otherwise erase a
        delete the moment it happened. Files already gone are left to the
        Trash manager — there is nothing to free.
        """
        if not max_age_days and not max_bytes:
            return []
        now = time.time() if now is None else now
        batches = [b for b in self.load() if b]
        entries = [(trashed_when(e), allocated_bytes(e.trashed), e)
                   for b in batches[:-1] for e in b]
        entries = [(when, size, e) for when, size, e in entries if size is not None]
        entries.sort(key=lambda t: t[0])
        doomed: list[TrashedFile] = []
        if max_age_days:
            cutoff = now - max_age_days * 86400
            doomed = [e for when, _size, e in entries if when < cutoff]
        if max_bytes:
            newest = batches[-1] if batches else []
            total = sum(allocated_bytes(e.trashed) or 0 for e in newest)
            total += sum(size for _when, size, e in entries if e not in doomed)
            for _when, size, e in entries:
                if total <= max_bytes:
                    break
                if e not in doomed:
                    doomed.append(e)
                    total -= size
        return doomed

    def purge(self, max_age_days: int, max_bytes: int,
              now: float | None = None) -> tuple[list[TrashedFile], list[str]]:
        """Delete what the rule selects; (deleted, error lines)."""
        deleted, errors = [], []
        for entry in self.purge_candidates(max_age_days, max_bytes, now):
            try:
                self.delete(entry)
                deleted.append(entry)
            except OSError as e:
                errors.append(f'{entry.trashed}: {e}')
        return deleted, errors
