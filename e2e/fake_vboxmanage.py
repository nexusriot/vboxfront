#!/usr/bin/env python3
"""A stateful stand-in for VBoxManage, for the hermetic end-to-end suite.

The unit suite's fake is a shell script that echoes canned listings: enough to
check that a command was built correctly, but it cannot answer the question
these tests exist for — *did the operation actually change anything?* This one
keeps a registry in a JSON file and real (sparse) image files on disk, so a
create is visible to the next list, a clone produces a second file, a resize
moves the number the listing reports, and `closemedium --delete` really unlinks
the backing file. The app under test is unmodified; only `VBoxManage` is.

It implements the subset VBoxFront actually invokes, in VBoxManage 7.2.12's
output shapes (verified against a real installation). Anything outside that
subset exits 1 with a message naming it, so a command the app starts sending
without a fake behind it fails loudly instead of silently passing.

State lives in $VBOXFRONT_E2E_STATE; every invocation is appended to
$VBOXFRONT_E2E_LOG so a test can assert on the exact argv the app chose.
"""

from __future__ import annotations

import json
import os
import sys
import uuid as uuidlib

STATE_PATH = os.environ.get("VBOXFRONT_E2E_STATE", "/tmp/vboxfront-e2e-state.json")
LOG_PATH = os.environ.get("VBOXFRONT_E2E_LOG", "")

#: Mirrors `list hddbackends` on 7.2.12 closely enough for the dialogs that
#: read capability bits: VDI has DIFFERENCING and no SPLIT2G, VMDK has SPLIT2G,
#: DMG has no create bit at all, RAW registers only DVD/floppy extensions.
BACKENDS = """\
Supported hard disk backends:

Backend 0: id='VMDK' description='VMDK' capabilities=0x0a7f extensions='vmdk (HardDisk)' properties=(
  name='RawDrive' desc='' type=string flags=0x00 default='', 
  name='Partitions' desc='' type=string flags=0x00 default='')
Backend 1: id='VDI' description='VDI' capabilities=0x0e77 extensions='vdi (HardDisk)' properties=(
  name='AllocationBlockSize' desc='' type=int flags=0x04 default='1048576')
Backend 2: id='VHD' description='VHD' capabilities=0x0a77 extensions='vhd (HardDisk)' properties=()
Backend 3: id='DMG' description='DMG' capabilities=0x0240 extensions='dmg (DVD)' properties=()
Backend 4: id='RAW' description='RAW' capabilities=0x0262 extensions='img (Floppy)' properties=()
"""

KIND_LIST = {"hdds": "disk", "dvds": "dvd", "floppies": "floppy"}
EXT_KIND = {".vdi": "disk", ".vmdk": "disk", ".vhd": "disk",
            ".iso": "dvd", ".img": "floppy", ".ima": "floppy", ".flp": "floppy"}


def load() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {"media": [], "vms": [], "running": []}


def save(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=1)
    os.replace(tmp, STATE_PATH)


def fail(message: str) -> int:
    sys.stderr.write(f"VBoxManage: error: {message}\n")
    return 1


def find(state: dict, token: str) -> dict | None:
    """Resolve a medium the way VBoxManage does: by UUID or by filename."""
    token = (token or "").strip()
    for medium in state["media"]:
        if medium["uuid"] == token:
            return medium
    for medium in state["media"]:
        if medium["location"] == token or os.path.basename(medium["location"]) == token:
            return medium
    return None


def file_mb(path: str) -> int:
    """Allocated size in whole MB — st_blocks, so a sparse file reads as small."""
    try:
        info = os.stat(path)
    except OSError:
        return 0
    return (info.st_blocks * 512) // (1024 * 1024)


#: Marks a UUID written into an image file. VirtualBox keeps a medium's UUID
#: *inside* the image, which is the whole reason copying a .vdi produces two
#: files it refuses to register together. Modelling that beats guessing from
#: file contents, which called two blank images of the same size copies of
#: each other.
IMAGE_MAGIC = b"VBFAKE01"


def stamp_uuid(path: str, uuid: str) -> None:
    """Write a medium's UUID into its image file."""
    if not uuid:
        return
    try:
        with open(path, "r+b") as handle:
            handle.seek(0)
            handle.write(IMAGE_MAGIC + uuid.encode())
    except OSError:
        pass


