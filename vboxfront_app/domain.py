"""VBoxFront domain."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shlex
import shutil

from .constants import (
    DESTRUCTIVE_FRAGMENTS,
    DESTRUCTIVE_PREFIXES,
    MEDIA_EXTENSIONS,
    MEDIUM_KEY_ORDER,
    PASSWORD_STDIN,
    TRASH_DIR_NAME,
    TWO_DEVICE_CONTROLLERS,
)
from .models import HealthFinding, MediumBackend, MediumRecord


def parse_hddbackends(text: str) -> list[MediumBackend]:
    """Parse `VBoxManage list hddbackends`.

    Worth asking VirtualBox rather than hardcoding: it is the only authority on
    which formats can be *created* (DMG and VHDX are read-only — no create
    bit), which of them are hard disks at all (RAW only registers DVD and
    floppy extensions, which is why it must not appear as a disk target), and
    which variants each one accepts.
    """
    backends: list[MediumBackend] = []
    pattern = re.compile(
        r"Backend\s+\d+:\s+id='([^']*)'.*?capabilities=(0x[0-9a-fA-F]+|\d+)"
        r".*?extensions='([^']*)'\s+properties=\((.*?)\)\s*$",
        re.DOTALL | re.MULTILINE,
    )
    for match in pattern.finditer(text):
        ident, caps, extensions, props = match.groups()
        exts = []
        for chunk in extensions.split(","):
            m = re.match(r"\s*(\S+)\s*\(([^)]*)\)", chunk)
            if m:
                exts.append((m.group(1), m.group(2)))
        properties = [
            {"name": m.group(1), "type": m.group(2), "default": m.group(3)}
            for m in re.finditer(
                r"name='([^']*)'[^\n]*?type=(\w+)[^\n]*?default='([^']*)'", props
            )
        ]
        backends.append(MediumBackend(
            id=ident,
            capabilities=int(caps, 16) if caps.startswith("0x") else int(caps),
            extensions=exts,
            properties=properties,
        ))
    return backends


def disk_backends(backends: list[MediumBackend]) -> list[MediumBackend]:
    """Backends that can create a hard-disk image file, in listing order."""
    return [b for b in backends if b.can_create() and b.extension_for("HardDisk")]


def parse_systemproperties(text: str) -> dict[str, str]:
    """Parse `VBoxManage list systemproperties` into {label: value}.

    Label-padded-to-a-column, value to end of line. Only one entry is used so
    far — "Default machine folder", which is where VirtualBox itself puts
    images, and therefore a far better first guess for a new disk than `~`.
    """
    props: dict[str, str] = {}
    for line in text.splitlines():
        label, sep, value = line.partition(":")
        if sep and label.strip() and not label.startswith((" ", "\t")):
            props[label.strip()] = value.strip()
    return props


def parse_media_list(text: str) -> list[MediumRecord]:
    """Parse `VBoxManage list [-l] hdds|dvds|floppies` output into records.

    Records are separated by a blank line and every field is a `Label: value`
    at column 0 — but neither can be taken at face value, because a multi-line
    Description is printed flush left and may contain blank lines and text that
    looks exactly like a field. See MEDIUM_KEYS for the rules that disambiguate
    it: a record opens at the `UUID:` following a blank line, a known label
    later in the field order opens a field, and anything else continues the
    field being read.
    """
    blocks: list[dict[str, str]] = []
    kv: dict[str, str] | None = None
    last_key = ""
    after_blank = True
    for line in text.splitlines():
        if not line.strip():
            # Keep blank lines that fall inside a value; the one separating two
            # records is stripped off again when the record is built.
            if kv is not None and last_key:
                kv[last_key] += "\n"
            after_blank = True
            continue
        label, sep, value = line.partition(":")
        label = label.strip()
        order = MEDIUM_KEY_ORDER.get(label, -1) if sep and line[0] not in " \t" else -1
        # A "UUID:" opens a record unless it could be part of the free-text
        # Description being read — that is the one field whose continuations
        # can look like anything. Anywhere else (leading warnings on the
        # merged stderr, say) it still starts the record it announces.
        #
        # A blank line is the record separator, so inside a Description it is
        # the strongest hint that the next "UUID:" is a real one — but only
        # once the record being read has a Location. Without one it is not a
        # record VBoxManage could have finished printing, and believing the
        # line dropped the real medium from the listing and put a phantom in
        # its place, carrying a UUID with a newline in it that every later
        # command would then be handed.
        opens_record = order == 0 and (
            last_key != "Description"
            or (after_blank and kv is not None and "Location" in kv)
        )
        if opens_record:
            kv = {"UUID": value.strip()}
            blocks.append(kv)
            last_key = "UUID"
        elif kv is None:
            pass  # noise ahead of the first record
        elif order > MEDIUM_KEY_ORDER[last_key]:
            kv[label] = value.strip()
            last_key = label
        elif last_key:
            kv[last_key] += "\n" + line.strip()
        after_blank = False

    records: list[MediumRecord] = []
    for block in blocks:
        kv = {k: v.strip("\n") for k, v in block.items()}
        if not kv.get("UUID") or "Location" not in kv:
            continue
        in_use = [e for e in kv.get("In use by VMs", "").split("\n") if e]
        # Several `Property:` lines in a row fold into one value (each repeat
        # is the same field, so it continues rather than opens); the repeated
        # label comes along on the continuation and is trimmed back off here.
        properties = [
            re.sub(r"^Property:\s*", "", line).strip()
            for line in kv.get("Property", "").split("\n")
            if line.strip()
        ]
        records.append(
            MediumRecord(
                uuid=kv.get("UUID", ""),
                location=kv.get("Location", ""),
                fmt=kv.get("Storage format") or kv.get("Format", ""),
                capacity=kv.get("Capacity", ""),
                size_on_disk=kv.get("Size on disk", ""),
                state=kv.get("State", ""),
                access_error=kv.get("Access Error", ""),
                medium_type=kv.get("Type", ""),
                parent_uuid=kv.get("Parent UUID", ""),
                variant=kv.get("Format variant", ""),
                encryption=kv.get("Encryption", ""),
                cipher=kv.get("Cipher", ""),
                password_id=kv.get("Password ID", ""),
                auto_reset=kv.get("Auto-Reset", ""),
                description=kv.get("Description", ""),
                in_use=in_use,
                properties=properties,
            )
        )
    return records


def parse_capacity_mb(capacity: str) -> int | None:
    m = re.match(r"\s*(\d+)\s*MBytes", capacity or "")
    return int(m.group(1)) if m else None


def format_mbytes(mb: int | None) -> str:
    if mb is None:
        return ""
    if mb < 1024:
        return f"{mb} MB"
    gb = mb / 1024
    if gb < 1024:
        return f"{gb:.1f} GB"
    return f"{gb / 1024:.2f} TB"


def parse_in_use_entry(entry: str) -> tuple[str, str] | None:
    """'name (UUID: xxx) [snap (UUID: yyy)]' -> (name, vm_uuid)."""
    m = re.match(r"(.+?) \(UUID: ([0-9a-fA-F-]{36})\)", entry.strip())
    return (m.group(1), m.group(2)) if m else None


def parse_in_use_snapshot(entry: str) -> str:
    """Snapshot name from 'vm (UUID: x) [snap name (UUID: y)]', or "".

    The name is already in the listing, so nothing has to ask
    `snapshot <vm> list` for it. A snapshot name can itself contain brackets
    and parentheses, hence the anchored match on the trailing "(UUID: …)]".
    """
    m = re.search(r"\[(.+) \(UUID: [0-9a-fA-F-]{36}\)\]\s*$", entry.strip())
    return m.group(1) if m else ""


def snapshot_only(rec: MediumRecord) -> bool:
    """True when every attachment of this medium is inside a snapshot.

    `storageattach` only ever touches a VM's *current* state, so a medium
    held solely by a snapshot cannot be detached at all — the slot is not in
    the machine's current configuration, and the detach reports "could not
    locate the attachment slot" with nothing the user can do about it. The
    listing already distinguishes the two: an attachment in a snapshot carries
    a trailing "[snap (UUID: …)]".
    """
    entries = [e for e in rec.in_use if parse_in_use_entry(e)]
    return bool(entries) and all(parse_in_use_snapshot(e) for e in entries)


def snapshot_word(rec: MediumRecord) -> str:
    """Snapshot names this medium is attached in, deduplicated, in order."""
    names: list[str] = []
    for entry in rec.in_use:
        name = parse_in_use_snapshot(entry)
        if name and name not in names:
            names.append(name)
    return ", ".join(names)


def parse_host_drives(text: str) -> list[str]:
    """Device names from `list hostdvds` / `list hostfloppies`.

    Both print `UUID:`/`Name:` pairs; only the name is usable, because that is
    what `storageattach --medium host:<name>` takes. Empty output (no optical
    drive, or none visible to this user) is normal and yields an empty list.
    """
    names = []
    for line in text.splitlines():
        label, sep, value = line.partition(":")
        if sep and label.strip() == "Name" and value.strip():
            names.append(value.strip())
    return names


def controller_types(info: dict[str, str]) -> dict[str, str]:
    """{controller name: type} from machinereadable VM info."""
    types: dict[str, str] = {}
    i = 0
    while f"storagecontrollername{i}" in info:
        types[info[f"storagecontrollername{i}"]] = info.get(f"storagecontrollertype{i}", "")
        i += 1
    return types


def max_device_index(controller_type: str) -> int:
    """Highest --device number a controller accepts.

    IDE and floppy controllers address two devices per port; everything else
    VirtualBox has is one, so a device spin that always went 0–1 offered a slot
    that only VBoxManage's error could reject.
    """
    return 1 if (controller_type or "").upper() in TWO_DEVICE_CONTROLLERS else 0


def parse_vms_list(text: str) -> list[tuple[str, str]]:
    """Parse `VBoxManage list vms` lines: "name" {uuid}."""
    vms = []
    for line in text.splitlines():
        m = re.match(r'"(.*)" \{([0-9a-fA-F-]{36})\}\s*$', line.strip())
        if m:
            vms.append((m.group(1), m.group(2)))
    return vms


def parse_machinereadable(text: str) -> dict[str, str]:
    """Parse `showvminfo --machinereadable` key=value lines (keys/values may be quoted)."""
    info: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if len(k) >= 2 and k[0] == '"' and k[-1] == '"':
            k = k[1:-1]
        if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
            v = v[1:-1]
        info[k] = v
    return info


def vm_storage_controllers(info: dict[str, str]) -> list[tuple[str, int]]:
    """[(controller name, port count)] from machinereadable VM info."""
    controllers = []
    i = 0
    while f"storagecontrollername{i}" in info:
        name = info[f"storagecontrollername{i}"]
        try:
            ports = int(info.get(f"storagecontrollerportcount{i}", "1"))
        except ValueError:
            ports = 1
        controllers.append((name, ports))
        i += 1
    return controllers


def find_attachment(info: dict[str, str], medium_uuid: str) -> tuple[str, int, int] | None:
    """Locate (controller, port, device) where the medium UUID is attached.

    Slot keys must be resolved via the known controller names because other
    per-slot keys ("<ctl>-nonrotational-<p>-<d>") share the same shape.
    """
    want = medium_uuid.lower()
    for ctl, _ports in vm_storage_controllers(info):
        prefix = f"{ctl}-ImageUUID-"
        for key, value in info.items():
            if not key.startswith(prefix) or value.lower() != want:
                continue
            m = re.match(r"(\d+)-(\d+)$", key[len(prefix):])
            if m:
                return (ctl, int(m.group(1)), int(m.group(2)))
    return None


def find_free_port(info: dict[str, str], controller: str, ports: int) -> int:
    """First port whose device-0 slot is empty, or 0 when the controller is full.

    Callers must not treat the fallback as free — see slot_occupant().
    """
    for p in range(ports):
        if not slot_occupant(info, controller, p, 0):
            return p
    return 0


def free_slots(info: dict[str, str], controller: str, ports: int,
               max_device: int, count: int, start_port: int = 0) -> list[tuple[int, int]]:
    """The first `count` empty slots on a controller, at or after `start_port`.

    Attaching a selection means filling consecutive free slots rather than
    asking for each one in turn. Occupied slots are stepped over, never
    replaced: `storageattach` would do that silently, and a batch is exactly
    where nobody is watching each command.

    Returns fewer than `count` — possibly none — when the controller runs out,
    which the caller reports rather than papering over.
    """
    slots: list[tuple[int, int]] = []
    for port in range(max(start_port, 0), ports):
        for device in range(max_device + 1):
            if len(slots) >= count:
                return slots
            if not slot_occupant(info, controller, port, device):
                slots.append((port, device))
    return slots[:count]


def slot_occupant(info: dict[str, str], controller: str, port: int, device: int) -> str:
    """Medium in a controller slot, or "" when it is empty.

    storageattach happily *replaces* an occupied slot: it exits 0 and prints
    nothing, silently detaching whatever was there (verified on 7.2.12), so
    every attach has to check the slot for itself.
    """
    current = info.get(f"{controller}-{port}-{device}", "none")
    return "" if current in ("none", "") else current


def first_free_disk_slot(info: dict[str, str]) -> tuple[str, int, int] | None:
    """(controller, port, device) where a hard disk could go, or None.

    Floppy controllers are skipped; anything else takes a disk. Controllers
    are tried in the VM's own order, which is the order its GUI shows them.
    """
    types = controller_types(info)
    for name, ports in vm_storage_controllers(info):
        ctype = types.get(name, "")
        if ctype.upper() == "I82078":
            continue
        slots = free_slots(info, name, ports, max_device_index(ctype), 1)
        if slots:
            return (name, *slots[0])
    return None


def parse_byte_size(text: str) -> int | None:
    """Accept 4096, 0x1000, 8K, 1.5M, 2G — offsets into an image are quoted
    every which way, and a hex offset is what a partition table hands you."""
    raw = (text or "").strip().replace("_", "")
    if not raw:
        return None
    try:
        if raw.lower().startswith("0x"):
            return int(raw, 16)
    except ValueError:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGTkmgt])?[Bb]?", raw)
    if not m:
        return None
    scale = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}
    value = float(m.group(1)) * scale.get((m.group(2) or "").lower(), 1)
    return int(value)


def is_differencing(medium: MediumRecord) -> bool:
    return "differencing" in medium.medium_type.lower()


def real_parent_uuid(medium: MediumRecord) -> str:
    """Parent UUID, with VBoxManage's literal "base" placeholder resolved to
    "no parent"."""
    return medium.parent_uuid if medium.parent_uuid not in ("", "base") else ""


def child_names(records: list[MediumRecord], uuid: str) -> list[str]:
    """Media in this listing whose parent is `uuid`.

    A medium with children is read-only — VirtualBox answers any write with
    "Write access denied: read-only" — so anything that writes has to check.
    """
    return [
        os.path.basename(r.location) or r.uuid
        for r in records
        if uuid and real_parent_uuid(r) == uuid
    ]


def is_uuid(text: str) -> bool:
    return bool(re.fullmatch(r"\{?[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\}?",
                             (text or "").strip()))


def same_file(a: str, b: str) -> bool:
    """True when two paths name the same file, symlinks and "…/.." included."""
    if not a or not b:
        return False
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.realpath(a) == os.path.realpath(b)


def registered_at(path: str, known: list[MediumRecord] | None,
                  exclude: str = "") -> MediumRecord | None:
    """The registered medium whose backing file is `path`, or None.

    Worth asking before a clone: clonemedium refuses a target that exists, so
    the app moves it aside and removes its backup after success. Overwriting
    a registered image still leaves its registry entry pointing at a different
    medium, so callers must warn or refuse that target. `exclude` is the UUID of the
    clone's own source, which every caller checks for separately and with a
    clearer message.
    """
    for rec in known or []:
        if rec.uuid != exclude and rec.location and same_file(path, rec.location):
            return rec
    return None


def medium_type_word(medium_type: str) -> str:
    return medium_type.split(" ")[0] if medium_type else ""


def state_word(medium: MediumRecord) -> str:
    """The State value alone.

    Belt and braces: `Access Error:` is in MEDIUM_KEYS so it no longer lands in
    `state`, but VBoxManage prints that block unindented and flush left, and
    anything else it adds there in future would fold in the same way. Every
    state comparison goes through here so one stray line cannot silently turn
    off the inaccessible highlight again.
    """
    return medium.state.split("\n")[0].strip()


def is_inaccessible(medium: MediumRecord) -> bool:
    return state_word(medium).lower() == "inaccessible"


def encryption_word(rec: MediumRecord) -> str:
    """What to show in the Encryption column: nothing at all when a medium is
    not encrypted, because "disabled" on every row is noise."""
    if rec.encryption in ("", "disabled"):
        return ""
    return rec.cipher or rec.encryption


def registry_fingerprint(media: dict[str, list[MediumRecord]]) -> str:
    """A short digest of what the registry currently holds.

    VBoxFront is not the only thing that writes it — the VirtualBox GUI, a VM
    starting, or a second copy of this app all can, and a stale row then aims
    an action at the wrong medium. Covers the fields an action keys off
    (UUID, location, capacity, allocation, type and attachments) rather than
    the whole record, so a cosmetic difference does not cry wolf.
    """
    parts = []
    for kind in sorted(media):
        for rec in sorted(media[kind], key=lambda r: r.uuid):
            parts.append("|".join([
                kind, rec.uuid, rec.location, rec.capacity, rec.size_on_disk,
                rec.medium_type, state_word(rec), ",".join(sorted(rec.in_use)),
            ]))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def totals_line(records: list[MediumRecord], shown: int | None = None) -> str:
    """One-line summary: how much has been promised versus actually allocated.

    Deliberately *not* framed as reclaimable space — the gap between capacity
    and size on disk is thin-provisioning headroom that was never allocated,
    whereas compacting reclaims blocks that were allocated and then freed
    inside the guest, which `list -l` cannot see.
    """
    if not records:
        return "No media."
    capacity = [parse_capacity_mb(r.capacity) for r in records]
    on_disk = [parse_capacity_mb(r.size_on_disk) for r in records]
    total_cap = sum(v for v in capacity if v is not None)
    total_disk = sum(v for v in on_disk if v is not None)
    parts = [f"{len(records)} media"]
    if shown is not None and shown != len(records):
        parts[0] += f" ({shown} shown)"
    if total_cap:
        parts.append(f"{format_mbytes(total_cap)} provisioned")
    if any(v is not None for v in on_disk):
        share = f" ({100 * total_disk / total_cap:.0f}% allocated)" if total_cap else ""
        unknown = sum(1 for v in on_disk if v is None)
        tail = f", {unknown} unknown" if unknown else ""
        parts.append(f"{format_mbytes(total_disk)} on disk{share}{tail}")
    return " · ".join(parts)


def format_bytes(count: int | None) -> str:
    """Human-readable byte count, for free-space figures that are not MBytes."""
    if count is None:
        return ""
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def free_bytes_for(path: str) -> int | None:
    """Free space on the filesystem that will hold `path`.

    Walks up to the first directory that exists: the target file is normally
    not there yet, and a path typed into a dialog may name folders that are not
    either. None when nothing can be measured, which callers treat as "no
    opinion" rather than as "full".
    """
    probe = os.path.dirname(os.path.abspath(path)) or os.sep
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def allocation_estimate(size_mb: int, variant: str = "") -> int:
    """Bytes a create will actually consume up front.

    A dynamic image writes little more than a header, so only a Fixed one has
    to fit whole. The allowance on top is for metadata, not slack.
    """
    total = max(size_mb, 0) * 1024 * 1024
    if "fixed" in (variant or "").lower():
        return total + 2 * 1024 * 1024
    return min(total, 8 * 1024 * 1024)


def space_warning(path: str, needed: int) -> str:
    """"" when `path`'s filesystem can hold `needed` bytes, else why not.

    Checked before launching rather than after: VBoxManage does not reserve
    space up front, so an image that does not fit fails somewhere in the middle
    and leaves a partial file behind.
    """
    if needed <= 0:
        return ""
    free = free_bytes_for(path)
    if free is None or needed <= free:
        return ""
    return (
        f"{os.path.dirname(os.path.abspath(path)) or os.sep} has "
        f"{format_bytes(free)} free, but this needs about "
        f"{format_bytes(needed)}."
    )


EXPORT_FIELDS = [
    ("name", lambda r: os.path.basename(r.location) or r.uuid),
    ("format", lambda r: r.fmt),
    ("capacity_mb", lambda r: parse_capacity_mb(r.capacity)),
    ("size_on_disk_mb", lambda r: parse_capacity_mb(r.size_on_disk)),
    ("type", lambda r: medium_type_word(r.medium_type)),
    ("state", lambda r: state_word(r)),
    ("access_error", lambda r: r.access_error),
    ("encryption", lambda r: encryption_word(r)),
    ("password_id", lambda r: r.password_id),
    ("in_use_by", lambda r: ", ".join(
        e[0] for e in (parse_in_use_entry(x) for x in r.in_use) if e)),
    ("snapshot", lambda r: snapshot_word(r)),
    ("parent_uuid", lambda r: real_parent_uuid(r)),
    ("location", lambda r: r.location),
    ("uuid", lambda r: r.uuid),
]


def medium_details(rec: MediumRecord) -> list[tuple[str, str]]:
    """(label, value) rows describing one medium, for the details pane.

    Everything here is already in the record the listing was built from, so
    opening the pane runs nothing. `Info` shells out to `showmediuminfo` and
    prints into the output panel, which made the panel a viewer; this is the
    same information laid out, without a command and without scrolling past
    whatever ran last.
    """
    rows = [
        ("Name", os.path.basename(rec.location) or rec.uuid),
        ("Location", rec.location),
        ("UUID", rec.uuid),
        ("Format", f"{rec.fmt} ({rec.variant})" if rec.variant else rec.fmt),
        ("Capacity", rec.capacity),
        ("Size on disk", rec.size_on_disk),
        ("Type", rec.medium_type),
        ("State", state_word(rec)),
    ]
    if rec.access_error:
        rows.append(("Access error", rec.access_error))
    if real_parent_uuid(rec):
        rows.append(("Parent UUID", real_parent_uuid(rec)))
    if rec.auto_reset:
        rows.append(("Auto-reset", rec.auto_reset))
    if encryption_word(rec):
        rows.append(("Encryption", encryption_word(rec)))
    if rec.password_id:
        rows.append(("Password ID", rec.password_id))
    if rec.in_use:
        rows.append(("In use by", "\n".join(rec.in_use)))
    if snapshot_word(rec):
        rows.append(("Snapshot", snapshot_word(rec)))
    for prop in rec.properties:
        rows.append(("Property", prop))
    if rec.description:
        rows.append(("Description", rec.description))
    return [(label, value) for label, value in rows if value]


def export_rows(records: list[MediumRecord]) -> list[dict]:
    return [{name: fn(rec) for name, fn in EXPORT_FIELDS} for rec in records]


def records_to_csv(records: list[MediumRecord]) -> str:
    """CSV with a header row. `csv` rather than joins: a Description or a path
    can contain the separator, and quoting it is not this file's job."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=[name for name, _fn in EXPORT_FIELDS],
                            lineterminator="\n")
    writer.writeheader()
    for row in export_rows(records):
        writer.writerow({k: "" if v is None else v for k, v in row.items()})
    return buf.getvalue()


