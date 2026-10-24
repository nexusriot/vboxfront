"""VBoxFront commands."""

from __future__ import annotations

import os
import shlex
import tempfile

from .constants import KIND_META, PASSWORD_STDIN
from .persistence import vboxmanage_path


def plan_script(commands: list[list[str]]) -> str:
    """The commands of a batch as a runnable shell script.

    Quoted the same way the output panel quotes a command line, so what is
    copied is what would have run — the point of the whole "every action is a
    visible VBoxManage command" arrangement, made portable.
    """
    vbm = vboxmanage_path() or "VBoxManage"
    lines = ["#!/bin/sh", "set -e", ""]
    lines += [" ".join(shlex.quote(a) for a in [vbm, *args]) for args in commands]
    return "\n".join(lines) + "\n"


def list_media_args(kind: str) -> list[str]:
    return ["list", "-l", KIND_META[kind]["list"]]


def create_disk_args(path: str, size_mb: int, fmt: str, variant: str) -> list[str]:
    args = ["createmedium", "disk", "--filename", path, "--size", str(size_mb), "--format", fmt]
    if variant and variant != "Standard":
        args += ["--variant", variant]
    return args


def attach_args(vm: str, controller: str, port: int, device: int, kind: str, medium: str,
                mtype: str = "", discard: bool = False, nonrotational: bool = False,
                hotpluggable: bool = False, passthrough: bool = False,
                tempeject: bool = False, forceunmount: bool = False) -> list[str]:
    """Every optional flag is omitted unless asked for, so the echoed command
    stays the shortest one that does the job.

    `medium` is passed through verbatim: besides a UUID or filename it can be
    one of VBoxManage's own placeholders — `emptydrive`, `additions`, or
    `host:<drive>` for a physical optical/floppy drive.
    """
    args = [
        "storageattach", vm,
        "--storagectl", controller,
        "--port", str(port),
        "--device", str(device),
        "--type", KIND_META[kind]["attach_type"],
        "--medium", medium,
    ]
    if mtype:
        args += ["--mtype", mtype]
    if discard:
        args += ["--discard", "on"]
    if nonrotational:
        args += ["--nonrotational", "on"]
    if hotpluggable:
        args += ["--hotpluggable", "on"]
    if passthrough:
        args += ["--passthrough", "on"]
    if tempeject:
        args += ["--tempeject", "on"]
    if forceunmount:
        args.append("--forceunmount")
    return args


def clone_args(kind: str, source: str, target: str, fmt: str = "",
               variant: str = "", existing: bool = False) -> list[str]:
    """`clonemedium`, with the format/variant/--existing knobs the CLI takes.

    `--existing` clones *into* a target that was created up front (keeping its
    size and variant), which is the only way to clone into a pre-allocated
    fixed image — everywhere else clonemedium refuses to touch a file that is
    already there.
    """
    args = ["clonemedium", kind, source, target]
    if existing:
        args.append("--existing")
    if fmt:
        args += ["--format", fmt]
    if variant and variant != "Standard":
        args += ["--variant", variant]
    return args


def mediumproperty_args(kind: str, medium: str, op: str, name: str,
                        value: str = "") -> list[str]:
    """`mediumproperty <kind> get|set|delete <medium> <name> [<value>]`."""
    args = ["mediumproperty", kind, op, medium, name]
    if op == "set":
        args.append(value)
    return args


def detach_args(vm: str, controller: str, port: int, device: int) -> list[str]:
    return [
        "storageattach", vm,
        "--storagectl", controller,
        "--port", str(port),
        "--device", str(device),
        "--medium", "none",
    ]


def properties_args(kind: str, uuid: str, new_type: str | None, new_description: str | None) -> list[str] | None:
    args = ["modifymedium", kind, uuid]
    if new_type:
        args += ["--type", new_type]
    if new_description is not None:
        args += ["--description", new_description]
    return args if len(args) > 3 else None


