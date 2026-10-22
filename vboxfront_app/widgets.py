"""VBoxFront widgets."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
import os

from .constants import COLUMNS, COL_CAPACITY, COL_LOCATION, COL_NAME, COL_SIZE
from .domain import (
    encryption_word,
    format_mbytes,
    is_inaccessible,
    medium_details,
    medium_type_word,
    parse_capacity_mb,
    parse_filter,
    parse_in_use_entry,
    real_parent_uuid,
    record_matches,
    snapshot_word,
    space_warning,
    state_word,
    totals_line,
)
from .models import MediumRecord
from .theme import color_css, error_color, muted_color


class ElidingLabel(QLabel):
    """A label that elides to whatever width it is given.

    A plain QLabel demands the width of its full text, and a VBoxManage command
    line is far wider than a toolbar row — enough to drag the header past the
    window edge and squeeze itself and the buttons out of view. This one keeps
    the full text for the tooltip and re-elides itself whenever it is resized,
    so it can never drive the layout.
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def fullText(self) -> str:
        return self._full

    def setFullText(self, text: str):
        self._full = text
        self.setToolTip(text)
        self._elide()

    def setText(self, text: str):
        # Anything setting the text directly would otherwise desync _full and
        # lose its elision on the next resize.
        self.setFullText(text)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self):
        super().setText(self.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideMiddle, max(self.width(), 40)
        ))


class MediumItem(QTreeWidgetItem):
    """Tree item that sorts size columns numerically via UserRole data."""

    def __lt__(self, other):
        tree = self.treeWidget()
        col = tree.sortColumn() if tree else 0
        a = self.data(col, Qt.ItemDataRole.UserRole)
        b = other.data(col, Qt.ItemDataRole.UserRole)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return a < b
        return self.text(col).lower() < other.text(col).lower()