def records_to_json(records: list[MediumRecord]) -> str:
    return json.dumps(export_rows(records), indent=2) + "\n"


def records_to_tsv(records: list[MediumRecord]) -> str:
    """Tab-separated rows for pasting into a spreadsheet or a ticket.

    No quoting layer, so anything containing a tab or newline is flattened —
    the clipboard is a lossy target and a mangled cell is worse than a space.
    """
    lines = ["\t".join(name for name, _fn in EXPORT_FIELDS)]
    for row in export_rows(records):
        lines.append("\t".join(
            "" if v is None else re.sub(r"[\t\r\n]+", " ", str(v))
            for v in row.values()
        ))
    return "\n".join(lines) + "\n"


def parse_uuid_collision(text: str) -> tuple[str, str] | None:
    """(conflicting file, UUID) from VBoxManage's "already exists" refusal.

    Verified against 7.2.12, which answers an attempt to open a copied image
    with:

        Cannot register the hard disk '/x/copy.vdi' {UUID} because a hard
        disk '/x/original.vdi' with UUID {UUID} already exists

    That is the commonest reason an image sits unregistered, and the fix —
    stamping it with a fresh UUID — is one the app already ships. Nothing
    pointed at it before.
    """
    match = re.search(
        r"because an? (?:hard disk|DVD image|floppy image) '([^']*)' with "
        r"UUID \{([0-9a-fA-F-]{36})\} already exists",
        text,
    )
    return (match.group(1), match.group(2)) if match else None


