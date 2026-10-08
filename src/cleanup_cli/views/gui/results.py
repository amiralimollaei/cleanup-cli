"""Virtualized GTK result rows backed by lightweight model items."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import gi  # pyright: ignore[reportMissingImports]

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GObject, Gtk, Pango  # noqa: E402  # pyright: ignore[reportMissingImports, reportAttributeAccessIssue]


@dataclass(frozen=True, slots=True)
class ResultRow:
    """The display data for a result, independent of its GTK widgets."""

    icon_name: str
    primary: str
    secondary: str


class ResultItem(GObject.Object):
    """A result's lightweight object in the GTK list model."""

    def __init__(self, row: ResultRow) -> None:
        super().__init__()
        self.row = row


class ResultList:
    """Keep all result data while creating widgets only for visible rows."""

    def __init__(self) -> None:
        self.model = Gio.ListStore.new(ResultItem)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", _setup_row)
        factory.connect("bind", _bind_row)
        factory.connect("unbind", _unbind_row)
        selection = Gtk.NoSelection.new(self.model)
        self.widget = Gtk.ListView.new(selection, factory)
        self.widget.set_show_separators(True)
        self.widget.set_hexpand(True)
        self.widget.set_vexpand(True)

    def append_rows(self, rows: Sequence[ResultRow]) -> None:
        """Append a batch with one model notification and no eager widgets."""

        if rows:
            self.model.splice(
                self.model.get_n_items(), 0, [ResultItem(row) for row in rows]
            )

    def clear(self) -> None:
        """Remove every result with one model notification."""

        self.model.remove_all()

    def close(self) -> None:
        """Release native row callbacks before Python begins finalization."""

        self.widget.set_model(None)
        self.widget.set_factory(None)


class _ResultRowWidget(Gtk.Box):
    """Widgets reused by the list factory as results enter and leave view."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.set_margin_top(10)
        self.set_margin_bottom(10)
        self.set_margin_start(12)
        self.set_margin_end(12)

        self._icon = Gtk.Image(pixel_size=24)
        self.append(self._icon)

        labels = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True
        )
        self._primary = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self._primary.add_css_class("heading")
        self._secondary = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self._secondary.add_css_class("dim-label")
        labels.append(self._primary)
        labels.append(self._secondary)
        self.append(labels)

    def bind(self, row: ResultRow) -> None:
        """Replace all displayed data when this widget is assigned a row."""

        self._icon.set_from_icon_name(row.icon_name)
        self._primary.set_text(row.primary)
        self._primary.set_tooltip_text(row.primary)
        self._secondary.set_text(row.secondary)
        self._secondary.set_tooltip_text(row.secondary)

    def clear(self) -> None:
        """Discard old row content before this widget is reused."""

        self._icon.clear()
        self._primary.set_text("")
        self._primary.set_tooltip_text(None)
        self._secondary.set_text("")
        self._secondary.set_tooltip_text(None)


def _setup_row(_factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
    item.set_child(_ResultRowWidget())


def _bind_row(_factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
    child = item.get_child()
    result = item.get_item()
    if isinstance(child, _ResultRowWidget):
        if isinstance(result, ResultItem):
            child.bind(result.row)
        else:
            child.clear()


def _unbind_row(_factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
    child = item.get_child()
    if isinstance(child, _ResultRowWidget):
        child.clear()
