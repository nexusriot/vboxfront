"""Find a moved image file by the UUID stored inside it.

VirtualBox keeps a medium's UUID in the image itself, not only in the
registry. So when a file is moved behind VirtualBox's back, the new copy can
be recognised for certain, not guessed from its name. Reading that UUID needs
no VBoxManage call and no VBoxSVC: it is a few hundred bytes at a fixed place.

Where each format keeps it (checked against images created by VBoxManage
7.2.22):

- **VDI**: the header after the 64-byte banner. Signature 0xbeda107f at 0x40,
  version 1.x at 0x44, `uuidCreate` at 0x188 and the parent's UUID at 0x1a8.
  Both are RTUUIDs, stored in little-endian GUID byte order.
- **VHD**: the 512-byte footer at the end of the file (a dynamic image also
  keeps a copy at offset 0). `Unique Id` is at byte 68. VirtualBox copies
  those 16 bytes into an RTUUID without byte-swapping, so it prints them in
  little-endian GUID order, not in the big-endian order the VHD spec uses.
- **VMDK**: `ddb.uuid.image` in the text descriptor. That is either a small
  file of its own (monolithicFlat, twoGbMaxExtent) or embedded in a sparse
  extent whose header ("KDMV") gives its offset and length in sectors.

Formats that store no UUID (RAW, ISO, QCOW, QED, Parallels) get one from
VirtualBox when it opens the file. For those the search can only offer files
with the same name and a matching size. The results label that weaker match.

Domain-level: no Qt, so the CLI and tests can use it directly.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import re
import struct
import uuid as uuidlib

from .constants import MEDIA_EXTENSIONS, TRASH_DIR_NAME

VDI_SIGNATURE = 0xBEDA107F
VDI_UUID_OFFSET = 0x188
VDI_PARENT_OFFSET = 0x1A8
VHD_COOKIE = b"conectix"
VHD_UUID_OFFSET = 68
VMDK_SPARSE_MAGIC = b"KDMV"
VMDK_TEXT_MAGIC = b"# Disk DescriptorFile"
#: A descriptor of its own is a few hundred bytes; an embedded one is usually
#: 10 KiB. Anything claiming more than this is not a descriptor.
VMDK_DESCRIPTOR_LIMIT = 1024 * 1024
NULL_UUID = "00000000-0000-0000-0000-000000000000"

MATCH_UUID = "uuid"
MATCH_NAME = "name"

_DDB_RE = re.compile(r'^\s*ddb\.uuid\.(image|parent)\s*=\s*"([^"]*)"', re.M)
_EXTENT_RE = re.compile(r'^\s*(?:RW|RDONLY|NOACCESS)\s+\d+\s+\w+\s+"([^"]+)"', re.M)


@dataclass
class ImageIdentity:
    """What an image file says about itself."""

    fmt: str  # "VDI", "VHD" or "VMDK"
    uuid: str
    parent_uuid: str = ""
    #: VMDK only: extent files the descriptor names, as written there.
    extents: tuple[str, ...] = ()


def _uuid_le(raw: bytes) -> str:
    if len(raw) != 16:
        return ""
    text = str(uuidlib.UUID(bytes_le=raw))
    return "" if text == NULL_UUID else text


def _read_vdi(head: bytes) -> ImageIdentity | None:
    if len(head) < VDI_PARENT_OFFSET + 16:
        return None
    signature, version = struct.unpack_from("<II", head, 0x40)
    if signature != VDI_SIGNATURE or version >> 16 != 1:
        return None
    own = _uuid_le(head[VDI_UUID_OFFSET:VDI_UUID_OFFSET + 16])
    if not own:
        return None
    parent = _uuid_le(head[VDI_PARENT_OFFSET:VDI_PARENT_OFFSET + 16])
    return ImageIdentity("VDI", own, parent)


def _read_vhd(handle, head: bytes) -> ImageIdentity | None:
    footer = head[:512] if head.startswith(VHD_COOKIE) else b""
    if not footer:
        # A fixed VHD has its footer only at the end.
        try:
            handle.seek(-512, os.SEEK_END)
            footer = handle.read(512)
        except OSError:
            return None
        if not footer.startswith(VHD_COOKIE):
            return None
    own = _uuid_le(footer[VHD_UUID_OFFSET:VHD_UUID_OFFSET + 16])
    if not own:
        return None
    parent = ""
    # A dynamic or differencing image has a sparse header at offset 512,
    # with the parent's UUID at byte 40 of it.
    if head[512:520] == b"cxsparse":
        parent = _uuid_le(head[512 + 40:512 + 56])
    return ImageIdentity("VHD", own, parent)


def _vmdk_from_text(text: str) -> ImageIdentity | None:
    found = dict(_DDB_RE.findall(text))
    own = found.get("image", "").strip().lower()
    if not own or own == NULL_UUID:
        return None
    parent = found.get("parent", "").strip().lower()
    return ImageIdentity("VMDK", own, "" if parent == NULL_UUID else parent,
                         tuple(_EXTENT_RE.findall(text)))


def _read_vmdk(handle, head: bytes) -> ImageIdentity | None:
    if head.startswith(VMDK_TEXT_MAGIC):
        try:
            handle.seek(0)
            text = handle.read(VMDK_DESCRIPTOR_LIMIT)
        except OSError:
            return None
        return _vmdk_from_text(text.decode("utf-8", "replace"))
    if not head.startswith(VMDK_SPARSE_MAGIC) or len(head) < 44:
        return None
    offset, size = struct.unpack_from("<QQ", head, 28)
    if not offset or not size or size * 512 > VMDK_DESCRIPTOR_LIMIT:
        return None
    try:
        handle.seek(offset * 512)
        text = handle.read(size * 512)
    except OSError:
        return None
    return _vmdk_from_text(text.split(b"\0", 1)[0].decode("utf-8", "replace"))


def read_image_identity(path: str) -> ImageIdentity | None:
    """The UUID (and parent UUID) an image file carries, or None.

    The format is decided by content, not extension: a `.vdi` renamed to
    `.img` is still recognised, and a file that only looks like an image by
    its name is not trusted.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(1024)
            return (_read_vdi(head) or _read_vmdk(handle, head)
                    or _read_vhd(handle, head))
    except OSError:
        return None