FILTER_FIELDS = {
    "name": lambda r: os.path.basename(r.location) or r.uuid,
    "fmt": lambda r: r.fmt,
    "format": lambda r: r.fmt,
    "type": lambda r: medium_type_word(r.medium_type),
    "state": lambda r: state_word(r),
    "encryption": lambda r: encryption_word(r),
    "vm": lambda r: ", ".join(
        e[0] for e in (parse_in_use_entry(x) for x in r.in_use) if e),
    "snapshot": lambda r: snapshot_word(r),
    "location": lambda r: r.location,
    "uuid": lambda r: r.uuid,
    "description": lambda r: r.description,
}


FILTER_SIZES = {
    "size": lambda r: parse_capacity_mb(r.size_on_disk),
    "capacity": lambda r: parse_capacity_mb(r.capacity),
}


FILTER_TERM = re.compile(
    r"(?P<field>[a-z]+)\s*:\s*(?P<op>[<>]=?|=)?\s*(?P<value>\S+)", re.I
)


def parse_filter(text: str) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Split a filter box into (field terms, plain words).

    `fmt:vdi size:>10G vm:alpha backup` is four terms: three structured and
    one substring. A colon that names no known field stays a plain word, so a
    Windows path pasted into the box still filters the way it always did.
    """
    terms: list[tuple[str, str, str]] = []
    words: list[str] = []
    for token in (text or "").split():
        match = FILTER_TERM.fullmatch(token)
        field = match.group("field").lower() if match else ""
        if match and (field in FILTER_FIELDS or field in FILTER_SIZES):
            terms.append((field, match.group("op") or "", match.group("value")))
        else:
            words.append(token.lower())
    return terms, words


def record_matches(rec: MediumRecord, terms: list[tuple[str, str, str]],
                   words: list[str], cells: list[str]) -> bool:
    """Whether one medium satisfies every term and every plain word.

    Terms are ANDed: narrowing is what the extra syntax is for. A term whose
    size cannot be read (an inaccessible medium reports none) fails rather
    than passes — "size:>10G" should not quietly include the unknown.
    """
    for field, op, value in terms:
        if field in FILTER_SIZES:
            have = FILTER_SIZES[field](rec)
            want = parse_byte_size(value)
            if have is None or want is None:
                return False
            have_bytes = have * 1024 * 1024
            if op == ">" and not have_bytes > want:
                return False
            if op == ">=" and not have_bytes >= want:
                return False
            if op == "<" and not have_bytes < want:
                return False
            if op == "<=" and not have_bytes <= want:
                return False
            if op in ("", "=") and have_bytes != want:
                return False
            continue
        if value.lower() not in (FILTER_FIELDS[field](rec) or "").lower():
            return False
    return all(any(word in cell.lower() for cell in cells) for word in words)


def scan_for_images(folders: list[str], known: list[str],
                    recursive: bool = True) -> list[tuple[str, str]]:
    """(kind, path) for image files under `folders` that nothing knows about.

    "Known" is the registry plus the app's own library, compared by real path
    so a symlinked VM folder does not report every disk in it as an orphan.
    Symlinked *directories* are not followed at all: a link back up the tree
    would otherwise walk forever. Trash folders are skipped: offering to adopt
    an image the user just deleted is the opposite of what the scan is for.
    """
    seen = set()
    for path in known:
        if path:
            seen.add(os.path.realpath(path))
    found: list[tuple[str, str]] = []
    reported = set()
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        for root, dirs, files in os.walk(folder):
            if not recursive:
                dirs[:] = []
            else:
                dirs[:] = [d for d in dirs if d != TRASH_DIR_NAME]
            for name in files:
                kind = MEDIA_EXTENSIONS.get(os.path.splitext(name)[1].lower())
                if not kind:
                    continue
                full = os.path.join(root, name)
                real = os.path.realpath(full)
                if real in seen or real in reported:
                    continue
                reported.add(real)
                found.append((kind, full))
    return sorted(found, key=lambda item: item[1])


def health_findings(media: dict[str, list[MediumRecord]]) -> list[HealthFinding]:
    """Everything checkable about a registry without running a command.

    Deliberately quiet about the merely unusual: only states that stop a medium
    from being used, or that will surprise someone later, are reported, so an
    empty result is a meaningful all-clear rather than a shrug.
    """
    findings: list[HealthFinding] = []
    for kind, records in media.items():
        by_uuid = {r.uuid: r for r in records}
        for rec in records:
            if is_inaccessible(rec):
                findings.append(HealthFinding(
                    kind, rec.uuid, rec.location, "error",
                    "VirtualBox cannot open this medium",
                    (rec.access_error.split("\n")[0] if rec.access_error
                     else "use Resolve… to re-point or repair it"),
                    code="inaccessible",
                ))
            elif rec.location and not os.path.exists(rec.location):
                # Registered, not yet noticed as broken: VBoxSVC caches State
                # and only re-checks when something opens the medium.
                findings.append(HealthFinding(
                    kind, rec.uuid, rec.location, "error",
                    "the backing file is missing",
                    "the registry still says it is fine; Resolve… can re-point "
                    "it, or Clean up… can unregister it",
                    code="missing-file",
                ))
            parent = real_parent_uuid(rec)
            if parent and parent not in by_uuid:
                findings.append(HealthFinding(
                    kind, rec.uuid, rec.location, "error",
                    "its parent image is not registered",
                    f"parent {parent}; Image UUID tools can reattach it",
                    code="orphan-child",
                ))
            if (rec.encryption not in ("", "disabled") and not rec.password_id):
                findings.append(HealthFinding(
                    kind, rec.uuid, rec.location, "warning",
                    "encrypted with no password ID recorded",
                    "nothing names the key this image needs",
                    code="no-password-id",
                ))
            if snapshot_only(rec):
                findings.append(HealthFinding(
                    kind, rec.uuid, rec.location, "warning",
                    f"held only by the snapshot “{snapshot_word(rec)}”",
                    "storageattach edits the current state only, so Detach "
                    "cannot release it; delete or restore that snapshot",
                    code="snapshot-only",
                ))
            capacity = parse_capacity_mb(rec.capacity)
            on_disk = parse_capacity_mb(rec.size_on_disk)
            if (capacity and on_disk is not None and on_disk == 0
                    and not is_inaccessible(rec)):
                findings.append(HealthFinding(
                    kind, rec.uuid, rec.location, "warning",
                    "nothing has been written to it",
                    f"{format_mbytes(capacity)} provisioned, 0 allocated",
                    code="empty",
                ))
    return findings


def discard_findings(media: dict[str, list[MediumRecord]],
                     infos: dict[str, dict[str, str]]) -> list[HealthFinding]:
    """Disks attached to a VM with `--discard` off.

    This is the usual reason a compact gives nothing back: without it the
    guest's TRIM never reaches the image, so blocks freed inside the guest
    stay allocated in the file forever and `modifymedium --compact` has
    nothing to find. The attach dialog has always offered the flag and
    nothing ever reported its absence.

    Separate from `health_findings` because it is the one check that cannot be
    made from the registry alone — it needs a `showvminfo` per VM.
    """
    findings: list[HealthFinding] = []
    seen: set[tuple[str, str]] = set()
    for rec in media.get("disk", []):
        for info in infos.values():
            slot = find_attachment(info, rec.uuid)
            if not slot:
                continue
            controller, port, device = slot
            vm = info.get("name", "")
            if info.get(f"{controller}-discard-{port}-{device}", "off") != "off":
                continue
            if (rec.uuid, vm) in seen:
                continue
            seen.add((rec.uuid, vm))
            findings.append(HealthFinding(
                "disk", rec.uuid, rec.location, "warning",
                f"attached to {vm} without --discard" if vm
                else "attached without --discard",
                "the guest's TRIM never reaches the image, so compacting it "
                "can reclaim nothing",
                code="no-discard",
            ))
    return findings


def health_report(findings: list[HealthFinding]) -> str:
    if not findings:
        return "No problems found.\n"
    return "\n".join(f.line() for f in findings) + "\n"


def is_destructive_command(command: str) -> bool:
    """True when re-running this would change or destroy data.

    `repairhd` counts unless it is the dry run, which only reports.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if "repairhd" in tokens and "-dry-run" not in tokens:
        return True
    if any(fragment in tokens for fragment in DESTRUCTIVE_FRAGMENTS):
        return True
    return any(
        token.startswith(prefix) and token != f"{prefix}-"
        for token in tokens
        for prefix in DESTRUCTIVE_PREFIXES
    )


