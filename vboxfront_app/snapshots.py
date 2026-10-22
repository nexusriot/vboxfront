"""Registry snapshot validation and comparisons by medium kind and UUID."""
from __future__ import annotations
from dataclasses import dataclass
import json

from .constants import KINDS
from .domain import export_rows


@dataclass
class RegistryChange:
    kind: str
    uuid: str
    change: str
    details: str


def load_snapshot(path):
    with open(path, encoding='utf-8') as handle:
        payload = json.load(handle)
    media = payload.get('media') if isinstance(payload, dict) else None
    if not isinstance(media, dict):
        raise ValueError('Snapshot must contain a media object')
    for kind, rows in media.items():
        if kind not in KINDS or not isinstance(rows, list):
            raise ValueError('Invalid snapshot media kind or rows')
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('uuid'), str) or not row['uuid']:
                raise ValueError('Each snapshot row needs a UUID')
            if row['uuid'] in seen:
                raise ValueError('Duplicate snapshot UUID')
            seen.add(row['uuid'])
    return media


def compare_registry(previous, current):
    """Both inputs contain exported row dictionaries, never guessed live probes."""
    changes = []
    for kind in KINDS:
        before = {r['uuid']: r for r in previous.get(kind, [])}
        after = {r['uuid']: r for r in current.get(kind, [])}
        for ident in sorted(before.keys() | after.keys()):
            if ident not in before:
                changes.append(RegistryChange(kind, ident, 'added', str(after[ident].get('location', ''))))
            elif ident not in after:
                changes.append(RegistryChange(kind, ident, 'missing', str(before[ident].get('location', ''))))
            else:
                a, b = before[ident], after[ident]
                fields = sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))
                if fields:
                    label = 'moved' if 'location' in fields else 'changed'
                    details = '; '.join(f'{k}: {a.get(k)} → {b.get(k)}' for k in fields)
                    changes.append(RegistryChange(kind, ident, label, details))
    return changes


def current_rows(media):
    return {kind: export_rows(rows) for kind, rows in media.items()}
