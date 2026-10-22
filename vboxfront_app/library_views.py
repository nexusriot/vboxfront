"""Saved media filters and VM controller/slot presentation models."""
import json
from dataclasses import dataclass
from .persistence import get_settings
from .domain import vm_storage_controllers, controller_types, max_device_index, slot_occupant


class SavedFilterStore:
    KEY = 'filters/saved'

    def load(self):
        try:
            rows = json.loads(get_settings().value(self.KEY, '{}', str))
            return {k: v for k, v in rows.items() if isinstance(k, str) and isinstance(v, str)}
        except (ValueError, AttributeError, TypeError):
            return {}

    def save(self, name, expression):
        name = name.strip()
        if not name:
            raise ValueError('A filter needs a name')
        rows = self.load()
        rows[name] = expression
        get_settings().setValue(self.KEY, json.dumps(rows))

    def delete(self, name):
        rows = self.load()
        rows.pop(name, None)
        get_settings().setValue(self.KEY, json.dumps(rows))


@dataclass(frozen=True)
class StorageSlot:
    vm: str
    vm_name: str
    controller: str
    port: int
    device: int
    medium: str


def storage_slots(vm, name, info):
    types = controller_types(info)
    return [StorageSlot(vm, name, ctl, port, device, slot_occupant(info, ctl, port, device))
            for ctl, ports in vm_storage_controllers(info)
            for port in range(ports)
            for device in range(max_device_index(types.get(ctl, '')) + 1)]
