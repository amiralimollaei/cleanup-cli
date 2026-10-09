"""Responsive workspace panels and shared divider positions."""

from __future__ import annotations

from weakref import WeakSet

import gi  # pyright: ignore[reportMissingImports]

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402  # pyright: ignore[reportMissingImports, reportAttributeAccessIssue]


class WorkspaceLayout:
    """Share user-selected divider positions across a window's tools."""

    def __init__(self) -> None:
        self.positions = self._defaults()
        self._panes: WeakSet[ResponsivePaned] = WeakSet()

    @staticmethod
    def _defaults() -> dict[Gtk.Orientation, int]:
        return {Gtk.Orientation.HORIZONTAL: 360, Gtk.Orientation.VERTICAL: 240}

    def register(self, pane: ResponsivePaned) -> None:
        self._panes.add(pane)

    def minimum_position(self, orientation: Gtk.Orientation, cross_size: int) -> int:
        minimum = 0
        for pane in tuple(self._panes):
            start = pane.get_start_child()
            if start is not None:
                minimum = max(minimum, start.measure(orientation, cross_size).minimum)
        return minimum

    def set_position(
        self,
        orientation: Gtk.Orientation,
        position: int,
        source: ResponsivePaned | None = None,
    ) -> None:
        if source is not None:
            cross_size = (
                source.get_width()
                if orientation == Gtk.Orientation.VERTICAL else source.get_height()
            )
            if cross_size > 0:
                position = max(position, self.minimum_position(orientation, cross_size))
        self.positions[orientation] = position
        for pane in tuple(self._panes):
            if pane is not source and pane.get_orientation() == orientation:
                pane.apply_layout_position(position)

    def reset(self) -> None:
        for orientation, position in self._defaults().items():
            self.set_position(orientation, position)


class ResponsivePaned(Gtk.Paned):
    """Keep both panels usable when the window becomes narrow or short."""

    def __init__(self, layout: WorkspaceLayout | None = None) -> None:
        super().__init__(
            orientation=Gtk.Orientation.HORIZONTAL,
            wide_handle=True,
            hexpand=True,
            vexpand=True,
        )
        self._layout = layout if layout is not None else WorkspaceLayout()
        self._applying_layout = False
        self._layout.register(self)
        self.set_position(self._layout.positions[Gtk.Orientation.HORIZONTAL])
        self.set_resize_start_child(False)
        self.set_shrink_start_child(False)
        self.set_shrink_end_child(False)
        self.connect("notify::position", self._on_position_changed)

    def _on_position_changed(self, *_args: object) -> None:
        if not self._applying_layout:
            self._layout.set_position(self.get_orientation(), self.get_position(), self)

    def apply_layout_position(self, position: int) -> None:
        previous = self._applying_layout
        self._applying_layout = True
        try:
            self.set_position(position)
        finally:
            self._applying_layout = previous

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        # Stacking helps narrow, tall windows; short windows need both panels
        # to keep the full available height for their scrollable content.
        root = self.get_root()
        tall = root.get_height() >= 600 if isinstance(root, Gtk.Window) else height >= 400
        orientation = (
            Gtk.Orientation.VERTICAL if width < 760 and tall else Gtk.Orientation.HORIZONTAL
        )
        self._applying_layout = True
        try:
            self.set_orientation(orientation)
            available = height if orientation == Gtk.Orientation.VERTICAL else width
            cross_size = width if orientation == Gtk.Orientation.VERTICAL else height
            minimum = self._layout.minimum_position(orientation, cross_size)
            position = max(
                minimum,
                min(self._layout.positions[orientation], int(available * 0.65)),
            )
            self.set_position(position)
            # Automatic allocation/clamping must not overwrite the user's
            # shared position when a hidden tab is laid out or the window shrinks.
            Gtk.Paned.do_size_allocate(self, width, height, baseline)
        finally:
            self._applying_layout = False