def rerun_blocker(command: str) -> str:
    """Why a history entry cannot be replayed as it stands, or "".

    A password is never in the command line — it went over stdin, or through a
    temp file that was removed when the command exited — so replaying one of
    those either hangs waiting for input or fails on a file that is gone. Both
    are worse than refusing.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if PASSWORD_STDIN in tokens or any(t.endswith(f"={PASSWORD_STDIN}") for t in tokens):
        return ("the password for this command went over stdin and was never "
                "stored; run it again from its own dialog")
    if any("vboxfront-pw-" in t for t in tokens):
        return ("this command read its passwords from temp files that were "
                "removed when it exited; run it again from the Encryption dialog")
    return ""


def format_duration(seconds: float) -> str:
    """A short human duration: 45s, 3m10s, 1h04m."""
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def eta_text(percent: int, elapsed: float) -> str:
    """"elapsed … ~remaining left", or just the elapsed time.

    Linear extrapolation from the percentage VBoxManage prints, which is the
    only signal there is. Deliberately prefixed with "~": a clone is not
    uniform work, and the number is a hint, not a promise. Nothing is guessed
    below 5% or in the first couple of seconds, where the estimate would swing
    wildly enough to be worse than silence.
    """
    shown = format_duration(elapsed)
    if percent <= 0 or percent >= 100 or elapsed < 2 or percent < 5:
        return shown
    remaining = elapsed * (100 - percent) / percent
    return f"{shown} · ~{format_duration(remaining)} left"


def reclaim_line(name: str, before_mb: int | None, after_mb: int | None) -> str:
    """What a compact actually gave back, measured rather than predicted."""
    if before_mb is None or after_mb is None:
        return ""
    delta = before_mb - after_mb
    if delta > 0:
        return f"[compact] {name}: reclaimed {format_mbytes(delta)} ({format_mbytes(before_mb)} → {format_mbytes(after_mb)})"
    if delta < 0:
        return f"[compact] {name}: grew by {format_mbytes(-delta)}"
    return f"[compact] {name}: nothing to reclaim ({format_mbytes(after_mb)} on disk)"
