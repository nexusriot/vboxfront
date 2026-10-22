"""VBoxFront constants."""

from __future__ import annotations

import bz2
import gzip
import lzma


DISK_FORMATS = ["VDI", "VMDK", "VHD", "RAW"]


DISK_EXT = {"VDI": ".vdi", "VMDK": ".vmdk", "VHD": ".vhd", "RAW": ".img"}


DISK_VARIANTS = ["Standard", "Fixed"]


VMDK_VARIANTS = ["Standard", "Fixed", "Split2G"]


MEDIUM_TYPES = ["normal", "writethrough", "immutable", "shareable", "readonly", "multiattach"]


ENCRYPTION_CIPHERS = ["AES-XTS256-PLAIN64", "AES-XTS128-PLAIN64"]


PASSWORD_STDIN = "stdin"


HEX_DUMP_WARN_BYTES = 1024 * 1024


CAP_CREATE_FIXED = 0x002


CAP_CREATE_DYNAMIC = 0x004


CAP_CREATE_SPLIT2G = 0x008


CAP_DIFFERENCING = 0x010


CAP_FILE = 0x040


KINDS = ["disk", "dvd", "floppy"]


KIND_META = {
    "disk": {
        "list": "hdds",
        "title": "Hard disks",
        "attach_type": "hdd",
        "io_flag": "--disk",
        "filter": "Disk images (*.vdi *.vmdk *.vhd *.img *.raw);;All files (*)",
    },
    "dvd": {
        "list": "dvds",
        "title": "DVD images",
        "attach_type": "dvddrive",
        "io_flag": "--dvd",
        "filter": "DVD images (*.iso);;All files (*)",
    },
    "floppy": {
        "list": "floppies",
        "title": "Floppy images",
        "attach_type": "fdd",
        "io_flag": "--floppy",
        "filter": "Floppy images (*.img *.ima *.flp);;All files (*)",
    },
}


MEDIUM_KEYS = [
    "UUID", "Parent UUID", "State", "Access Error", "Description", "Type", "Auto-Reset",
    "Location", "Storage format", "Format variant", "Capacity", "Size on disk",
    "Encryption", "Cipher", "Password ID", "Property", "In use by VMs",
    "Child UUIDs",
]


MEDIUM_KEY_ORDER = {label: i for i, label in enumerate(MEDIUM_KEYS)}


MEDIUM_KEY_ORDER["Format"] = MEDIUM_KEY_ORDER["Storage format"]


COLUMNS = ["Name", "Format", "Capacity", "Size on disk", "Type", "State",
           "Encryption", "In use by", "Snapshot", "Location", "UUID"]


(COL_NAME, COL_FORMAT, COL_CAPACITY, COL_SIZE, COL_TYPE, COL_STATE,
 COL_ENCRYPTION, COL_IN_USE, COL_SNAPSHOT, COL_LOCATION, COL_UUID) = range(len(COLUMNS))


MEDIA_EXTENSIONS = {
    ".vdi": "disk", ".vmdk": "disk", ".vhd": "disk", ".vhdx": "disk",
    ".hdd": "disk", ".qcow": "disk", ".qcow2": "disk", ".qed": "disk",
    ".iso": "dvd", ".dmg": "dvd",
    ".img": "floppy", ".ima": "floppy", ".flp": "floppy",
}


RAW_EXTENSIONS = {".raw", ".bin"}


TWO_DEVICE_CONTROLLERS = {"PIIX3", "PIIX4", "ICH6", "I82078"}


TRASH_DIR_NAME = ".vboxfront-trash"


COMPRESSORS = {
    ".gz": gzip.open,
    ".tgz": gzip.open,
    ".bz2": bz2.open,
    ".xz": lzma.open,
    ".lzma": lzma.open,
}


STDIN_CHUNK = 1024 * 1024


LONG_OPERATION_SECONDS = 30


DESTRUCTIVE_FRAGMENTS = [
    "--delete", "encryptmedium", "formatfat", "sethduuid", "sethdparentuuid",
    "--resize", "--resizebyte", "--move", "--setlocation", "storageattach",
    "--existing",
]


DESTRUCTIVE_PREFIXES = ["--output="]


MIXED_TYPE = "(leave unchanged)"