class MediaPane(QWidget):
    """One tab: a tree of media of a single kind (disk/dvd/floppy)."""

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.records: list[MediumRecord] = []
        self._by_uuid: dict[str, MediumRecord] = {}
        self._filter = ""
        self._columns_sized = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setSelectionBehavior(self.tree.SelectionBehavior.SelectRows)
        # Extended, not single: every batch action (compact, remove, detach,
        # convert) takes whatever is selected, and the sequential command queue
        # already existed to run them one after another.
        self.tree.setSelectionMode(self.tree.SelectionMode.ExtendedSelection)
        self.tree.setEditTriggers(self.tree.EditTrigger.NoEditTriggers)
        self.tree.setRootIsDecorated(kind == "disk")
        self.tree.setAllColumnsShowFocus(True)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(COL_NAME, Qt.SortOrder.AscendingOrder)
        header = self.tree.header()
        # Interactive, not ResizeToContents: the widths are the user's to set
        # (and are remembered between sessions), which ResizeToContents would
        # override on every populate. Location still takes up the slack.
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_LOCATION, QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(False)
        header.setSectionsMovable(True)
        # Eleven columns is a horizontal scroll on a laptop, and which of them
        # matter depends entirely on the job. Hidden sections ride along in
        # the header state that is already persisted, so a choice sticks.
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._show_column_menu)
        layout.addWidget(self.tree)

        self.totals = QLabel("")
        self.totals.setStyleSheet(color_css(muted_color()))
        layout.addWidget(self.totals)

        # Everything shown here is already in the record, so the pane costs no
        # command — unlike Info, which shells out and prints into the output
        # panel. Hidden by default: it is a second thing competing for width.
        self.details = QTreeWidget()
        self.details.setColumnCount(2)
        self.details.setHeaderLabels(["Field", "Value"])
        self.details.setRootIsDecorated(False)
        self.details.setAlternatingRowColors(True)
        self.details.setMaximumHeight(200)
        self.details.setVisible(False)
        #: Asked for, as opposed to on screen: a widget inside a window that
        #: has not been shown yet reports isVisible() False, which would
        #: leave the pane empty until the first repaint.
        self._details_wanted = False
        self.details.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.details)
        self.tree.currentItemChanged.connect(lambda *_: self.refresh_details())

    def populate(self, records: list[MediumRecord]):
        selected = self.selected_record()
        prev_uuid = selected.uuid if selected else None
        prev_selection = {r.uuid for r in self.selected_records()}

        self.records = records
        self._by_uuid = {r.uuid: r for r in records}
        self.tree.setSortingEnabled(False)
        self.tree.clear()

        # Diff-image chains: children hang under their parent when it is
        # present in the same listing; everything else is a top-level row. A
        # medium is never its own parent — `sethdparentuuid` will happily
        # stamp one, and treating it as a child of itself left it with no
        # root to hang from.
        children: dict[str, list[MediumRecord]] = {}
        roots: list[MediumRecord] = []
        for r in records:
            parent = real_parent_uuid(r)
            if parent and parent != r.uuid and parent in self._by_uuid:
                children.setdefault(parent, []).append(r)
            else:
                roots.append(r)

        def add(rec: MediumRecord, parent_item: QTreeWidgetItem | None, seen: set[str]):
            if rec.uuid in seen:
                return
            seen.add(rec.uuid)
            item = self._make_item(rec)
            if parent_item is None:
                self.tree.addTopLevelItem(item)
            else:
                parent_item.addChild(item)
            for child in children.get(rec.uuid, []):
                add(child, item, seen)

        seen: set[str] = set()
        for rec in roots:
            add(rec, None, seen)
        # A parent cycle (A's parent is B, B's parent is A) leaves both out of
        # `roots`, and nothing would ever draw them: the row simply vanished
        # while the totals line went on counting it. Whatever the chain looks
        # like, every medium gets a row.
        for rec in records:
            if rec.uuid not in seen:
                add(rec, None, seen)

        self.tree.setSortingEnabled(True)
        self.tree.expandAll()
        self.autosize_columns()
        self.apply_filter(self._filter)

        if prev_uuid:
            self.select_uuid(prev_uuid)
        self.refresh_details()
        # A batch operation refreshes between its steps; losing the rest of the
        # selection there would leave the user re-picking them every time.
        if len(prev_selection) > 1:
            for item in self._iter_items():
                if item.data(COL_NAME, Qt.ItemDataRole.UserRole) in prev_selection:
                    item.setSelected(True)

    def set_details_visible(self, shown: bool):
        self._details_wanted = shown
        self.details.setVisible(shown)
        if shown:
            self.refresh_details()

    def details_visible(self) -> bool:
        return self._details_wanted

    def refresh_details(self):
        """Redraw the pane for whatever row is current."""
        if not self._details_wanted:
            return
        self.details.clear()
        rec = self.selected_record()
        if rec is None:
            self.details.addTopLevelItem(
                QTreeWidgetItem(["", "Select a medium to see its details."])
            )
            return
        for label, value in medium_details(rec):
            item = QTreeWidgetItem([label, value.replace("\n", " · ")])
            item.setToolTip(1, value)
            if label in ("Access error",):
                item.setForeground(1, error_color())
            self.details.addTopLevelItem(item)
        self.details.resizeColumnToContents(0)

    def _show_column_menu(self, pos):
        """Tick the columns to show. Name has no entry: hiding every column
        leaves a tree with nothing to click, and it is the one that names the
        row."""
        menu = QMenu(self)
        header = self.tree.header()
        for col, label in enumerate(COLUMNS):
            if col == COL_NAME:
                continue
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(not header.isSectionHidden(col))
            action.toggled.connect(
                lambda shown, c=col: header.setSectionHidden(c, not shown)
            )
        menu.addSeparator()
        menu.addAction("Show all").triggered.connect(self._show_all_columns)
        menu.exec(header.mapToGlobal(pos))

    def _show_all_columns(self):
        header = self.tree.header()
        for col in range(len(COLUMNS)):
            header.setSectionHidden(col, False)

    def visible_columns(self) -> list[int]:
        return [c for c in range(len(COLUMNS))
                if not self.tree.header().isSectionHidden(c)]

    def autosize_columns(self):
        """Fit the columns to their contents, once, on the first listing.

        Only a starting point: after this the widths belong to the user, and
        `mark_columns_restored()` suppresses it entirely when a saved layout is
        being applied instead.
        """
        if self._columns_sized or not self.records:
            return
        self._columns_sized = True
        for col in range(len(COLUMNS)):
            if col != COL_LOCATION:
                self.tree.resizeColumnToContents(col)

    def mark_columns_restored(self):
        self._columns_sized = True

    def _make_item(self, rec: MediumRecord) -> MediumItem:
        cap_mb = parse_capacity_mb(rec.capacity)
        size_mb = parse_capacity_mb(rec.size_on_disk)
        in_use_names = ", ".join(
            e[0] for e in (parse_in_use_entry(x) for x in rec.in_use) if e
        )
        cells = [
            os.path.basename(rec.location) or rec.uuid,
            rec.fmt,
            format_mbytes(cap_mb) or rec.capacity,
            format_mbytes(size_mb) or rec.size_on_disk,
            medium_type_word(rec.medium_type),
            state_word(rec),
            encryption_word(rec),
            in_use_names,
            snapshot_word(rec),
            rec.location,
            rec.uuid,
        ]
        item = MediumItem(cells)
        item.setData(COL_NAME, Qt.ItemDataRole.UserRole, rec.uuid)
        if cap_mb is not None:
            item.setData(COL_CAPACITY, Qt.ItemDataRole.UserRole, cap_mb)
        if size_mb is not None:
            item.setData(COL_SIZE, Qt.ItemDataRole.UserRole, size_mb)
        tooltips = [
            rec.location,
            f"{rec.fmt} ({rec.variant})" if rec.variant else rec.fmt,
            rec.capacity,
            rec.size_on_disk,
            rec.medium_type,
            "\n".join(x for x in (rec.state, rec.access_error) if x),
            f"Password ID: {rec.password_id}" if rec.password_id else rec.encryption,
            "\n".join(rec.in_use),
            "\n".join(rec.in_use),
            rec.location,
            rec.uuid,
        ]
        for col, tip in enumerate(tooltips):
            if tip:
                item.setToolTip(col, tip)
        if is_inaccessible(rec):
            for col in range(len(COLUMNS)):
                item.setForeground(col, error_color())
        return item

    def selected_record(self) -> MediumRecord | None:
        item = self.tree.currentItem()
        if not item:
            return None
        return self._by_uuid.get(item.data(COL_NAME, Qt.ItemDataRole.UserRole) or "")

    def selected_records(self) -> list[MediumRecord]:
        """Every selected medium, in the order the tree shows them.

        Rows carry all columns (SelectRows), so the selection contains one item
        per selected *cell* column; keying on the UUID collapses that back to
        one record per row.
        """
        found: list[MediumRecord] = []
        seen: set[str] = set()
        for item in self._iter_items():
            if not item.isSelected():
                continue
            uuid = item.data(COL_NAME, Qt.ItemDataRole.UserRole) or ""
            rec = self._by_uuid.get(uuid)
            if rec and uuid not in seen:
                seen.add(uuid)
                found.append(rec)
        return found

    def select_uuid(self, uuid: str):
        for item in self._iter_items():
            if item.data(COL_NAME, Qt.ItemDataRole.UserRole) == uuid:
                self.tree.setCurrentItem(item)
                return

    def _iter_items(self):
        def walk(item):
            yield item
            for i in range(item.childCount()):
                yield from walk(item.child(i))
        for i in range(self.tree.topLevelItemCount()):
            yield from walk(self.tree.topLevelItem(i))

    def apply_filter(self, text: str):
        self._filter = text
        terms, words = parse_filter(text)

        def visit(item) -> bool:
            cells = [item.text(c) for c in range(len(COLUMNS))]
            rec = self._by_uuid.get(
                item.data(COL_NAME, Qt.ItemDataRole.UserRole) or ""
            )
            if not terms and not words:
                self_match = True
            elif rec is None:
                self_match = not terms and all(
                    any(word in cell.lower() for cell in cells) for word in words
                )
            else:
                self_match = record_matches(rec, terms, words, cells)
            child_match = False
            for i in range(item.childCount()):
                if visit(item.child(i)):
                    child_match = True
            visible = self_match or child_match
            item.setHidden(not visible)
            return visible

        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))
        self._update_totals()

    def _update_totals(self):
        shown = sum(1 for item in self._iter_items() if not item.isHidden())
        self.totals.setText(totals_line(self.records, shown))


def confirm_space(parent, path: str, needed: int) -> bool:
    """Ask before starting something the target filesystem cannot hold.

    A warning rather than a refusal: the estimate cannot know about
    compression, reflinks or a quota that is about to be raised, and being
    wrong should not stop the user.
    """
    warning = space_warning(path, needed)
    if not warning:
        return True
    ret = QMessageBox.warning(
        parent,
        "Not enough space",
        f"{warning}\n\nVBoxManage does not reserve space up front, so this "
        "normally fails part-way and leaves a partial file behind.\n\n"
        "Continue anyway?",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return ret == QMessageBox.StandardButton.Yes
