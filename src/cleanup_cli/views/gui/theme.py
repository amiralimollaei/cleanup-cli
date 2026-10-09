"""GNOME color-scheme synchronization for plain GTK 4."""

from __future__ import annotations

import gi  # pyright: ignore[reportMissingImports]

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, Gtk  # noqa: E402  # pyright: ignore[reportMissingImports, reportAttributeAccessIssue]


class GnomeThemeSynchronizer:
    """Apply GNOME's modern light/dark preference to plain GTK 4.

    GTK already reads the configured widget and icon theme names. Unlike
    libadwaita, however, plain GTK does not consume GNOME's ``color-scheme``
    setting automatically. This adapter changes only GTK's dark-variant hint
    and leaves every actual theme choice under system control.
    """

    schema_id = "org.gnome.desktop.interface"
    key = "color-scheme"

    def __init__(self) -> None:
        self._settings: Gio.Settings | None = None
        self._handler_id: int | None = None

    def start(self) -> None:
        """Apply the current preference and subscribe to future changes."""

        if self._settings is None:
            self._settings = self._create_settings()
        if self._settings is None:
            return
        if self._handler_id is None:
            self._handler_id = self._settings.connect(
                f"changed::{self.key}",
                self._on_color_scheme_changed,
            )
        self._apply(self._settings.get_string(self.key))

    def stop(self) -> None:
        """Disconnect the GNOME preference listener, if it was installed."""

        if self._settings is not None and self._handler_id is not None:
            self._settings.disconnect(self._handler_id)
        self._handler_id = None

    @classmethod
    def _create_settings(cls) -> Gio.Settings | None:
        source = Gio.SettingsSchemaSource.get_default()
        if source is None:
            return None
        schema = source.lookup(cls.schema_id, True)
        if schema is None or not schema.has_key(cls.key):
            return None
        return Gio.Settings.new_full(schema, None, None)

    def _on_color_scheme_changed(
        self,
        settings: Gio.Settings,
        _key: str,
    ) -> None:
        self._apply(settings.get_string(self.key))

    @staticmethod
    def _apply(color_scheme: str) -> None:
        gtk_settings = Gtk.Settings.get_default()
        if gtk_settings is None:
            return
        gtk_settings.set_property(
            "gtk-application-prefer-dark-theme",
            color_scheme == "prefer-dark",
        )