def resize_args(uuid: str, size_mb: int | None = None,
                size_bytes: int | None = None) -> list[str]:
    """Grow a disk. `--resizebyte` when an exact size was asked for.

    `--resize` only speaks megabytes, so anything that is not a whole number of
    them has to go through --resizebyte or be silently rounded.
    """
    args = ["modifymedium", "disk", uuid]
    if size_bytes is not None:
        return args + ["--resizebyte", str(size_bytes)]
    return args + ["--resize", str(size_mb or 0)]


def move_args(uuid: str, new_path: str) -> list[str]:
    return ["modifymedium", "disk", uuid, "--move", new_path]


def encrypt_args(uuid: str, password: str, password_id: str, cipher: str) -> tuple[list[str], bytes]:
    # The password goes through stdin so it never appears in the process list
    # or the echoed command line. See PASSWORD_STDIN for why not "-".
    args = ["encryptmedium", uuid, "--newpassword", PASSWORD_STDIN,
            "--newpasswordid", password_id, "--cipher", cipher]
    return args, (password + "\n").encode()


def decrypt_args(uuid: str, password: str) -> tuple[list[str], bytes]:
    return ["encryptmedium", uuid, "--oldpassword", PASSWORD_STDIN], (password + "\n").encode()


def check_password_args(uuid: str, password: str) -> tuple[list[str], bytes]:
    return ["checkmediumpwd", uuid, PASSWORD_STDIN], (password + "\n").encode()


def mediumio_args(kind: str, medium: str, password: str = "") -> tuple[list[str], bytes | None]:
    """Common prefix of every `mediumio` call, plus its stdin payload.

    `--password-file=-` is the same console trap as elsewhere (it disables
    terminal echo and dies on a pipe), but the literal name `stdin` works, so
    an encrypted medium needs no temp file here.
    """
    args = ["mediumio", f"{KIND_META[kind]['io_flag']}={medium}"]
    if password:
        args.append(f"--password-file={PASSWORD_STDIN}")
        return args, (password + "\n").encode()
    return args, None


def mediumio_cat_args(kind: str, medium: str, offset: int = 0, size: int | None = None,
                      hex_dump: bool = True, output: str = "",
                      password: str = "") -> tuple[list[str], bytes | None]:
    """Read a byte range straight out of an image — no VM, no mount, no root."""
    args, stdin_data = mediumio_args(kind, medium, password)
    args.append("cat")
    if hex_dump:
        args.append("--hex")
    if offset:
        args.append(f"--offset={offset}")
    if size is not None:
        args.append(f"--size={size}")
    if output:
        args.append(f"--output={output}")
    return args, stdin_data


def mediumio_formatfat_args(kind: str, medium: str, quick: bool = True,
                            password: str = "") -> tuple[list[str], bytes | None]:
    args, stdin_data = mediumio_args(kind, medium, password)
    args.append("formatfat")
    if quick:
        args.append("--quick")
    return args, stdin_data


def convertfromraw_stdin_args(target: str, size_bytes: int,
                              fmt: str = "VDI", variant: str = "") -> list[str]:
    """`convertfromraw stdin <target> <bytes>` — import without unpacking.

    VBoxManage reads the image off standard input, so a compressed archive can
    be decompressed straight into it and never exists as a file. The byte
    count is mandatory and is the *uncompressed* length: it is how the backend
    sizes the target before a single sector arrives.
    """
    args = ["convertfromraw", "stdin", target, str(size_bytes)]
    if fmt:
        args.append(f"--format={fmt}")
    if variant and variant != "Standard":
        args.append(f"--variant={variant}")
    return args


def export_raw_args(uuid: str, target: str) -> list[str]:
    """`clonemedium disk <uuid> <target> --format RAW` — a flat image out.

    RAW is missing from every format combo because `disk_backends()` drops it:
    its backend registers no HardDisk extension, so it can never be a managed
    target. It is still a perfectly good *clone* target, and the flat image is
    what feeds dd, a USB stick or qemu — the one direction the app could not
    go until this existed.
    """
    return ["clonemedium", "disk", uuid, target, "--format", "RAW"]


