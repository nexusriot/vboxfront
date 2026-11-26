"""Backup bundles: a medium and its whole chain, copied with a manifest.

A bundle is a folder `<name>.vboxbundle/` holding `manifest.json` and one
subfolder per medium under `media/`. Files are copied byte for byte, so
every image keeps its UUID and its parent link: restoring re-registers the
exact chain, which `clonemedium` cannot do (a clone gets a new UUID, and a
differencing image's clone is no longer a child of anything).

What goes in:

- the medium and every ancestor down to its base image, because a
  differencing image is unreadable without them;
- every file a VMDK descriptor names (monolithicFlat, twoGbMaxExtent), each
  medium in its own subfolder so the descriptor's relative names still work.
  This is the multi-file case the overwrite backup does not cover.

Each file's SHA-256 is recorded while it is copied, and a restore checks it
before anything is registered. Runs of zero bytes are written as holes, so a
sparse source does not balloon in the bundle. The bundle is written under a
`.partial` name and renamed only when complete; a cancelled or failed backup
leaves nothing that looks like a bundle.

Copying an image that a running VM is writing gives an inconsistent copy, so
callers must check that first (`running_users`). Qt-free.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import shutil
import time

from .domain import parse_in_use_entry, real_parent_uuid
from .locate import read_image_identity
from .models import MediumRecord
from .reports import chain_of, chain_problems

FORMAT = "vboxfront-bundle"
VERSION = 1
SUFFIX = ".vboxbundle"
MANIFEST = "manifest.json"
CHUNK = 1024 * 1024
_ZERO = bytes(CHUNK)


class BundleError(Exception):
    pass


class Cancelled(BundleError):
    pass


@dataclass
class BundleMember:
    kind: str
    record: MediumRecord
    files: list[str] = field(default_factory=list)  # absolute source paths

    @property
    def name(self) -> str:
        return os.path.basename(self.record.location) or self.record.uuid


def member_files(rec: MediumRecord) -> list[str]:
    """The backing file plus any extent files its VMDK descriptor names."""
    files = [rec.location]
    identity = read_image_identity(rec.location)
    folder = os.path.dirname(rec.location)
    for extent in (identity.extents if identity else ()):
        full = extent if os.path.isabs(extent) else os.path.join(folder, extent)
        if os.path.realpath(full) != os.path.realpath(rec.location) and full not in files:
            files.append(full)
    return files


def plan_members(media: dict[str, list[MediumRecord]], kind: str,
                 uuid: str) -> list[BundleMember]:
    """The members of a bundle for `uuid`, base first; BundleError if unusable."""
    records = media.get(kind, [])
    chain = chain_of(records, uuid) if kind == "disk" else [
        r for r in records if r.uuid == uuid]
    if not chain:
        raise BundleError(f"{uuid} is not a registered medium")
    problems = chain_problems(chain)
    if problems:
        raise BundleError("; ".join(problems))
    members = []
    for rec in chain:
        files = member_files(rec)
        absent = [f for f in files if not os.path.isfile(f)]
        if absent:
            raise BundleError(f"{os.path.basename(rec.location)}: missing "
                              + ", ".join(absent))
        members.append(BundleMember(kind, rec, files))
    return members


def running_users(members: list[BundleMember], running_vm_uuids) -> list[str]:
    """'disk.vdi (vm)' for every member attached to a VM that is running."""
    running = {u.lower() for u in running_vm_uuids}
    out = []
    for member in members:
        for entry in member.record.in_use:
            parsed = parse_in_use_entry(entry)
            if parsed and parsed[1].lower() in running:
                out.append(f"{member.name} ({parsed[0]})")
    return out


def bundle_size(members: list[BundleMember]) -> int:
    total = 0
    for member in members:
        for path in member.files:
            try:
                total += os.path.getsize(path)
            except OSError:
                pass
    return total


def default_bundle_name(members: list[BundleMember]) -> str:
    stem = os.path.splitext(members[-1].name)[0] if members else "media"
    stem = re.sub(r"[^\w.-]+", "_", stem).strip("._") or "media"
    return f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}{SUFFIX}"


def copy_file(src: str, dst: str, progress=None, cancelled=None) -> str:
    """Copy `src` to a new `dst` keeping holes; returns the SHA-256 hex digest.

    `progress(n)` gets the bytes just read; `cancelled()` is polled per chunk.
    The digest covers every byte, holes included, so it does not depend on
    how sparse either copy happens to be.
    """
    digest = hashlib.sha256()
    with open(src, "rb") as reader, open(dst, "xb") as writer:
        size = 0
        while True:
            if cancelled and cancelled():
                raise Cancelled("cancelled")
            block = reader.read(CHUNK)
            if not block:
                break
            digest.update(block)
            size += len(block)
            if block == _ZERO[:len(block)]:
                writer.seek(len(block), os.SEEK_CUR)
            else:
                writer.write(block)
            if progress:
                progress(len(block))
        writer.truncate(size)
    return digest.hexdigest()


def hash_file(path: str, progress=None, cancelled=None) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as reader:
        while True:
            if cancelled and cancelled():
                raise Cancelled("cancelled")
            block = reader.read(CHUNK)
            if not block:
                break
            digest.update(block)
            if progress:
                progress(len(block))
    return digest.hexdigest()


def write_bundle(members: list[BundleMember], dest_dir: str, name: str = "",
                 progress=None, cancelled=None) -> str:
    """Copy `members` into a new bundle under `dest_dir`; returns its path."""
    if not members:
        raise BundleError("nothing to back up")
    name = name or default_bundle_name(members)
    if not name.endswith(SUFFIX):
        name += SUFFIX
    final = os.path.join(dest_dir, name)
    if os.path.lexists(final):
        raise BundleError(f"{final} already exists")
    partial = final + ".partial"
    if os.path.lexists(partial):
        raise BundleError(f"{partial} is left over from an earlier attempt; remove it first")
    os.makedirs(partial)
    entries = []
    try:
        for index, member in enumerate(members):
            rec = member.record
            sub = f"{index:02d}-{rec.uuid[:8]}"
            os.makedirs(os.path.join(partial, "media", sub))
            files = []
            for src in member.files:
                rel = os.path.join("media", sub, os.path.basename(src))
                digest = copy_file(src, os.path.join(partial, rel), progress, cancelled)
                files.append({"path": rel.replace(os.sep, "/"),
                              "size": os.path.getsize(src), "sha256": digest})
            entries.append({
                "uuid": rec.uuid, "parent_uuid": real_parent_uuid(rec),
                "kind": member.kind, "format": rec.fmt, "name": member.name,
                "original_location": rec.location, "capacity": rec.capacity,
                "type": rec.medium_type, "description": rec.description,
                "files": files,
            })
        manifest = {"format": FORMAT, "version": VERSION,
                    "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "media": entries}
        with open(os.path.join(partial, MANIFEST), "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=1)
        os.replace(partial, final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return final


def read_manifest(bundle: str) -> dict:
    path = os.path.join(bundle, MANIFEST)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as e:
        raise BundleError(f"no readable {MANIFEST} in {bundle}: {e.strerror or e}") from e
    except ValueError as e:
        raise BundleError(f"{path} is not JSON: {e}") from e
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise BundleError(f"{bundle} is not a VBoxFront bundle")
    if not isinstance(data.get("version"), int) or data["version"] > VERSION:
        raise BundleError(f"{bundle} was written by a newer VBoxFront")
    media = data.get("media")
    if not isinstance(media, list) or not media:
        raise BundleError(f"{bundle} lists no media")
    for entry in media:
        for f in entry.get("files", []):
            rel = str(f.get("path", ""))
            # Never follow a manifest out of its own folder.
            if not rel or os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/"):
                raise BundleError(f"{bundle}: unsafe file path {rel!r} in manifest")
    return data


def manifest_size(data: dict) -> int:
    return sum(int(f.get("size", 0)) for e in data["media"] for f in e.get("files", []))


def verify_bundle(bundle: str, progress=None, cancelled=None) -> list[str]:
    """Problems found by re-hashing every file; [] means intact."""
    data = read_manifest(bundle)
    problems = []
    for entry in data["media"]:
        for f in entry.get("files", []):
            path = os.path.join(bundle, *str(f["path"]).split("/"))
            if not os.path.isfile(path):
                problems.append(f"{f['path']} is missing")
                continue
            if os.path.getsize(path) != int(f.get("size", -1)):
                problems.append(f"{f['path']} has the wrong size")
                continue
            if hash_file(path, progress, cancelled) != f.get("sha256"):
                problems.append(f"{f['path']} does not match its checksum")
    return problems


@dataclass
class RestoreStep:
    uuid: str
    kind: str
    name: str
    target: str = ""        # the backing file once restored
    files: list[tuple[str, str]] = field(default_factory=list)  # (src, dst)
    skip: str = ""          # why nothing is done for this member


def plan_restore(bundle: str, into: str,
                 registered: dict[str, str]) -> tuple[list[RestoreStep], list[str]]:
    """What a restore into `into` would do, base first, and what blocks it.

    `registered` maps registered UUIDs (lower case) to their locations. A
    member already registered whose file is in place is skipped, so a child
    can be restored on top of a base that never went away. Restoring a UUID
    that is registered elsewhere would collide, and any existing target file
    would be overwritten; both block the restore.
    """
    data = read_manifest(bundle)
    steps: list[RestoreStep] = []
    blockers: list[str] = []
    for entry in data["media"]:
        uuid = str(entry["uuid"]).lower()
        step = RestoreStep(uuid, entry.get("kind", "disk"), entry.get("name", uuid))
        location = registered.get(uuid)
        if location is not None:
            if location and os.path.exists(location):
                step.skip = f"already registered at {location}"
                step.target = location
                steps.append(step)
                continue
            blockers.append(f"{step.name} is registered (at {location or 'no path'}) but its "
                            "file is gone; use Resolve… or unregister it first")
        files = entry.get("files", [])
        for index, f in enumerate(files):
            src = os.path.join(bundle, *str(f["path"]).split("/"))
            dst = os.path.join(into, os.path.basename(str(f["path"])))
            if os.path.lexists(dst):
                blockers.append(f"{dst} already exists")
            step.files.append((src, dst))
            if index == 0:
                step.target = dst
        steps.append(step)
    targets = [dst for s in steps for _src, dst in s.files]
    if len(set(targets)) != len(targets):
        blockers.append("two files in the bundle share a name; restore them into "
                        "separate folders by hand")
    return steps, blockers


def restore_files(bundle: str, steps: list[RestoreStep], progress=None,
                  cancelled=None) -> None:
    """Verify, then copy each step's files; removes what it wrote on failure."""
    data = read_manifest(bundle)
    sums = {os.path.join(bundle, *str(f["path"]).split("/")): f.get("sha256")
            for e in data["media"] for f in e.get("files", [])}
    written: list[str] = []
    try:
        for step in steps:
            if step.skip:
                continue
            for src, dst in step.files:
                os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                # Only a file this restore created may be removed on failure;
                # copy_file opens with "x", so an existing one is never ours.
                try:
                    digest = copy_file(src, dst, progress, cancelled)
                except FileExistsError:
                    raise BundleError(f"{dst} already exists") from None
                except BaseException:
                    written.append(dst)
                    raise
                written.append(dst)
                if digest != sums.get(src):
                    raise BundleError(f"{os.path.basename(src)} in the bundle is damaged "
                                      "(checksum mismatch); nothing was registered")
    except BaseException:
        for path in written:
            try:
                os.remove(path)
            except OSError:
                pass
        raise
