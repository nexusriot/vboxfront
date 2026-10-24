"""Qt-free summaries of a parsed registry: where the space goes, and chains.

Everything here works from the listing already parsed, so a report costs no
VBoxManage command — the same rule the details pane and health checks follow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os

from .domain import (
    format_mbytes,
    is_differencing,
    is_inaccessible,
    parse_capacity_mb,
    parse_in_use_entry,
    real_parent_uuid,
)
from .models import MediumRecord

NOT_ATTACHED = "(not attached)"


@dataclass
class SpaceRow:
    label: str
    mb: int
    count: int = 1
    note: str = ""
    #: Media this row stands for, so a dialog can act on them.
    uuids: list[str] = field(default_factory=list)


@dataclass
class SpaceSection:
    title: str
    rows: list[SpaceRow]
    note: str = ""

    @property
    def total_mb(self) -> int:
        return sum(r.mb for r in self.rows)


def size_mb(rec: MediumRecord) -> int:
    return parse_capacity_mb(rec.size_on_disk) or 0


def chain_of(records: list[MediumRecord], uuid: str) -> list[MediumRecord]:
    """The chain from its base image down to `uuid`, base first.

    Stops at a parent the listing does not contain (or a cycle, which a
    corrupt registry can produce), so the first element is not necessarily a
    base image — `chain_is_complete` says whether it is.
    """
    by_uuid = {r.uuid: r for r in records}
    chain: list[MediumRecord] = []
    seen: set[str] = set()
    current = by_uuid.get(uuid)
    while current is not None and current.uuid not in seen:
        chain.append(current)
        seen.add(current.uuid)
        current = by_uuid.get(real_parent_uuid(current))
    chain.reverse()
    return chain


def chain_is_complete(chain: list[MediumRecord]) -> bool:
    return bool(chain) and not real_parent_uuid(chain[0])


def chain_problems(chain: list[MediumRecord]) -> list[str]:
    """Why reading through this chain would fail, in plain words."""
    problems = []
    if chain and not chain_is_complete(chain):
        problems.append(
            f"the parent of {os.path.basename(chain[0].location) or chain[0].uuid} "
            "is not registered"
        )
    for rec in chain:
        name = os.path.basename(rec.location) or rec.uuid
        if is_inaccessible(rec):
            problems.append(f"{name} is inaccessible")
        elif rec.location and not os.path.exists(rec.location):
            # The registry can still say "created" for a file deleted since
            # VBoxSVC last opened it — the health check's "missing" case.
            problems.append(f"the file of {name} is missing")
    return problems


def chain_root(records_by_uuid: dict[str, MediumRecord], rec: MediumRecord) -> MediumRecord:
    seen = {rec.uuid}
    while True:
        parent = records_by_uuid.get(real_parent_uuid(rec))
        if parent is None or parent.uuid in seen:
            return rec
        seen.add(parent.uuid)
        rec = parent


def compact_candidates(disks: list[MediumRecord]) -> list[MediumRecord]:
    """Dynamic, readable VDIs, largest first — the only images compact can shrink."""
    found = [
        r for r in disks
        if r.fmt.upper() == "VDI" and "fixed" not in r.variant.lower()
        and not is_inaccessible(r) and size_mb(r) > 0
    ]
    return sorted(found, key=size_mb, reverse=True)


def space_report(media: dict[str, list[MediumRecord]], trash_bytes: int = 0) -> list[SpaceSection]:
    """Where the space goes, grouped the ways that answer "what do I delete?"."""
    disks = media.get("disk", [])
    everything = [r for records in media.values() for r in records]
    sections: list[SpaceSection] = []

    kinds = []
    for kind, title in (("disk", "Hard disks"), ("dvd", "Optical images"),
                        ("floppy", "Floppy images")):
        records = media.get(kind, [])
        if records:
            kinds.append(SpaceRow(title, sum(size_mb(r) for r in records), len(records)))
    if trash_bytes:
        kinds.append(SpaceRow("Trash (indexed deletes)", trash_bytes // (1024 * 1024), 0,
                              "freed only when the trash is purged"))
    sections.append(SpaceSection("Totals", kinds))

    by_vm: dict[str, SpaceRow] = {}
    for rec in disks:
        names = [p[0] for p in (parse_in_use_entry(e) for e in rec.in_use) if p] or [NOT_ATTACHED]
        for name in dict.fromkeys(names):
            row = by_vm.setdefault(name, SpaceRow(name, 0, 0))
            row.mb += size_mb(rec)
            row.count += 1
            row.uuids.append(rec.uuid)
            if len(names) > 1:
                row.note = "shares disks with other VMs"
    sections.append(SpaceSection(
        "By VM", sorted(by_vm.values(), key=lambda r: r.mb, reverse=True),
        "A disk attached to several VMs is counted under each of them."))

    by_folder: dict[str, SpaceRow] = {}
    for rec in everything:
        folder = os.path.dirname(rec.location) or "(unknown)"
        row = by_folder.setdefault(folder, SpaceRow(folder, 0, 0))
        row.mb += size_mb(rec)
        row.count += 1
        row.uuids.append(rec.uuid)
    sections.append(SpaceSection(
        "By folder", sorted(by_folder.values(), key=lambda r: r.mb, reverse=True)))

    by_uuid = {r.uuid: r for r in disks}
    chains: dict[str, SpaceRow] = {}
    for rec in disks:
        root = chain_root(by_uuid, rec)
        row = chains.setdefault(root.uuid, SpaceRow(
            os.path.basename(root.location) or root.uuid, 0, 0, uuids=[]))
        row.mb += size_mb(rec)
        row.count += 1
        row.uuids.append(rec.uuid)
    chain_rows = [r for r in chains.values() if r.count > 1]
    for row in chain_rows:
        row.note = f"base + {row.count - 1} differencing image(s)"
    sections.append(SpaceSection(
        "Differencing chains", sorted(chain_rows, key=lambda r: r.mb, reverse=True),
        "Flatten a chain to fold it back into one image."))

    candidates = [
        SpaceRow(os.path.basename(r.location) or r.uuid, size_mb(r), 1,
                 "differencing" if is_differencing(r) else "", [r.uuid])
        for r in compact_candidates(disks)
    ]
    sections.append(SpaceSection(
        "Compact candidates", candidates,
        "Dynamic VDIs, largest first. What compact gives back depends on the "
        "guest having freed (TRIMmed or zeroed) the blocks — unknowable until "
        "it runs."))
    return sections


def space_report_text(sections: list[SpaceSection]) -> str:
    lines = []
    for section in sections:
        lines.append(f"{section.title} — {format_mbytes(section.total_mb)}")
        if section.note:
            lines.append(f"  ({section.note})")
        for row in section.rows:
            count = f"  [{row.count}]" if row.count else ""
            note = f"  — {row.note}" if row.note else ""
            lines.append(f"  {format_mbytes(row.mb):>10}  {row.label}{count}{note}")
        if not section.rows:
            lines.append("  (none)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def space_report_json(sections: list[SpaceSection]) -> list[dict]:
    return [
        {"title": s.title, "total_mb": s.total_mb, "note": s.note,
         "rows": [{"label": r.label, "mb": r.mb, "count": r.count, "note": r.note,
                   "uuids": r.uuids} for r in s.rows]}
        for s in sections
    ]
