"""GTK application shell and tab composition."""

from __future__ import annotations

from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path
import sys
from typing import Protocol

import gi  # pyright: ignore[reportMissingImports]

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, Gtk, Pango  # noqa: E402  # pyright: ignore[reportMissingImports, reportAttributeAccessIssue]

from cleanup_cli.views.gui.layout import WorkspaceLayout
from cleanup_cli.views.gui.theme import GnomeThemeSynchronizer
from cleanup_cli.views.gui.widgets import empty_state


class GtkTab(Protocol):
    """A page that can be registered in :class:`GtkGuiView`."""

    title: str
    icon_name: str

    def build(self) -> Gtk.Widget:
        """Build and return this tab's GTK content."""
        ...


class GtkGuiView:
    """GTK implementation of the main view, composed from arbitrary tabs."""

    def __init__(
        self,
        *tabs: GtkTab,
        application_id: str = "io.github.amiralimollaei.CleanupCli",
        title: str = "Image Cleanup",
    ) -> None:
        self._tabs = tabs
        self._title = title
        self._application = Gtk.Application(application_id=application_id)
        self._theme = GnomeThemeSynchronizer()
        self._styles: Gtk.CssProvider | None = None
        self._stack: Gtk.Stack | None = None
        self._tool_switcher: Gtk.StackSwitcher | None = None
        self._layout = WorkspaceLayout()
        self._application.connect("activate", self._on_activate)
        self._application.connect("shutdown", self._on_shutdown)
        self._install_actions()

    @property
    def tabs(self) -> tuple[GtkTab, ...]:
        """Return the tabs registered with this view in display order."""

        return self._tabs

    def run(self, arguments: Sequence[str] | None = None) -> int:
        """Start the GTK application event loop."""

        argv = list(sys.argv if arguments is None else [sys.argv[0], *arguments])
        return self._application.run(argv)

    def _install_actions(self) -> None:
        reset_layout = Gio.SimpleAction.new("reset-layout", None)
        reset_layout.connect("activate", lambda *_: self._layout.reset())
        self._application.add_action(reset_layout)

        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._show_about)
        self._application.add_action(about)

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self._application.quit())
        self._application.add_action(quit_action)
        self._application.set_accels_for_action("app.quit", ["<Control>q"])

    def _on_activate(self, application: Gtk.Application) -> None:
        self._theme.start()
        self._install_styles()
        existing = application.get_active_window()
        if existing is not None:
            existing.present()
            return

        window = Gtk.ApplicationWindow(application=application)
        window.add_css_class("cleanup-window")
        window.set_title(self._title)
        window.set_default_size(1100, 760)
        window.set_size_request(560, 400)
        window.set_titlebar(self._build_header_bar())
        window.set_child(self._build_main_view())
        window.present()

    def _install_styles(self) -> None:
        if self._styles is not None:
            return
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(str(Path(__file__).with_name("style.css")))
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self._styles = provider

    def _build_header_bar(self) -> Gtk.HeaderBar:
        header = Gtk.HeaderBar()
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        heading.set_margin_top(6)
        heading.set_margin_bottom(6)
        title = Gtk.Label(label=self._title, xalign=0)
        title.add_css_class("title")
        subtitle = Gtk.Label(label="Image maintenance tools", xalign=0)
        subtitle.add_css_class("subtitle")
        heading.append(title)
        heading.append(subtitle)
        tools = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        tools.append(heading)
        self._tool_switcher = None
        if len(self._tabs) > 1:
            switcher = Gtk.StackSwitcher(valign=Gtk.Align.CENTER)
            switcher.add_css_class("cleanup-tool-switcher")
            self._tool_switcher = switcher
            self._bind_tool_switcher()
            navigation = Gtk.ScrolledWindow(
                hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                vscrollbar_policy=Gtk.PolicyType.NEVER,
                propagate_natural_width=True,
                propagate_natural_height=True,
                max_content_width=500,
                hexpand=True,
                child=switcher,
            )
            viewport = navigation.get_child()
            if isinstance(viewport, Gtk.Viewport):
                viewport.set_scroll_to_focus(True)
            tools.append(navigation)
        header.pack_start(tools)
        header.set_title_widget(Gtk.Box())

        menu = Gio.Menu()
        menu.append("Reset Panel Sizes", "app.reset-layout")
        menu.append("About Image Cleanup", "app.about")
        menu.append("Quit", "app.quit")
        menu_button = Gtk.MenuButton(
            icon_name="open-menu-symbolic",
            menu_model=menu,
            tooltip_text="Main menu",
        )
        header.pack_end(menu_button)
        return header

    def _build_main_view(self) -> Gtk.Widget:
        if not self._tabs:
            self._stack = None
            return empty_state(
                "view-grid-symbolic",
                "No tools available",
                "Add a tab when constructing the main view.",
            )

        stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            transition_duration=160,
            hexpand=True,
            vexpand=True,
            hhomogeneous=False,
            vhomogeneous=False,
        )
        for index, tab in enumerate(self._tabs):
            configure_layout = getattr(tab, "set_workspace_layout", None)
            if callable(configure_layout):
                configure_layout(self._layout)
            child = tab.build()
            stack.add_titled(child, f"tab-{index}", tab.title)

        self._stack = stack
        self._bind_tool_switcher()

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main.append(stack)
        return main

    def _bind_tool_switcher(self) -> None:
        switcher = self._tool_switcher
        if switcher is None:
            return
        switcher.set_stack(self._stack)
        button = switcher.get_first_child()
        while button is not None:
            if isinstance(button, Gtk.Button):
                label = button.get_child()
                if isinstance(label, Gtk.Label):
                    label.set_ellipsize(Pango.EllipsizeMode.END)
                    button.set_tooltip_text(label.get_text())
            button = button.get_next_sibling()

    def _show_about(self, *_args: object) -> None:
        dialog = Gtk.AboutDialog(
            transient_for=self._application.get_active_window(),
            modal=True,
            program_name=self._title,
            version=version("cleanup-cli"),
            comments="Find duplicate images and convert images to WebP.",
            website="https://github.com/amiralimollaei/cleanup-cli",
            authors=["amiralimollaei"],
            logo_icon_name="user-trash-symbolic",
        )
        dialog.present()

    def _on_shutdown(self, *_args: object) -> None:
        self._theme.stop()
        display = Gdk.Display.get_default()
        if display is not None and self._styles is not None:
            Gtk.StyleContext.remove_provider_for_display(display, self._styles)
        self._styles = None
        for tab in self._tabs:
            shutdown = getattr(tab, "shutdown", None)
            if callable(shutdown):
                shutdown()
