"""Persistent trash index and restore/delete operations."""
from __future__ import annotations
from dataclasses import asdict
import json
import os

from .models import TrashedFile
from .persistence import get_settings, restore_trashed, add_library_path


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