def read_uuid(path: str) -> str:
    """The UUID stamped into an image file, or "" when there is none."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(len(IMAGE_MAGIC) + 36)
    except OSError:
        return ""
    if not head.startswith(IMAGE_MAGIC):
        return ""
    return head[len(IMAGE_MAGIC):].decode("ascii", "replace")


def write_image(path: str, size_mb: int, fixed: bool = False,
                uuid: str = "") -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as handle:
        if fixed:
            # Really occupy the space, so "size on disk" differs from a dynamic
            # image of the same capacity the way it does for real.
            handle.write(b"\0" * (size_mb * 1024 * 1024))
        else:
            handle.truncate(size_mb * 1024 * 1024)
    stamp_uuid(path, uuid)


def in_use_lines(state: dict, medium: dict) -> list[str]:
    lines = []
    for vm in state["vms"]:
        for att in vm.get("attachments", []):
            if att.get("uuid") == medium["uuid"]:
                entry = f"{vm['name']} (UUID: {vm['uuid']})"
                if att.get("snapshot"):
                    entry += (f" [{att['snapshot']} (UUID: "
                              f"{att.get('snapshot_uuid', '0' * 8 + '-0000-0000-0000-' + '0' * 12)})]")
                lines.append(entry)
    return lines


def describe(state: dict, medium: dict) -> str:
    """One `list -l` block, in VBoxManage's field order and spacing."""
    out = [f"UUID:           {medium['uuid']}"]
    out.append(f"Parent UUID:    {medium.get('parent') or 'base'}")
    out.append(f"State:          {medium.get('state', 'created')}")
    if medium.get("access_error"):
        out.append(f"Access Error:   {medium['access_error']}")
    if medium.get("description"):
        # Continuations are printed flush left, blank lines and all.
        head, _, rest = medium["description"].partition("\n")
        out.append(f"Description:    {head}")
        out.extend(rest.split("\n") if rest else [])
    kindword = "differencing" if medium.get("parent") else "base"
    out.append(f"Type:           {medium.get('type', 'normal')} ({kindword})")
    if medium.get("autoreset"):
        out.append(f"Auto-Reset:     {medium['autoreset']}")
    out.append(f"Location:       {medium['location']}")
    out.append(f"Storage format: {medium['format'].lower()}")
    out.append(f"Format variant: {medium.get('variant', 'dynamic default')}")
    out.append(f"Capacity:       {medium['capacity_mb']} MBytes")
    out.append(f"Size on disk:   {file_mb(medium['location'])} MBytes")
    out.append(f"Encryption:     {medium.get('encryption', 'disabled')}")
    if medium.get("cipher"):
        out.append(f"Cipher:         {medium['cipher']}")
    if medium.get("password_id"):
        out.append(f"Password ID:    {medium['password_id']}")
    for name, value in sorted(medium.get("properties", {}).items()):
        out.append(f"Property:       {name}={value}")
    users = in_use_lines(state, medium)
    if users:
        out.append(f"In use by VMs:  {users[0]}")
        out.extend(f"                {u}" for u in users[1:])
    return "\n".join(out)


def cmd_list(state: dict, args: list[str]) -> int:
    args = [a for a in args if a not in ("-l", "--long")]
    if not args:
        return fail("list: nothing to list")
    what = args[0]
    if what == "hddbackends":
        sys.stdout.write(BACKENDS)
        return 0
    if what == "systemproperties":
        print(f"API version:                     7_2")
        print(f"Default machine folder:          {os.environ.get('VBOXFRONT_E2E_VMDIR', '/vms')}")
        print(f"Default hard disk format:        VDI")
        return 0
    if what in ("hostdvds", "hostfloppies"):
        return 0  # no physical drives in a container, which is normal output
    if what == "vms":
        for vm in state["vms"]:
            print(f'"{vm["name"]}" {{{vm["uuid"]}}}')
        return 0
    if what == "runningvms":
        for vm in state["vms"]:
            if vm["uuid"] in state.get("running", []):
                print(f'"{vm["name"]}" {{{vm["uuid"]}}}')
        return 0
    if what in KIND_LIST:
        kind = KIND_LIST[what]
        blocks = [describe(state, m) for m in state["media"] if m["kind"] == kind]
        if blocks:
            sys.stdout.write("\n\n".join(blocks) + "\n")
        return 0
    return fail(f"list: unsupported subject {what!r}")


