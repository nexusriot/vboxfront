"""VBoxFront persistence."""

from __future__ import annotations

from PyQt6.QtCore import QDateTime, QSettings, Qt
import json
import os
import re
import shutil

from .constants import TRASH_DIR_NAME
from .domain import export_rows, same_file
from .models import MediumRecord, TrashedFile


def get_settings() -> QSettings:
    return QSettings("vboxfront", "vboxfront")


def vboxmanage_path() -> str | None:
    custom = get_settings().value("vboxmanage_path", "", str)
    if custom:
        return custom
    return shutil.which("VBoxManage") or shutil.which("vboxmanage")


def vboxmanage_problem() -> str:
    """Why VBoxManage cannot be run, or "" when it looks usable.

    A configured path is honoured even when it is wrong — silently falling back
    to PATH would hide the user's own setting — so the check has to look at it
    rather than just at whether *something* was found. Without this a bogus
    Settings entry passed the startup check and every command afterwards died
    with a bare "Failed to start".
    """
    custom = get_settings().value("vboxmanage_path", "", str)
    path = vboxmanage_path()
    if not path:
        return (
            "Could not find `VBoxManage` in PATH. Install VirtualBox, add it to "
            "your PATH, or set its location in File → Settings."
        )
    if custom and not (os.path.isfile(custom) and os.access(custom, os.X_OK)):
        return (
            f"The VBoxManage location set in File → Settings is not an "
            f"executable file:\n\n{custom}\n\nEvery command will fail to "
            "start until it is corrected or cleared."
        )
    return ""


def _as_str_list(value) -> list[str]:
    # QSettings returns None for missing keys and a bare str for 1-item lists.
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [str(v) for v in value]


def load_library(kind: str) -> list[str]:
    return _as_str_list(get_settings().value(f"library/{kind}"))


def save_library(kind: str, paths: list[str]) -> None:
    get_settings().setValue(f"library/{kind}", paths)


def add_library_path(kind: str, path: str) -> None:
    # Matched by file, not spelling: VBoxManage may report a Location with
    # symlinks or ".." resolved differently from the path that was stored.
    paths = load_library(kind)
    if not any(same_file(p, path) for p in paths):
        paths.append(path)
        save_library(kind, paths)


def remove_library_path(kind: str, path: str) -> bool:
    """Drop every entry naming the same file as `path`; True if any went."""
    paths = load_library(kind)
    kept = [p for p in paths if not same_file(p, path)]
    if len(kept) == len(paths):
        return False
    save_library(kind, kept)
    return True


def plan_mode() -> bool:
    """Whether a batch shows its commands instead of running them straight."""
    return get_settings().value("plan_mode", False, bool)


def set_plan_mode(on: bool) -> None:
    get_settings().setValue("plan_mode", bool(on))


def trash_enabled() -> bool:
    """Whether a delete moves the file aside instead of unlinking it."""
    return get_settings().value("trash/enabled", True, bool)


def trash_root() -> str:
    """The configured trash folder, or "" while it follows each image."""
    return get_settings().value("trash/dir", "", str)


def trash_purge_days() -> int:
    """Purge trashed files older than this many days; 0 never does."""
    return max(0, get_settings().value("trash/purge_days", 0, int))


def trash_purge_gb() -> int:
    """Keep the trash under this many GB, oldest out first; 0 for no cap."""
    return max(0, get_settings().value("trash/purge_gb", 0, int))


