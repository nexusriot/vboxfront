"""VBoxFront models."""

from __future__ import annotations

from dataclasses import dataclass, field
import os

from .constants import CAP_CREATE_DYNAMIC, CAP_CREATE_FIXED, CAP_CREATE_SPLIT2G, CAP_FILE


@dataclass
class MediumRecord:
    uuid: str
    location: str
    fmt: str
    capacity: str
    size_on_disk: str
    state: str = ""
    access_error: str = ""
    medium_type: str = ""
    parent_uuid: str = ""
    variant: str = ""
    encryption: str = ""
    cipher: str = ""
    password_id: str = ""
    auto_reset: str = ""
    description: str = ""
    in_use: list[str] = field(default_factory=list)
    properties: list[str] = field(default_factory=list)


@dataclass
class MediumBackend:
    """One entry of `VBoxManage list hddbackends`."""

    id: str
    capabilities: int = 0
    extensions: list[tuple[str, str]] = field(default_factory=list)
    properties: list[dict[str, str]] = field(default_factory=list)

    def can_create(self) -> bool:
        return bool(self.capabilities & (CAP_CREATE_DYNAMIC | CAP_CREATE_FIXED)
                    and self.capabilities & CAP_FILE)

    def extension_for(self, device: str = "HardDisk") -> str:
        for ext, dev in self.extensions:
            if dev == device:
                return "." + ext
        return ""

    def variants(self) -> list[str]:
        names = []
        if self.capabilities & CAP_CREATE_DYNAMIC:
            names.append("Standard")
        if self.capabilities & CAP_CREATE_FIXED:
            names.append("Fixed")
        if self.capabilities & CAP_CREATE_SPLIT2G:
            names.append("Split2G")
        return names or ["Standard"]


@dataclass
class TrashedFile:
    """One backing file moved aside by a delete, and where it came from."""

    kind: str
    original: str
    trashed: str


@dataclass
class HealthFinding:
    """One thing wrong with a medium, in a form a report can print."""

    kind: str
    uuid: str
    location: str
    level: str  # "error" or "warning"
    problem: str
    hint: str = ""
    #: What kind of problem this is, for the fixes the dialog can offer.
    #: Matching on the prose would break the moment the wording changed.
    code: str = ""

    def line(self) -> str:
        name = os.path.basename(self.location) or self.uuid
        tail = f" — {self.hint}" if self.hint else ""
        return f"[{self.level}] {name}: {self.problem}{tail}"
