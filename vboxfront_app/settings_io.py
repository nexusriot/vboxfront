"""Export and import the app's own settings as one JSON file.

For moving to another machine, or keeping a copy before an experiment. Only
what means something away from this window is exported: window geometry and
column layout are per-screen, and the trash index names files on this
machine's disks that a different machine does not have. Command history is
optional, because it can name paths a user would rather not hand around.

Needs QtCore (QSettings) but not QtWidgets, so the CLI can use it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import time

from .constants import KINDS
from .domain import same_file
from .library_views import SavedFilterStore
from .persistence import get_settings, load_history, load_library, save_library
from .version import APP_VERSION

FORMAT = "vboxfront-settings"
VERSION = 1

SECTION_PREFERENCES = "preferences"
SECTION_LIBRARY = "library"
SECTION_FILTERS = "filters"
SECTION_FOLDERS = "folders"
SECTION_HISTORY = "history"
SECTIONS = (SECTION_PREFERENCES, SECTION_LIBRARY, SECTION_FILTERS,
            SECTION_FOLDERS, SECTION_HISTORY)
DEFAULT_SECTIONS = (SECTION_PREFERENCES, SECTION_LIBRARY, SECTION_FILTERS,
                    SECTION_FOLDERS)
SECTION_TITLES = {
    SECTION_PREFERENCES: "Preferences (VBoxManage path, image folder, trash, plan mode)",
    SECTION_LIBRARY: "Media library",
    SECTION_FILTERS: "Saved filters",
    SECTION_FOLDERS: "Remembered dialog folders",
    SECTION_HISTORY: "Command history (may name private paths)",
}

#: key -> type. The type is what QSettings has to be asked for, because the
#: INI backend hands every value back as a string.
PREFERENCE_KEYS: dict[str, type] = {
    "vboxmanage_path": str,
    "image_folder": str,
    "plan_mode": bool,
    "trash/enabled": bool,
    "trash/dir": str,
    "trash/purge_days": int,
    "trash/purge_gb": int,
    "ui/auto_refresh": bool,
    "ui/details": bool,
}


class SettingsFileError(ValueError):
    """The file is not a VBoxFront settings export, or not one this reads."""


def export_settings(sections=DEFAULT_SECTIONS) -> dict:
    settings = get_settings()
    data: dict = {
        "format": FORMAT, "version": VERSION, "app_version": APP_VERSION,
        "exported": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sections": [s for s in SECTIONS if s in sections],
    }
    if SECTION_PREFERENCES in sections:
        data[SECTION_PREFERENCES] = {
            key: settings.value(key, kind(), kind)
            for key, kind in PREFERENCE_KEYS.items() if settings.contains(key)
        }
    if SECTION_LIBRARY in sections:
        data[SECTION_LIBRARY] = {kind: load_library(kind) for kind in KINDS}
    if SECTION_FILTERS in sections:
        data[SECTION_FILTERS] = SavedFilterStore().load()
    if SECTION_FOLDERS in sections:
        settings.beginGroup("folders")
        try:
            data[SECTION_FOLDERS] = {k: settings.value(k, "", str) for k in settings.childKeys()}
        finally:
            settings.endGroup()
    if SECTION_HISTORY in sections:
        data[SECTION_HISTORY] = load_history()
    return data


def write_settings_file(path: str, sections=DEFAULT_SECTIONS) -> dict:
    data = export_settings(sections)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1)
    os.replace(tmp, path)
    return data


def read_settings_file(path: str) -> dict:
    """Parse and check an export; raises SettingsFileError with a reason."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as e:
        raise SettingsFileError(f"cannot read {path}: {e.strerror or e}") from e
    except ValueError as e:
        raise SettingsFileError(f"{path} is not JSON: {e}") from e
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise SettingsFileError(f"{path} is not a VBoxFront settings export")
    if not isinstance(data.get("version"), int) or data["version"] > VERSION:
        raise SettingsFileError(
            f"{path} was written by a newer VBoxFront (format {data.get('version')}); "
            f"this one reads format {VERSION}")
    return data