def user_trash_dir() -> str:
    """Fallback trash for images whose own folder cannot be written."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return os.path.join(base, "vboxfront", "trash")


def trash_dir_for(path: str) -> str:
    """Where deleting `path` would put it.

    A configured folder wins. Otherwise the image's own folder, which keeps
    the move a rename inside one filesystem — instant whatever the image
    weighs, where a copy of a 200 GB disk is not. The per-user fallback is for
    the one case where that cannot work at all: a folder nothing may write to.
    """
    configured = trash_root()
    if configured:
        return configured
    parent = os.path.dirname(os.path.abspath(path))
    if parent and os.access(parent, os.W_OK):
        return os.path.join(parent, TRASH_DIR_NAME)
    return user_trash_dir()


def unique_in(directory: str, name: str) -> str:
    """A path in `directory` that nothing occupies yet.

    The trash is the one place a collision must not overwrite: two disks both
    called `disk.vdi` in different folders land here together, and the second
    delete must not erase what the first one saved.
    """
    candidate = os.path.join(directory, name)
    if not os.path.lexists(candidate):
        return candidate
    stem, ext = os.path.splitext(name)
    counter = 1
    while True:
        candidate = os.path.join(directory, f"{stem}.{counter}{ext}")
        if not os.path.lexists(candidate):
            return candidate
        counter += 1


def move_to_trash(path: str) -> str:
    """Move `path` into its trash folder; returns where it landed.

    `os.replace` first because that is the whole point — a rename inside one
    filesystem costs nothing — with `shutil.move`'s copy as the fallback for a
    configured trash that turned out to be on another filesystem.
    """
    directory = trash_dir_for(path)
    os.makedirs(directory, exist_ok=True)
    dest = unique_in(directory, os.path.basename(path))
    try:
        os.replace(path, dest)
    except OSError:
        shutil.move(path, dest)
    return dest


def restore_trashed(entry: TrashedFile) -> None:
    """Put a trashed file back where it came from, or raise OSError saying why.

    Refuses to write over whatever is at the original path now: an undo that
    destroyed a file created since the delete would be the same mistake this
    whole path exists to undo.
    """
    if os.path.lexists(entry.original):
        raise FileExistsError(f"{entry.original} exists again")
    parent = os.path.dirname(entry.original)
    if parent and not os.path.isdir(parent):
        raise FileNotFoundError(f"{parent} is gone")
    try:
        os.replace(entry.trashed, entry.original)
    except OSError:
        shutil.move(entry.trashed, entry.original)


def image_folder() -> str:
    """Where a new image should be offered, best guess first.

    `~/new-disk.vdi` is almost never right. The order is: what the user set in
    Settings, then VirtualBox's own default machine folder (read once per
    session from `list systemproperties` and cached), then the home directory
    as the last resort.
    """
    for candidate in (get_settings().value("image_folder", "", str),
                      get_settings().value("vbox/machinefolder", "", str)):
        if candidate and os.path.isdir(candidate):
            return candidate
    return os.path.expanduser("~")


def remembered_folder(key: str) -> str:
    """The folder this kind of dialog last landed in, else `image_folder()`.

    Keyed per purpose rather than globally: extracting a byte range and
    creating a disk are not the same errand, and one should not drag the
    other's folder around.
    """
    stored = get_settings().value(f"folders/{key}", "", str)
    if stored and os.path.isdir(stored):
        return stored
    return image_folder()


def remember_folder(key: str, path: str) -> None:
    """Record where a dialog ended up, given the file or folder it chose."""
    if not path:
        return
    folder = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
    if folder and os.path.isdir(folder):
        get_settings().setValue(f"folders/{key}", folder)


def default_image_path(key: str, name: str) -> str:
    """A full starting path for a "save a new image" dialog."""
    return os.path.join(remembered_folder(key), name)


def snapshot_dir() -> str:
    """Where pre-batch registry snapshots are kept."""
    return os.path.join(os.path.dirname(user_trash_dir()), "snapshots")


def write_registry_snapshot(media: dict[str, list[MediumRecord]],
                            reason: str) -> str:
    """Serialise the whole registry before something destructive runs.

    The export machinery already turns records into rows; this writes them
    somewhere automatic and timestamped. It cannot undo anything — the trash
    does that for files — but it is the difference between "something went
    wrong" and "here is exactly what the registry looked like beforehand",
    including the UUIDs, which is what a rebuild needs. Returns the path, or
    "" when it could not be written (never a reason to stop the operation).
    """
    stamp = QDateTime.currentDateTime().toString("yyyyMMdd-HHmmss-zzz")
    import uuid
    stamp += "-" + uuid.uuid4().hex[:8]
    safe = re.sub(r"[^a-z0-9]+", "-", reason.lower()).strip("-") or "batch"
    path = os.path.join(snapshot_dir(), f"{stamp}-{safe}.json")
    payload = {
        "when": QDateTime.currentDateTime().toString(Qt.DateFormat.ISODate),
        "reason": reason,
        "media": {kind: export_rows(records) for kind, records in media.items()},
    }
    try:
        os.makedirs(snapshot_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1)
    except OSError:
        return ""
    return path


def load_history() -> list[dict]:
    """Past commands, newest last. Stored as one JSON blob, not a QSettings
    list: the INI backend splits string lists on commas, which a command line
    can legitimately contain."""
    raw = get_settings().value("history", "", str)
    try:
        entries = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [e for e in entries if isinstance(e, dict) and "command" in e]


def append_history(command: str, code: int, when: str, limit: int = 200) -> None:
    entries = load_history()
    entries.append({"command": command, "exit": code, "when": when})
    get_settings().setValue("history", json.dumps(entries[-limit:]))


def clear_history() -> None:
    get_settings().remove("history")