def create_diff_args(path: str, parent: str, fmt: str = "") -> list[str]:
    """A differencing child of `parent`; its size is inherited, never given."""
    args = ["createmedium", "disk", "--filename", path, "--diffparent", parent]
    if fmt:
        args += ["--format", fmt]
    return args


def create_floppy_args(path: str, size_mb: int, formatted: bool = True) -> list[str]:
    """`--variant Formatted` gives a FAT-formatted image straight away.

    Only floppies: VDI rejects the variant with VERR_FILE_NOT_FOUND, so a disk
    has to be formatted afterwards with `mediumio formatfat` instead.
    """
    args = ["createmedium", "floppy", "--filename", path, "--size", str(size_mb)]
    if formatted:
        args += ["--variant", "Formatted"]
    return args


def autoreset_args(uuid: str, enabled: bool) -> list[str]:
    return ["modifymedium", "disk", uuid, "--autoreset", "on" if enabled else "off"]


def setlocation_args(kind: str, uuid: str, path: str) -> list[str]:
    """Re-point a registry entry at a file that moved outside the app."""
    return ["modifymedium", kind, uuid, "--setlocation", path]


def properties_undo_args(kind: str, medium, new_type: str | None,
                         new_description: str | None,
                         new_autoreset: bool | None) -> list[list[str]]:
    """The calls that put back what a Properties edit is about to change.

    Built from the listing *before* the edit, and only for what the edit
    actually changes. Reverse order: auto-reset only applies to an immutable
    disk, so it has to be restored while the disk is still immutable.
    """
    commands: list[list[str]] = []
    old_reset = medium.auto_reset.lower() == "on"
    if new_autoreset is not None and kind == "disk" and new_autoreset != old_reset:
        commands.append(autoreset_args(medium.uuid, old_reset))
    old_type = medium.medium_type.split(" ")[0] if medium.medium_type else ""
    restore_type = old_type if new_type and old_type and new_type != old_type else None
    restore_desc = (medium.description
                    if new_description is not None and new_description != medium.description
                    else None)
    edit = properties_args(kind, medium.uuid, restore_type, restore_desc)
    if edit:
        commands.append(edit)
    return commands


def repairhd_args(path: str, fmt: str = "", dry_run: bool = False) -> list[str]:
    args = ["internalcommands", "repairhd"]
    if dry_run:
        args.append("-dry-run")
    if fmt:
        args += ["-format", fmt.upper()]
    return args + [path]


def sethduuid_args(path: str, uuid: str = "") -> list[str]:
    """Stamp a fresh UUID into an image file so a copy of it can be registered."""
    return ["internalcommands", "sethduuid", path] + ([uuid] if uuid else [])


def sethdparentuuid_args(path: str, parent_uuid: str) -> list[str]:
    return ["internalcommands", "sethdparentuuid", path, parent_uuid]


def change_password_args(uuid: str, old_file: str, new_file: str,
                         password_id: str, cipher: str) -> list[str]:
    """Re-key an encrypted medium in one call, reading both secrets from files.

    Two secrets cannot share the stdin stream: the first read consumes the
    whole buffer, so the second one comes back wrong (VBOX_E_PASSWORD_INCORRECT
    on 7.2.12). Files are the only way to hand over both.
    """
    return ["encryptmedium", uuid,
            "--oldpassword", old_file,
            "--newpassword", new_file,
            "--newpasswordid", password_id,
            "--cipher", cipher]


def write_secret_file(secret: str) -> str:
    """Spill one password to a private temp file, for the two-secret call above.

    mkstemp creates it 0600 and the caller removes it as soon as the command
    exits, so it is never readable by anyone else and never outlives the run.
    """
    fd, path = tempfile.mkstemp(prefix="vboxfront-pw-")
    with os.fdopen(fd, "w") as handle:
        handle.write(secret + "\n")
    return path


def rerun_args(command: str) -> list[str] | None:
    """The argv to replay, with the leading VBoxManage path dropped."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens:
        return None
    if os.path.basename(tokens[0]).lower().startswith("vboxmanage"):
        tokens = tokens[1:]
    return tokens or None
