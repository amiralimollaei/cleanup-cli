"""Reusable form controls and page presentation helpers."""

from __future__ import annotations

import gi  # pyright: ignore[reportMissingImports]

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402  # pyright: ignore[reportMissingImports, reportAttributeAccessIssue]

from cleanup_cli.views.gui.results import ResultRow


def settings_group(title: str, *rows: Gtk.Widget) -> Gtk.Box:
    """Group related controls in a compact, themed card."""

    group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    heading = Gtk.Label(label=title, xalign=0)
    heading.add_css_class("heading")
    group.append(heading)
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    card.add_css_class("cleanup-card")
    for index, row in enumerate(rows):
        if index:
            card.append(Gtk.Separator())
        card.append(row)
    group.append(card)
    return group


def setting_row(
    label: str,
    control: Gtk.Widget,
    description: str | None = None,
    *,
    stacked: bool = False,
) -> Gtk.Box:
    """Keep numeric inputs compact and let text wrap beside them."""

    row = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL if stacked else Gtk.Orientation.HORIZONTAL,
        spacing=8,
    )
    row.add_css_class("cleanup-setting-row")
    labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
    labels.set_valign(Gtk.Align.CENTER)
    if label:
        field_label = Gtk.Label(label=label, xalign=0, wrap=True)
        field_label.set_mnemonic_widget(control)
        labels.append(field_label)
    if description:
        detail = Gtk.Label(label=description, xalign=0, wrap=True)
        detail.add_css_class("dim-label")
        detail.add_css_class("caption")
        labels.append(detail)
    row.append(labels)
    control.set_valign(Gtk.Align.CENTER)
    if not stacked:
        control.set_halign(Gtk.Align.END)
    row.append(control)
    return row


def set_margins(widget: Gtk.Widget, margin: int) -> None:
    """Apply the same margin to all four sides of a widget."""

    widget.set_margin_top(margin)
    widget.set_margin_bottom(margin)
    widget.set_margin_start(margin)
    widget.set_margin_end(margin)


class OptionalNumberControl:
    """Numeric GTK control whose active value can be automatic (``None``)."""

    def __init__(
        self,
        *,
        minimum: float,
        maximum: float,
        value: float,
        unit: str | None = None,
    ) -> None:
        self.spin = Gtk.SpinButton.new_with_range(minimum, maximum, 1)
        self.spin.set_width_chars(5)
        self.spin.set_max_width_chars(5)
        self.spin.set_value(value)
        self.spin.set_numeric(True)
        self.spin.set_sensitive(False)
        self.automatic = Gtk.CheckButton(label="Auto", active=True)
        self.automatic.set_tooltip_text("Choose automatically for your system")
        self.automatic.connect(
            "toggled",
            lambda button: self.spin.set_sensitive(not button.get_active()),
        )

        self.widget = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.automatic.set_hexpand(True)
        self.widget.append(self.automatic)
        self.widget.append(self.spin)
        # Reserve a unit column even when this control has no visible unit so
        # paired controls keep their Auto checkboxes and spin buttons aligned.
        unit_label = Gtk.Label(label=unit or "")
        unit_label.set_width_chars(max(7, len(unit or "")))
        if unit is not None:
            unit_label.add_css_class("dim-label")
        self.widget.append(unit_label)

    @property
    def value(self) -> int | None:
        """Return the explicit integer, or ``None`` for automatic selection."""

        if self.automatic.get_active():
            return None
        return self.spin.get_value_as_int()

    def set_explicit(self, value: int) -> None:
        """Select and expose an explicit value."""

        self.automatic.set_active(False)
        self.spin.set_value(value)


def result_row(
    icon_name: str,
    primary: str,
    secondary: str,
) -> ResultRow:
    """Describe a result without creating widgets for off-screen rows."""

    return ResultRow(icon_name, primary, secondary)


def empty_state(
    icon_name: str,
    title: str,
    description: str | None,
    *,
    steps: tuple[str, ...] = (),
    scroll: bool = True,
) -> Gtk.Widget:
    """Build a centered placeholder with optional steps and scrolling."""

    box = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=12,
        valign=Gtk.Align.CENTER,
        hexpand=True,
        vexpand=True,
    )
    set_margins(box, 24)
    icon = Gtk.Image.new_from_icon_name(icon_name)
    icon.set_pixel_size(48)
    icon.add_css_class("dim-label")
    box.append(icon)
    heading = Gtk.Label(label=title, wrap=True, justify=Gtk.Justification.CENTER)
    heading.add_css_class("title-3")
    box.append(heading)
    if description is not None:
        detail = Gtk.Label(
            label=description,
            wrap=True,
            justify=Gtk.Justification.CENTER,
            max_width_chars=40,
        )
        detail.add_css_class("dim-label")
        box.append(detail)
    if steps:
        guide = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        guide.set_margin_top(12)
        for index, step in enumerate(steps, 1):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            badge = Gtk.Label(label=str(index), valign=Gtk.Align.START)
            badge.add_css_class("cleanup-step")
            row.append(badge)
            row.append(Gtk.Label(label=step, xalign=0, wrap=True, hexpand=True))
            guide.append(row)
        box.append(guide)
    if not scroll:
        return box
    return Gtk.ScrolledWindow(
        hscrollbar_policy=Gtk.PolicyType.NEVER,
        vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        hexpand=True,
        vexpand=True,
        child=box,
    )