def uuid_already_taken(state: dict, path: str) -> dict | None:
    """A registered medium holding the UUID stamped into `path`.

    What copying a .vdi actually does: the UUID is inside the image, so the
    copy and the original claim the same one and VirtualBox refuses the
    second. A file *at* the registered location is the medium itself, not a
    copy of it.
    """
    stamped = read_uuid(path)
    if not stamped:
        return None
    for medium in state["media"]:
        if medium["uuid"] != stamped:
            continue
        if os.path.abspath(medium["location"]) == os.path.abspath(path):
            return None
        return medium
    return None


def cmd_showmediuminfo(state: dict, args: list[str]) -> int:
    if len(args) < 2:
        return fail("showmediuminfo: missing arguments")
    medium = find(state, args[1])
    if medium is None:
        # Opening a path that is not registered yet is how the app's library
        # re-registers standalone images on every refresh.
        path = args[1]
        if os.path.isfile(path):
            # A copy of a registered image carries its UUID, and VirtualBox
            # refuses to open it. The fake stands in for that by matching on
            # content, which is what a copy shares.
            twin = uuid_already_taken(state, path)
            if twin is not None:
                return fail(
                    f"Cannot register the hard disk '{path}' "
                    f"{{{twin['uuid']}}} because a hard disk "
                    f"'{twin['location']}' with UUID {{{twin['uuid']}}} "
                    "already exists"
                )
            kind = EXT_KIND.get(os.path.splitext(path)[1].lower(), args[0])
            medium = register(state, path, kind=kind)
            save(state)
        else:
            return fail(f"Could not find file for the medium '{args[1]}'")
    sys.stdout.write(describe(state, medium) + "\n")
    return 0


