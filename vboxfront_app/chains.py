"""Trace and repair a differencing chain from what the image files say.

The registry's view of a chain (`Parent UUID:` in the listing) is only as
good as the registry. The files themselves record their own UUID and their
parent's (see `locate.py`), so a chain can be followed on disk even when the
registry has lost track: a parent that moved, a child found by the orphan
scan, a parent that was never registered here.

Three repairs come out of a trace, cheapest first:

- **re-point** a registered member whose file moved
  (`modifymedium disk <uuid> --setlocation`);
- **register** members found on disk that the registry does not know, by
  adding them to the media library *parent first*. VirtualBox refuses to
  open a differencing image before its parent ("Parent medium with UUID …
  is not found in the media registry", 7.2.22);
- **relink** an unregistered child to a parent file whose UUID differs
  (`internalcommands sethdparentuuid`). Only for a parent the user picks,
  and only when they confirm it holds exactly the data the child was
  created on. A wrong relink silently corrupts what the guest reads.

Qt-free, so the CLI can trace chains too.
"""
from __future__ import annotations

from dataclasses import dataclass
import os

from .domain import is_inaccessible, real_parent_uuid
from .locate import (
    MATCH_UUID,
    find_moved_image,
    missing_extents,
    read_image_identity,
)
from .models import MediumRecord

STATUS_OK = "ok"
STATUS_REPOINT = "repoint"
STATUS_REGISTER = "register"
STATUS_MISSING = "missing"

STATUS_TEXT = {
    STATUS_OK: "registered, file in place",
    STATUS_REPOINT: "registered, file moved: re-point",
    STATUS_REGISTER: "not registered: add to library",
    STATUS_MISSING: "not found",
}


@dataclass
class ChainLink:
    uuid: str
    path: str = ""
    status: str = STATUS_MISSING
    parent_uuid: str = ""
    record: MediumRecord | None = None
    note: str = ""

    @property
    def name(self) -> str:
        return os.path.basename(self.path or (self.record.location if self.record else "")) \
            or self.uuid


def _norm(uuid: str) -> str:
    return (uuid or "").strip().strip("{}").lower()


def trace_chain(uuid: str = "", path: str = "",
                disks: list[MediumRecord] = (), folders: list[str] = (),
                known: list[str] = (), cancelled=None,
                max_links: int = 256) -> list[ChainLink]:
    """The chain ending at `uuid` (or at the image file `path`), base first.

    Each link is located by UUID: the registry's file if it is there and
    carries that UUID, else a file found under `folders`. The parent comes
    from the file's header when it can be read, from the registry otherwise.
    The trace stops at a base image, at a link that cannot be found (its
    status is "missing"), or at a cycle.
    """
    by_uuid = {_norm(r.uuid): r for r in disks}
    links: list[ChainLink] = []
    seen: set[str] = set()
    want = _norm(uuid)
    hint = path
    if not want and path:
        identity = read_image_identity(path)
        if identity is None:
            return [ChainLink("", path, STATUS_MISSING,
                              note="no VDI, VHD or VMDK header to read a UUID from")]
        want = identity.uuid
    # Known paths are skipped by the search; a registered file that carries
    # the wrong UUID is not where a link lives either way.
    looped = False
    while want and len(links) < max_links:
        if want in seen:
            looped = True
            break
        if cancelled and cancelled():
            break
        seen.add(want)
        rec = by_uuid.get(want)
        link = ChainLink(want, record=rec)
        candidates = [p for p in (rec.location if rec else "", hint) if p]
        for candidate in candidates:
            identity = read_image_identity(candidate) if os.path.isfile(candidate) else None
            if identity and identity.uuid == want:
                link.path = candidate
                break
            if identity is None and os.path.isfile(candidate) and rec is not None \
                    and candidate == rec.location:
                # A format with no UUID in its header (RAW): trust the registry.
                link.path = candidate
                break
        if not link.path:
            found = [c for c in find_moved_image(
                want, rec.location if rec else hint, list(folders), list(known),
                cancelled=cancelled) if c.match == MATCH_UUID]
            if found:
                link.path = found[0].path
                if len(found) > 1:
                    link.note = f"{len(found)} files carry this UUID; using the first"
        if link.path:
            if rec is None:
                link.status = STATUS_REGISTER
            elif rec.location and os.path.exists(rec.location) and \
                    os.path.realpath(rec.location) == os.path.realpath(link.path):
                link.status = STATUS_OK
            else:
                link.status = STATUS_REPOINT
            identity = read_image_identity(link.path)
            lost = missing_extents(link.path, identity)
            if lost:
                link.note = "data file(s) missing beside it: " + ", ".join(lost)
            link.parent_uuid = identity.parent_uuid if identity else (
                _norm(real_parent_uuid(rec)) if rec else "")
        else:
            link.status = STATUS_MISSING
            link.parent_uuid = _norm(real_parent_uuid(rec)) if rec else ""
            if rec is not None and not link.note:
                link.note = ("registry says " + rec.location) if rec.location else ""
        links.append(link)
        if link.status == STATUS_MISSING and rec is None:
            break
        want, hint = link.parent_uuid, ""
    if looped:
        links[-1].note = (links[-1].note + "; " if links[-1].note else "") + \
            "its parent UUID loops back into the chain"
    links.reverse()
    return links


def chain_is_repairable(links: list[ChainLink]) -> bool:
    return bool(links) and all(link.status != STATUS_MISSING for link in links)


def chain_fixes(links: list[ChainLink]) -> tuple[list[list[str]], list[str]]:
    """(re-point commands, paths to add to the library base first)."""
    commands: list[list[str]] = []
    register: list[str] = []
    for link in links:
        if link.status == STATUS_REPOINT and link.record is not None:
            commands.append(["modifymedium", "disk", link.record.uuid,
                             "--setlocation", link.path])
        elif link.status == STATUS_REGISTER:
            register.append(link.path)
    return commands, register


def relink_args(child_path: str, parent_uuid: str) -> list[str]:
    return ["internalcommands", "sethdparentuuid", child_path, parent_uuid]


def relink_blocker(child: ChainLink, parent_path: str) -> str:
    """Why `child` must not be relinked onto `parent_path`, or ""."""
    if child.record is not None:
        return ("this image is registered; rewriting its parent UUID would leave "
                "the registry describing a different chain. Unregister it first.")
    if not child.path or not os.path.isfile(child.path):
        return "the child image file is not there"
    identity = read_image_identity(parent_path)
    if identity is None:
        return f"{os.path.basename(parent_path)} has no UUID header to link to"
    if identity.uuid == child.uuid:
        return "an image cannot be its own parent"
    if identity.uuid == child.parent_uuid:
        return "it is already this image's parent; re-pointing is enough"
    return ""


def registry_problems(links: list[ChainLink]) -> list[str]:
    """Plain-words summary of what is wrong, for a report or the CLI."""
    out = []
    for link in links:
        if link.status == STATUS_MISSING:
            out.append(f"{link.name} ({link.uuid}) was not found"
                       + (f": {link.note}" if link.note else ""))
        elif link.status == STATUS_REPOINT:
            out.append(f"{link.name} moved to {link.path}")
        elif link.status == STATUS_REGISTER:
            out.append(f"{link.name} at {link.path} is not registered")
        elif link.record is not None and is_inaccessible(link.record):
            out.append(f"{link.name} is inaccessible in the registry")
    return out
