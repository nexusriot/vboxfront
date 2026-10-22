"""Durable output replacement and failed-operation metadata.

Only file-producing operations are journalled. Passwords and stdin payloads
are never stored. Backups stay beside the output so renames stay on one disk.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import tempfile
import uuid

from PyQt6.QtCore import QLockFile


@dataclass
class Operation:
    id: str
    args: list[str]
    target: str
    backup: str = ""
    partial: str = ""
    state: str = "prepared"
    error: str = ""
    when: str = ""
    source_archive: str = ""
    size: int = 0
    partial_stamp: list = field(default_factory=list)

    def __post_init__(self):
        if not isinstance(self.id, str) or uuid.UUID(self.id).hex != self.id:
            raise ValueError('Invalid operation ID')
        if not isinstance(self.args, list) or not all(isinstance(a, str) for a in self.args):
            raise ValueError('Invalid operation arguments')
        if not all(isinstance(p, str) for p in (self.target, self.backup, self.partial, self.source_archive, self.state, self.error, self.when)):
            raise ValueError('Invalid operation paths')
        if not os.path.isabs(self.target) or not isinstance(self.size, int) or self.size < 0:
            raise ValueError('Invalid operation target or size')


class OperationJournal:
    def __init__(self, path: str):
        self.path = Path(path)
        self.directory = Path(str(path) + '.d')
        self.operations: list[Operation] = []
        self._locks = {}
        if self.directory.exists():
            self.operations = [Operation(**json.loads(p.read_text()))
                               for p in sorted(self.directory.glob('*.json'))]
        elif self.path.exists():
            # Import the initial journal layout without discarding recovery data.
            self.operations = [Operation(**r) for r in json.loads(self.path.read_text())]
            self.save()
        for op in self.operations:
            if op.state in ('running', 'prepared'):
                try:
                    lease = self._acquire(op.target)
                except OSError:
                    continue  # Another application instance still owns this output.
                op.state = 'interrupted'
                op.error = 'Operation interrupted; inspect files before retrying'
                if not op.backup and os.path.lexists(op.target):
                    self._set_partial(op, op.target)
                self.save(op)
                lease.unlock()

    def _acquire(self, target):
        self.directory.mkdir(parents=True, exist_ok=True)
        identity = os.path.join(os.path.realpath(os.path.dirname(target)), os.path.basename(target))
        digest = hashlib.sha256(os.fsencode(identity)).hexdigest()
        lease = QLockFile(str(self.directory / (digest + ".lock")))
        lease.setStaleLockTime(0)
        if not lease.tryLock(0):
            raise OSError("Another operation is using this target")
        return lease

    def close(self):
        for lease in self._locks.values():
            lease.unlock()
        self._locks.clear()

    @staticmethod
    def _stamp(path):
        s = os.lstat(path)
        return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]

    def _set_partial(self, op, path):
        op.partial = path
        op.partial_stamp = self._stamp(path)

    def save(self, op=None):
        # Each operation has its own atomic file, so two application instances
        # updating different operations cannot erase one another's records.
        self.directory.mkdir(parents=True, exist_ok=True)
        for entry in ([op] if op is not None else self.operations):
            fd, name = tempfile.mkstemp(prefix='.operation-', dir=self.directory)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                    json.dump(asdict(entry), handle, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(name, self.directory / (entry.id + '.json'))
            finally:
                if os.path.exists(name):
                    os.unlink(name)

    def begin(self, args: list[str], target: str, overwrite: bool = False,
              source_archive: str = "", size: int = 0) -> Operation:
        target = os.path.abspath(target)
        if os.path.lexists(target) and not overwrite:
            raise FileExistsError(target)
        if os.path.isdir(target):
            raise IsADirectoryError(target)
        lease = self._acquire(target)
        if (os.path.lexists(target) and not overwrite) or os.path.isdir(target):
            lease.unlock()
            raise FileExistsError(target)
        ident = uuid.uuid4().hex
        backup = target + '.vboxfront-backup-' + ident if os.path.lexists(target) else ''
        op = Operation(ident, list(args), target, backup=backup,
                       when=datetime.now(timezone.utc).isoformat(),
                       source_archive=source_archive, size=size)
        self.operations.append(op)
        self._locks[op.id] = lease
        # Persist intent before moving the old output, including recovery paths.
        try:
            self.save(op)
            if backup:
                os.replace(target, backup)
            op.state = 'running'
            self.save(op)
        except OSError:
            # The command has not run; never leave an original stranded.
            if backup and os.path.lexists(backup) and not os.path.lexists(target):
                os.replace(backup, target)
            op.state = 'failed'
            self._locks.pop(op.id).unlock()
            self.save(op)
            raise
        return op

    def finish(self, op: Operation, code: int):
        try:
            return self._finish(op, code)
        finally:
            lease = self._locks.pop(op.id, None)
            if lease is not None:
                lease.unlock()

    def _finish(self, op: Operation, code: int):
        if code == 0 and not os.path.isfile(op.target):
            code = 1
            op.error = 'Command produced no output file'
        if code == 0:
            op.state = 'succeeded'
            # Persist success before removing a backup. On a crash between the
            # two, the recovery manager can show the leftover backup.
            self.save(op)
            if op.backup and os.path.lexists(op.backup):
                try:
                    os.unlink(op.backup)
                except OSError as e:
                    op.error = f'Output succeeded; backup cleanup failed: {e}'
        else:
            op.state = 'failed'
            op.error = op.error or f'Command failed (exit {code})'
            self.restore(op)
        self.save(op)
        if code == 0:
            for previous in self.operations:
                if (previous is not op and previous.target == op.target
                        and previous.args == op.args and previous.state != 'succeeded'
                        and not any(p and os.path.lexists(p) for p in (previous.backup, previous.partial))):
                    previous.state = 'succeeded'
                    previous.error = 'Retried successfully'
                    self.save(previous)
        return code

    def restore(self, op: Operation):
        """Preserve partial output, then restore a previous output if available."""
        lease = None if op.id in self._locks else self._acquire(op.target)
        try:
            if op.backup and os.path.lexists(op.backup):
                if os.path.lexists(op.target):
                    partial = op.target + '.vboxfront-partial-' + uuid.uuid4().hex
                    op.partial = partial
                    self.save(op)
                    os.replace(op.target, partial)
                    self._set_partial(op, partial)
                os.replace(op.backup, op.target)
            elif not op.backup and os.path.lexists(op.target):
                self._set_partial(op, op.target)
            op.state = 'failed'
            self.save(op)
        finally:
            if lease is not None:
                lease.unlock()

    def unresolved(self):
        return [o for o in self.operations if o.state != 'succeeded'
                or (o.backup and os.path.lexists(o.backup))]

    def remove_partial(self, op: Operation):
        if op.partial and os.path.lexists(op.partial):
            if not op.partial_stamp or self._stamp(op.partial) != op.partial_stamp:
                raise OSError('Partial output changed since the failure; inspect it before removing it manually')
            if not os.path.isfile(op.partial) and not os.path.islink(op.partial):
                raise IsADirectoryError(op.partial)
            os.unlink(op.partial)
        op.partial = ''
        op.partial_stamp = []
        self.save(op)

    def forget(self, op: Operation):
        if any(path and os.path.lexists(path) for path in (op.backup, op.partial)):
            raise ValueError('Restore or remove the remaining files first')
        record = self.directory / (op.id + '.json')
        if record.exists():
            record.unlink()
        self.operations.remove(op)
