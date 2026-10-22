"""Batch outcomes independent of Qt and the command process."""
from dataclasses import dataclass


@dataclass
class BatchItem:
    entry: tuple
    state: str = 'waiting'
    code: int | None = None

    @property
    def args(self):
        return self.entry[0]


class BatchReport:
    def __init__(self, entries, title=''):
        self.title = title
        self.items = [BatchItem(e) for e in entries]

    def start(self):
        item = next(i for i in self.items if i.state == 'waiting')
        item.state = 'running'
        return item

    def skip_waiting(self):
        for i in self.items:
            if i.state == 'waiting':
                i.state = 'skipped'

    def summary(self):
        return ', '.join(f'{sum(i.state == state for i in self.items)} {state}'
                         for state in ('succeeded', 'failed', 'skipped'))