def read_image_uuid(path: str) -> str:
    identity = read_image_identity(path)
    return identity.uuid if identity else ""


def missing_extents(path: str, identity: ImageIdentity | None = None) -> list[str]:
    """Extent files a VMDK descriptor names that are not beside it.

    A monolithicFlat or split VMDK is a descriptor plus data files, named
    relative to the descriptor. Moving the descriptor alone gives VirtualBox
    a medium it still cannot open.
    """
    identity = identity or read_image_identity(path)
    if not identity or not identity.extents:
        return []
    folder = os.path.dirname(path)
    missing = []
    for extent in identity.extents:
        full = extent if os.path.isabs(extent) else os.path.join(folder, extent)
        if not os.path.exists(full):
            missing.append(extent)
    return missing


@dataclass
class LocateCandidate:
    path: str
    match: str  # MATCH_UUID or MATCH_NAME
    note: str = ""


def default_search_folders(old_location: str, extra: list[str] = ()) -> list[str]:
    """Where a moved file most likely went, nearest first.

    The closest folder above the old location that still exists (files usually
    move a short way: a renamed VM folder, a sibling disk), then `extra` (the
    default image folder). The filesystem root is never offered: walking all
    of it is not a search anyone means to start by accident.
    """
    folders: list[str] = []
    probe = os.path.dirname(os.path.abspath(old_location)) if old_location else ""
    while probe:
        if os.path.isdir(probe):
            break
        parent = os.path.dirname(probe)
        probe = "" if parent == probe else parent
    if probe and os.path.dirname(probe) != probe:
        folders.append(probe)
    for folder in extra:
        if folder and os.path.isdir(folder) and not any(
            os.path.realpath(folder) == os.path.realpath(f) for f in folders
        ):
            folders.append(folder)
    return folders


def _size_matches(path: str, expected_mb: int | None) -> bool:
    if expected_mb is None:
        return False
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    # VBoxManage prints capacity in whole MBytes, rounded either way.
    return abs(size / (1024 * 1024) - expected_mb) <= 1


def find_moved_image(uuid: str, old_location: str, folders: list[str],
                     known: list[str] = (), expected_mb: int | None = None,
                     recursive: bool = True, cancelled=None,
                     progress=None) -> list[LocateCandidate]:
    """Files under `folders` that are the medium `uuid`, best match first.

    A file whose stored UUID equals `uuid` is a certain match. A file with no
    readable UUID, the old file name and a size matching `expected_mb` is a
    possible match (for ISOs and other formats that store none). A file
    storing a *different* UUID is never offered, whatever it is called.

    Files the registry or the library already uses (`known`) are skipped, as
    are trash folders and symlinked directories, the same rules as the orphan
    scan. `cancelled()` is polled between files; `progress(n)` gets the number
    of image files examined so far.
    """
    want = (uuid or "").strip().strip("{}").lower()
    old_name = os.path.basename(old_location or "").lower()
    skip = {os.path.realpath(p) for p in known if p}
    seen: set[str] = set()
    found: list[LocateCandidate] = []
    examined = 0
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        for root, dirs, files in os.walk(folder):
            if cancelled and cancelled():
                return found
            dirs[:] = [] if not recursive else [d for d in dirs if d != TRASH_DIR_NAME]
            for name in sorted(files):
                if os.path.splitext(name)[1].lower() not in MEDIA_EXTENSIONS:
                    continue
                full = os.path.join(root, name)
                real = os.path.realpath(full)
                if real in skip or real in seen:
                    continue
                seen.add(real)
                examined += 1
                if progress and examined % 50 == 0:
                    progress(examined)
                if cancelled and cancelled():
                    return found
                identity = read_image_identity(full)
                if identity:
                    if identity.uuid != want:
                        continue
                    lost = missing_extents(full, identity)
                    note = ("missing beside it: " + ", ".join(lost)) if lost else ""
                    found.append(LocateCandidate(full, MATCH_UUID, note))
                elif name.lower() == old_name and _size_matches(full, expected_mb):
                    found.append(LocateCandidate(
                        full, MATCH_NAME,
                        "same name and size; this format stores no UUID to confirm it",
                    ))
    if progress:
        progress(examined)
    found.sort(key=lambda c: (c.match != MATCH_UUID, bool(c.note), c.path))
    return found


def parent_first(paths: list[str]) -> list[str]:
    """`paths` reordered so a differencing image comes after its parent.

    VirtualBox will not open a child before its parent is open, so the
    library has to be re-opened in chain order. Paths whose header cannot be
    read keep their place relative to each other; the sort is stable.
    """
    uuids: dict[str, str] = {}
    parents: dict[str, str] = {}
    for path in paths:
        identity = read_image_identity(path)
        if identity:
            uuids[path] = identity.uuid
            parents[path] = identity.parent_uuid
    by_uuid = {uuid: path for path, uuid in uuids.items()}
    ordered: list[str] = []
    placed: set[str] = set()

    def place(path: str, trail: set[str]):
        if path in placed or path in trail:
            return
        parent = by_uuid.get(parents.get(path, ""))
        if parent and parent != path:
            place(parent, trail | {path})
        placed.add(path)
        ordered.append(path)

    for path in paths:
        place(path, set())
    return ordered
