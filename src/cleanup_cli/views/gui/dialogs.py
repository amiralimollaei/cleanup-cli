"""Native asynchronous GTK folder selection and destructive-action confirmation."""

from __future__ import annotations

from collections.abc import Callable

import gi  # pyright: ignore[reportMissingImports]

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402  # pyright: ignore[reportMissingImports, reportAttributeAccessIssue]


def choose_folder(entry: Gtk.Entry, title: str) -> None:
    """Open GTK's native asynchronous folder chooser for an entry."""

    dialog = Gtk.FileDialog()
    dialog.set_title(title)
    current = entry.get_text().strip()
    if current:
        current_file = Gio.File.new_for_path(current)
        if current_file.query_exists():
            dialog.set_initial_folder(current_file)

    root = entry.get_root()
    parent = root if isinstance(root, Gtk.Window) else None

    def selected(file_dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            folder = file_dialog.select_folder_finish(result)
        except GLib.Error:
            return
        path = folder.get_path()
        if path is not None:
            entry.set_text(path)

    dialog.select_folder(parent, None, selected)


def confirm_destructive_action(
    parent_widget: Gtk.Widget,
    *,
    heading: str,
    body: str,
    action_label: str,
    callback: Callable[[], None],
) -> None:
    """Show a GNOME alert before a destructive operation."""

    dialog = Gtk.AlertDialog()
    dialog.set_message(heading)
    dialog.set_detail(body)
    dialog.set_buttons(["Cancel", action_label])
    dialog.set_cancel_button(0)
    dialog.set_default_button(0)

    root = parent_widget.get_root()
    parent = root if isinstance(root, Gtk.Window) else None

    def chosen(alert: Gtk.AlertDialog, result: Gio.AsyncResult) -> None:
        try:
            response = alert.choose_finish(result)
        except GLib.Error:
            return
        if response == 1:
            callback()

    dialog.choose(parent, None, chosen)