def register(state: dict, path: str, kind: str = "disk", fmt: str = "",
             capacity_mb: int | None = None, variant: str = "dynamic default",
             parent: str = "") -> dict:
    fmt = fmt or os.path.splitext(path)[1].lstrip(".").upper() or "VDI"
    if capacity_mb is None:
        try:
            capacity_mb = max(os.path.getsize(path) // (1024 * 1024), 1)
        except OSError:
            capacity_mb = 1
    medium = {
        "uuid": str(uuidlib.uuid4()), "location": os.path.abspath(path),
        "kind": kind, "format": fmt, "capacity_mb": capacity_mb,
        "variant": variant, "type": "normal", "state": "created",
        "parent": parent, "encryption": "disabled", "properties": {},
    }
    if fmt.upper() == "VDI":
        medium["properties"]["AllocationBlockSize"] = "1048576"
    stamp_uuid(medium["location"], medium["uuid"])
    state["media"].append(medium)
    return medium


def opt(args: list[str], name: str) -> str | None:
    """Value of `--name value` or `--name=value`, or None."""
    for i, arg in enumerate(args):
        if arg == name and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def cmd_createmedium(state: dict, args: list[str]) -> int:
    kind = args[0] if args and not args[0].startswith("-") else "disk"
    path = opt(args, "--filename")
    if not path:
        return fail("createmedium: --filename is required")
    if os.path.exists(path):
        return fail(f"Could not create the medium storage unit '{path}'"
                    " (VERR_ALREADY_EXISTS)")
    diffparent = opt(args, "--diffparent")
    variant = opt(args, "--variant") or ""
    if diffparent:
        parent = find(state, diffparent)
        if parent is None:
            return fail(f"Could not find a medium matching '{diffparent}'")
        size_mb = parent["capacity_mb"]
        write_image(path, 0)
        medium = register(state, path, kind="disk",
                          fmt=opt(args, "--format") or parent["format"],
                          capacity_mb=size_mb, parent=parent["uuid"])
        medium["variant"] = "dynamic differencing"
    else:
        try:
            size_mb = int(opt(args, "--size") or 0)
        except ValueError:
            return fail("createmedium: --size must be a number")
        if size_mb <= 0:
            return fail("createmedium: --size must be above zero")
        fixed = "fixed" in variant.lower()
        write_image(path, size_mb, fixed=fixed)
        medium = register(state, path, kind=kind,
                          fmt=opt(args, "--format") or
                          ("VDI" if kind == "disk" else "RAW"),
                          capacity_mb=size_mb,
                          variant="fixed default" if fixed else "dynamic default")
        if "formatted" in variant.lower():
            medium["properties"]["Formatted"] = "FAT"
    save(state)
    print("0%...10%...50%...90%...100%")
    print(f"Medium created. UUID: {medium['uuid']}")
    return 0


def cmd_closemedium(state: dict, args: list[str]) -> int:
    rest = [a for a in args if a not in ("disk", "dvd", "floppy")]
    delete = "--delete" in rest
    rest = [a for a in rest if a != "--delete"]
    if not rest:
        return fail("closemedium: no medium given")
    medium = find(state, rest[0])
    if medium is None:
        return fail(f"Could not find a medium matching '{rest[0]}'")
    if in_use_lines(state, medium):
        return fail("Medium is attached to a virtual machine (VBOX_E_OBJECT_IN_USE)")
    if any(m.get("parent") == medium["uuid"] for m in state["media"]):
        return fail("Medium has differencing children (VBOX_E_OBJECT_IN_USE)")
    state["media"].remove(medium)
    if delete:
        try:
            os.remove(medium["location"])
        except OSError:
            pass
    save(state)
    return 0


def cmd_modifymedium(state: dict, args: list[str]) -> int:
    rest = [a for a in args if a not in ("disk", "dvd", "floppy")]
    if not rest:
        return fail("modifymedium: no medium given")
    medium = find(state, rest[0])
    if medium is None:
        return fail(f"Could not find a medium matching '{rest[0]}'")
    has_children = any(m.get("parent") == medium["uuid"] for m in state["media"])
    writes = {"--compact", "--resize", "--resizebyte", "--move"}
    touches_image = any(a in writes for a in rest)
    if has_children and touches_image:
        return fail("Write access denied: read-only (VERR_VD_IMAGE_READ_ONLY)")
    # --setlocation is the one operation that works on a medium whose file is
    # gone: that is what it is for.
    if touches_image and not os.path.isfile(medium["location"]):
        return fail(f"Could not open the medium '{medium['location']}'"
                    " (VERR_FILE_NOT_FOUND)")
    if "--compact" in rest:
        # Shrink the file the way a compact does, keeping capacity put.
        # Keeps its identity too, which lives inside the image.
        write_image(medium["location"], max(medium["capacity_mb"] // 8, 1),
                    uuid=medium["uuid"])
        print("0%...50%...100%")
    size = opt(rest, "--resize")
    size_b = opt(rest, "--resizebyte")
    if size or size_b:
        new_mb = int(size) if size else max(int(size_b) // (1024 * 1024), 1)
        if new_mb < medium["capacity_mb"]:
            return fail("Shrinking is not yet supported (VERR_NOT_SUPPORTED)")
        medium["capacity_mb"] = new_mb
        print("0%...100%")
    new_type = opt(rest, "--type")
    if new_type:
        if medium.get("parent"):
            return fail("Cannot change the type of a differencing medium")
        medium["type"] = new_type
    description = opt(rest, "--description")
    if description is not None:
        medium["description"] = description
    autoreset = opt(rest, "--autoreset")
    if autoreset:
        medium["autoreset"] = autoreset
    move_to = opt(rest, "--move")
    if move_to:
        os.makedirs(os.path.dirname(move_to) or ".", exist_ok=True)
        os.replace(medium["location"], move_to)
        medium["location"] = os.path.abspath(move_to)
        print("0%...100%")
    relocate = opt(rest, "--setlocation")
    if relocate:
        medium["location"] = os.path.abspath(relocate)
        medium["state"] = "created"
        medium.pop("access_error", None)
    save(state)
    return 0


def cmd_clonemedium(state: dict, args: list[str]) -> int:
    rest = [a for a in args if a not in ("disk", "dvd", "floppy")]
    positional = []
    skip = False
    for arg in rest:
        if skip:
            skip = False
            continue
        if arg.startswith("--"):
            skip = arg in ("--format", "--variant")
            continue
        positional.append(arg)
    if len(positional) < 2:
        return fail("clonemedium: source and target are required")
    source = find(state, positional[0])
    if source is None:
        return fail(f"Could not find a medium matching '{positional[0]}'")
    target = positional[1]
    existing = "--existing" in rest
    if os.path.exists(target) and not existing:
        return fail(f"Could not create the clone medium '{target}'"
                    " (VERR_ALREADY_EXISTS)")
    if existing and not os.path.exists(target):
        return fail(f"Could not open the medium '{target}' (VERR_FILE_NOT_FOUND)")
    fmt = opt(rest, "--format") or source["format"]
    variant = opt(rest, "--variant") or ""
    if existing:
        clone = find(state, target)
        if clone is not None:
            save(state)
            print("0%...100%")
            print(f"Clone medium created in format '{clone['format']}'. "
                  f"UUID: {clone['uuid']}")
            return 0
    if fmt.upper() == "RAW":
        # A flat image: the whole capacity, and nothing registered — RAW
        # cannot be a managed medium, which is why it is an export only.
        write_image(target, source["capacity_mb"], fixed=True)
        print("0%...50%...100%")
        print("Clone medium created in format 'RAW'.")
        return 0
    write_image(target, source["capacity_mb"], fixed="fixed" in variant.lower())
    clone = register(state, target, kind=source["kind"], fmt=fmt,
                     capacity_mb=source["capacity_mb"],
                     variant="fixed default" if "fixed" in variant.lower()
                     else "dynamic default")
    save(state)
    print("0%...50%...100%")
    print(f"Clone medium created in format '{fmt}'. UUID: {clone['uuid']}")
    return 0


def cmd_convertfromraw(state: dict, args: list[str]) -> int:
    positional = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg.startswith("--"):
            skip = arg in ("--format", "--variant")
            continue
        positional.append(arg)
    if len(positional) < 2:
        return fail("convertfromraw: source and target are required")
    src, dst = positional[0], positional[1]
    if src == "stdin":
        # `convertfromraw stdin <target> <bytes>`: the image arrives on
        # standard input and the length is mandatory, because the backend
        # sizes the target before a sector of it turns up.
        if len(positional) < 3:
            return fail("convertfromraw stdin: the byte count is required")
        try:
            expected = int(positional[2])
        except ValueError:
            return fail("convertfromraw stdin: the byte count must be a number")
        read = 0
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        with open(dst, "wb") as handle:
            while read < expected:
                chunk = sys.stdin.buffer.read(min(1 << 20, expected - read))
                if not chunk:
                    break
                handle.write(chunk)
                read += len(chunk)
        if read != expected:
            os.remove(dst)
            return fail(
                f"Cannot read {expected} bytes from standard input, got {read} "
                "(VERR_EOF)"
            )
        print(f"Converting from stdin to file=\"{dst}\"...")
        return 0
    if not os.path.isfile(src):
        return fail(f"Cannot open file '{src}'")
    if os.path.exists(dst):
        return fail(f"Cannot create file '{dst}' (VERR_ALREADY_EXISTS)")
    size_mb = max(os.path.getsize(src) // (1024 * 1024), 1)
    write_image(dst, size_mb)
    # convertfromraw only writes the file; it registers nothing, which is why
    # the app has to put the result in its own library.
    print("Converting from raw image file=\"%s\" to file=\"%s\"..." % (src, dst))
    return 0


def cmd_storageattach(state: dict, args: list[str]) -> int:
    if not args:
        return fail("storageattach: no VM given")
    vm = next((v for v in state["vms"]
               if v["uuid"] == args[0] or v["name"] == args[0]), None)
    if vm is None:
        return fail(f"Could not find a registered machine named '{args[0]}'")
    controller = opt(args, "--storagectl")
    port, device = opt(args, "--port"), opt(args, "--device")
    token = opt(args, "--medium")
    if controller is None or port is None or device is None:
        return fail("storageattach: --storagectl, --port and --device are required")
    if not any(c["name"] == controller for c in vm["controllers"]):
        return fail(f"Could not find a controller named '{controller}'")
    slot = {"controller": controller, "port": int(port), "device": int(device)}
    vm["attachments"] = [
        a for a in vm.get("attachments", [])
        if (a["controller"], a["port"], a["device"])
        != (slot["controller"], slot["port"], slot["device"])
    ]
    if token not in (None, "none"):
        medium = find(state, token)
        slot["uuid"] = medium["uuid"] if medium else ""
        slot["location"] = medium["location"] if medium else token
        vm["attachments"].append(slot)
    save(state)
    return 0


def cmd_showvminfo(state: dict, args: list[str]) -> int:
    if not args:
        return fail("showvminfo: no VM given")
    vm = next((v for v in state["vms"]
               if v["uuid"] == args[0] or v["name"] == args[0]), None)
    if vm is None:
        return fail(f"Could not find a registered machine named '{args[0]}'")
    print(f'name="{vm["name"]}"')
    print(f'UUID="{vm["uuid"]}"')
    for i, ctl in enumerate(vm["controllers"]):
        print(f'storagecontrollername{i}="{ctl["name"]}"')
        print(f'storagecontrollertype{i}="{ctl.get("type", "IntelAhci")}"')
        print(f'storagecontrollerinstance{i}="0"')
        print(f'storagecontrollermaxportcount{i}="{ctl["ports"]}"')
        print(f'storagecontrollerportcount{i}="{ctl["ports"]}"')
        print(f'storagecontrollerbootable{i}="on"')
    taken = {(a["controller"], a["port"], a["device"]): a
             for a in vm.get("attachments", []) if not a.get("snapshot")}
    for ctl in vm["controllers"]:
        devices = 2 if ctl.get("type", "").upper() in ("PIIX3", "PIIX4", "ICH6") else 1
        for port in range(ctl["ports"]):
            for device in range(devices):
                att = taken.get((ctl["name"], port, device))
                key = f'"{ctl["name"]}-{port}-{device}"'
                if not att:
                    print(f'{key}="none"')
                    continue
                print(f'{key}="{att.get("location", "emptydrive")}"')
                if att.get("uuid"):
                    print(f'"{ctl["name"]}-ImageUUID-{port}-{device}"'
                          f'="{att["uuid"]}"')
    return 0


def cmd_mediumio(state: dict, args: list[str]) -> int:
    token = ""
    for arg in args:
        for flag in ("--disk=", "--dvd=", "--floppy="):
            if arg.startswith(flag):
                token = arg[len(flag):]
    medium = find(state, token)
    if medium is None:
        return fail(f"Could not find a medium matching '{token}'")
    if medium.get("encryption") == "enabled" and "--password-file=stdin" not in args:
        return fail("The medium is encrypted and no password was given"
                    " (VERR_VD_PASSWORD_INCORRECT)")
    if "--password-file=stdin" in args:
        sys.stdin.readline()
    if "formatfat" in args:
        if any(m.get("parent") == medium["uuid"] for m in state["media"]):
            return fail("Write access denied: read-only")
        medium["properties"]["Formatted"] = "FAT"
        save(state)
        return 0
    if "cat" in args:
        offset = int(opt(args, "--offset") or 0)
        size = int(opt(args, "--size") or 512)
        output = opt(args, "--output")
        try:
            with open(medium["location"], "rb") as handle:
                handle.seek(offset)
                data = handle.read(size)
        except OSError as exc:
            return fail(str(exc))
        data = data.ljust(size, b"\0")
        if output and output != "-":
            with open(output, "wb") as handle:
                handle.write(data)
            return 0
        if "--hex" in args:
            for i in range(0, len(data), 16):
                chunk = data[i:i + 16]
                hexed = " ".join(f"{b:02x}" for b in chunk)
                print(f"{offset + i:08x}: {hexed}")
        else:
            sys.stdout.buffer.write(data)
        return 0
    return fail(f"mediumio: unsupported operation in {args!r}")


def cmd_encryptmedium(state: dict, args: list[str]) -> int:
    if not args:
        return fail("encryptmedium: no medium given")
    medium = find(state, args[0])
    if medium is None:
        return fail(f"Could not find a medium matching '{args[0]}'")
    old = opt(args, "--oldpassword")
    new = opt(args, "--newpassword")

    def secret(source: str) -> str:
        if source == "stdin":
            return sys.stdin.readline().rstrip("\n")
        with open(source, encoding="utf-8") as handle:
            return handle.readline().rstrip("\n")

    if old is not None:
        if medium.get("encryption") != "enabled":
            return fail("The medium is not encrypted")
        if secret(old) != medium.get("password", ""):
            return fail("The given password is incorrect"
                        " (VBOX_E_PASSWORD_INCORRECT)")
    if new is not None:
        medium["password"] = secret(new)
        medium["encryption"] = "enabled"
        medium["cipher"] = opt(args, "--cipher") or "AES-XTS256-PLAIN64"
        medium["password_id"] = opt(args, "--newpasswordid") or ""
    else:
        medium["encryption"] = "disabled"
        for key in ("password", "cipher", "password_id"):
            medium.pop(key, None)
    save(state)
    print("0%...100%")
    return 0


def cmd_checkmediumpwd(state: dict, args: list[str]) -> int:
    if not args:
        return fail("checkmediumpwd: no medium given")
    medium = find(state, args[0])
    if medium is None:
        return fail(f"Could not find a medium matching '{args[0]}'")
    given = sys.stdin.readline().rstrip("\n")
    if medium.get("encryption") != "enabled":
        return fail("The medium is not encrypted")
    if given != medium.get("password", ""):
        return fail("The given password is incorrect")
    print("The given password is correct")
    return 0


def cmd_mediumproperty(state: dict, args: list[str]) -> int:
    rest = [a for a in args if a not in ("disk", "dvd", "floppy")]
    if len(rest) < 3:
        return fail("mediumproperty: get|set|delete <medium> <name> [<value>]")
    op, token, name = rest[0], rest[1], rest[2]
    medium = find(state, token)
    if medium is None:
        return fail(f"Could not find a medium matching '{token}'")
    props = medium.setdefault("properties", {})
    if op == "get":
        print(f"{name} = {props.get(name, '')}")
    elif op == "set":
        props[name] = rest[3] if len(rest) > 3 else ""
    elif op == "delete":
        props.pop(name, None)
    else:
        return fail(f"mediumproperty: unknown operation {op!r}")
    save(state)
    return 0


def cmd_internalcommands(state: dict, args: list[str]) -> int:
    if not args:
        return fail("internalcommands: no subcommand")
    sub, rest = args[0], args[1:]
    if sub == "repairhd":
        path = [a for a in rest if not a.startswith("-")]
        path = [p for p in path if p.upper() not in ("VDI", "VMDK", "VHD")]
        if not path or not os.path.isfile(path[-1]):
            return fail("repairhd: file not found")
        print("The VDI image is consistent, no repair needed.")
        # 7.2.12 signs off with this line whatever it found, dry run included.
        print("Corrupted VDI image repaired successfully!")
        return 0
    if sub in ("sethduuid", "sethdparentuuid"):
        if not rest:
            return fail(f"{sub}: no image given")
        path = rest[0]
        if not os.path.isfile(path):
            return fail(f"{sub}: file not found")
        given = rest[1].strip("{}") if len(rest) > 1 else str(uuidlib.uuid4())
        if sub == "sethduuid":
            # Rewriting the identity inside the file is the whole job.
            stamp_uuid(path, given)
        medium = find(state, path)
        if medium is not None:
            # Stamping a registered image desynchronises it, exactly as the
            # dialog warns: the registry keeps the old UUID and loses the file.
            medium["state"] = "inaccessible"
            medium["access_error"] = (
                f"UUID {{{medium['uuid']}}} of the medium '{path}' does not "
                f"match the value {{{given}}} stored in the media registry")
            save(state)
        print(f"UUID changed to: {given}")
        return 0
    return fail(f"internalcommands: unsupported subcommand {sub!r}")


HANDLERS = {
    "list": cmd_list,
    "showmediuminfo": cmd_showmediuminfo,
    "createmedium": cmd_createmedium,
    "closemedium": cmd_closemedium,
    "modifymedium": cmd_modifymedium,
    "clonemedium": cmd_clonemedium,
    "convertfromraw": cmd_convertfromraw,
    "storageattach": cmd_storageattach,
    "showvminfo": cmd_showvminfo,
    "mediumio": cmd_mediumio,
    "encryptmedium": cmd_encryptmedium,
    "checkmediumpwd": cmd_checkmediumpwd,
    "mediumproperty": cmd_mediumproperty,
    "internalcommands": cmd_internalcommands,
}


def main(argv: list[str]) -> int:
    if LOG_PATH:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(" ".join(argv) + "\n")
    if not argv:
        return fail("no command")
    if argv[0] in ("--version", "-v"):
        print("7.2.12r174389")
        return 0
    handler = HANDLERS.get(argv[0])
    if handler is None:
        return fail(f"unsupported subcommand {argv[0]!r} — the fake needs a "
                    "handler for it before the app can rely on it")
    return handler(load(), argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