def available_sections(data: dict) -> list[str]:
    return [s for s in SECTIONS if s in data]


@dataclass
class ImportReport:
    applied: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        lines = [f"Imported: {', '.join(self.applied) or 'nothing'}."]
        return "\n".join(lines + self.notes)


def _str_map(value) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if isinstance(k, str)}


def import_settings(data: dict, sections=None, replace: bool = False) -> ImportReport:
    """Apply an export. Library, filters and folders merge unless `replace`.

    Merging is the default because importing on a machine that already has a
    library should not quietly forget it. A VBoxManage path that is not an
    executable here is skipped rather than imported: a wrong one makes every
    command fail to start, and the other machine's path usually is wrong.
    """
    sections = available_sections(data) if sections is None else [
        s for s in sections if s in data]
    settings = get_settings()
    report = ImportReport()

    if SECTION_PREFERENCES in sections:
        prefs = data.get(SECTION_PREFERENCES) or {}
        if isinstance(prefs, dict):
            for key, kind in PREFERENCE_KEYS.items():
                if key not in prefs:
                    continue
                value = prefs[key]
                if kind is bool:
                    value = value if isinstance(value, bool) else str(value).lower() == "true"
                elif kind is int:
                    try:
                        value = max(0, int(value))
                    except (TypeError, ValueError):
                        continue
                else:
                    value = str(value)
                if key == "vboxmanage_path" and value and not (
                        os.path.isfile(value) and os.access(value, os.X_OK)):
                    report.notes.append(
                        f"Kept this machine's VBoxManage setting: {value} is not an "
                        "executable here.")
                    continue
                if key in ("image_folder", "trash/dir") and value and not os.path.isdir(value):
                    report.notes.append(f"{key} points at {value}, which does not exist here.")
                settings.setValue(key, value)
            report.applied.append(SECTION_PREFERENCES)

    if SECTION_LIBRARY in sections:
        incoming = data.get(SECTION_LIBRARY) or {}
        added = missing = 0
        for kind in KINDS:
            paths = [str(p) for p in (incoming.get(kind) or []) if p]
            current = [] if replace else load_library(kind)
            for path in paths:
                if any(same_file(path, have) for have in current):
                    continue
                current.append(path)
                added += 1
                if not os.path.exists(path):
                    missing += 1
            save_library(kind, current)
        report.applied.append(SECTION_LIBRARY)
        report.notes.append(f"Library: {added} entr{'y' if added == 1 else 'ies'} added.")
        if missing:
            report.notes.append(
                f"{missing} library file(s) do not exist on this machine; they will "
                "report \"could not open\" until moved here or pruned in Settings.")

    if SECTION_FILTERS in sections:
        incoming = _str_map(data.get(SECTION_FILTERS))
        merged = {} if replace else SavedFilterStore().load()
        merged.update(incoming)
        settings.setValue(SavedFilterStore.KEY, json.dumps(merged))
        report.applied.append(SECTION_FILTERS)

    if SECTION_FOLDERS in sections:
        if replace:
            settings.remove("folders")
        for key, folder in _str_map(data.get(SECTION_FOLDERS)).items():
            if "/" not in key and folder:
                settings.setValue(f"folders/{key}", folder)
        report.applied.append(SECTION_FOLDERS)

    if SECTION_HISTORY in sections:
        incoming = [e for e in (data.get(SECTION_HISTORY) or [])
                    if isinstance(e, dict) and "command" in e]
        entries = [] if replace else load_history()
        seen = {(e.get("when"), e.get("command"), e.get("exit")) for e in entries}
        for entry in incoming:
            key = (entry.get("when"), entry.get("command"), entry.get("exit"))
            if key not in seen:
                seen.add(key)
                entries.append(entry)
        # Oldest first, as the history is kept, so the cap drops the oldest.
        entries.sort(key=lambda e: str(e.get("when", "")))
        settings.setValue("history", json.dumps(entries[-200:]))
        report.applied.append(SECTION_HISTORY)

    settings.sync()
    return report
