"""Reusable GTK application shell and controller-backed tab infrastructure."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
import sys
from typing import Generic, Protocol, TypeVar

import gi  # pyright: ignore[reportMissingImports]

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk, Pango  # noqa: E402  # pyright: ignore[reportMissingImports, reportAttributeAccessIssue]

from ...controllers import Controller
from ...models import TaskProgress


RequestT = TypeVar("RequestT")
ResultT = TypeVar("ResultT")


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
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._show_about)
        self._application.add_action(about)

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self._application.quit())
        self._application.add_action(quit_action)
        self._application.set_accels_for_action("app.quit", ["<Control>q"])

    def _on_activate(self, application: Gtk.Application) -> None:
        self._theme.start()
        existing = application.get_active_window()
        if existing is not None:
            existing.present()
            return

        window = Gtk.ApplicationWindow(application=application)
        window.set_title(self._title)
        window.set_default_size(980, 720)
        window.set_size_request(680, 520)
        window.set_titlebar(self._build_header_bar())
        window.set_child(self._build_main_view())
        window.present()

    def _build_header_bar(self) -> Gtk.HeaderBar:
        header = Gtk.HeaderBar()
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title = Gtk.Label(label=self._title)
        title.add_css_class("title")
        subtitle = Gtk.Label(label="Image maintenance tools")
        subtitle.add_css_class("subtitle")
        heading.append(title)
        heading.append(subtitle)
        header.set_title_widget(heading)

        menu = Gio.Menu()
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
            return _empty_state(
                "view-grid-symbolic",
                "No tools available",
                "Add a tab when constructing the main view.",
            )

        stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            transition_duration=160,
            hexpand=True,
            vexpand=True,
        )
        for index, tab in enumerate(self._tabs):
            child = tab.build()
            page = stack.add_titled(child, f"tab-{index}", tab.title)
            page.set_icon_name(tab.icon_name)

        sidebar = Gtk.StackSidebar(stack=stack)
        sidebar.set_size_request(220, -1)
        sidebar.set_vexpand(True)

        sidebar_scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        sidebar_scroll.set_child(sidebar)

        split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        split.set_start_child(sidebar_scroll)
        split.set_end_child(stack)
        split.set_position(220)
        split.set_resize_start_child(False)
        split.set_shrink_start_child(False)
        split.set_shrink_end_child(False)
        return split

    def _show_about(self, *_args: object) -> None:
        dialog = Gtk.AboutDialog(
            transient_for=self._application.get_active_window(),
            modal=True,
            program_name=self._title,
            version="0.1.0",
            comments="Find duplicate images and convert images to WebP.",
            website="https://github.com/amiralimollaei/cleanup-cli",
            authors=["amiralimollaei"],
            logo_icon_name="user-trash-symbolic",
        )
        dialog.present()

    def _on_shutdown(self, *_args: object) -> None:
        self._theme.stop()
        for tab in self._tabs:
            shutdown = getattr(tab, "shutdown", None)
            if callable(shutdown):
                shutdown()


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


class ControllerGtkTab(Generic[RequestT, ResultT]):
    """Shared asynchronous execution behavior for controller-backed tabs."""

    title: str
    icon_name: str

    def __init__(self, controller: Controller[RequestT, ResultT]) -> None:
        self._controller = controller
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cleanup-gui")
        self._form: Gtk.Widget | None = None
        self._run_button: Gtk.Button | None = None
        self._spinner: Gtk.Spinner | None = None
        self._activity_label: Gtk.Label | None = None
        self._progress_bar: Gtk.ProgressBar | None = None
        self._activity_box: Gtk.Box | None = None
        self._error_revealer: Gtk.Revealer | None = None
        self._error_label: Gtk.Label | None = None
        self._result_stack: Gtk.Stack | None = None
        self._result_list: Gtk.ListBox | None = None
        self._summary_icon: Gtk.Image | None = None
        self._summary_label: Gtk.Label | None = None
        self._running = False
        self._closed = False

    def shutdown(self) -> None:
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _page(self, form: Gtk.Widget) -> Gtk.Widget:
        self._form = form
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        page.set_margin_top(24)
        page.set_margin_bottom(24)
        page.set_margin_start(24)
        page.set_margin_end(24)

        title = Gtk.Label(label=self.title, xalign=0)
        title.add_css_class("title-1")
        page.append(title)
        page.append(form)

        error_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        error_box.append(Gtk.Image.new_from_icon_name("dialog-error-symbolic"))
        self._error_label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        error_box.append(self._error_label)
        self._error_revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
            child=error_box,
        )
        page.append(self._error_revealer)

        activity = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        activity_heading = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        self._spinner = Gtk.Spinner()
        self._activity_label = Gtk.Label(xalign=0)
        activity_heading.append(self._spinner)
        activity_heading.append(self._activity_label)
        activity.append(activity_heading)
        self._progress_bar = Gtk.ProgressBar(
            show_text=True,
            hexpand=True,
        )
        activity.append(self._progress_bar)
        activity.set_visible(False)
        self._activity_box = activity
        page.append(activity)

        result_heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        result_title = Gtk.Label(label="Results", xalign=0, hexpand=True)
        result_title.add_css_class("title-3")
        summary_icon = Gtk.Image()
        summary_icon.set_visible(False)
        self._summary_icon = summary_icon
        summary_label = Gtk.Label(xalign=1)
        summary_label.add_css_class("dim-label")
        self._summary_label = summary_label
        result_heading.append(result_title)
        result_heading.append(summary_icon)
        result_heading.append(summary_label)
        page.append(result_heading)

        result_stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            vexpand=True,
        )
        self._result_stack = result_stack
        result_stack.add_named(
            _empty_state("view-list-symbolic", "No results yet", None), "empty"
        )
        result_list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE,
            show_separators=True,
        )
        self._result_list = result_list
        results_scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            child=result_list,
        )
        results_scroll.set_min_content_height(220)
        frame = Gtk.Frame(child=results_scroll)
        result_stack.add_named(frame, "results")
        result_stack.set_visible_child_name("empty")
        page.append(result_stack)
        return page

    def _submit(self, request: RequestT, activity: str) -> None:
        if self._running:
            return
        self._running = True
        self._clear_error()
        self._set_busy(True, activity)
        future = self._executor.submit(self._controller.execute, request)
        future.add_done_callback(self._schedule_completion)

    def _schedule_completion(self, future: Future[ResultT]) -> None:
        GLib.idle_add(self._complete, future)

    def _queue_progress(self, progress: TaskProgress) -> None:
        GLib.idle_add(self._apply_progress, progress)

    def _apply_progress(self, progress: TaskProgress) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        if self._activity_label is not None:
            self._activity_label.set_text(progress.activity)
        if self._progress_bar is not None:
            self._progress_bar.set_fraction(progress.fraction)
            self._progress_bar.set_text(
                f"{progress.completed} of {progress.total} files"
            )
        return GLib.SOURCE_REMOVE

    def _complete(self, future: Future[ResultT]) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        self._running = False
        self._set_busy(False, "")
        try:
            result = future.result()
        except Exception as error:
            self._show_error(str(error) or type(error).__name__)
        else:
            self._render_result(result)
        return GLib.SOURCE_REMOVE

    def _set_busy(self, busy: bool, activity: str) -> None:
        if self._form is not None:
            self._form.set_sensitive(not busy)
        if self._spinner is not None:
            if busy:
                self._spinner.start()
            else:
                self._spinner.stop()
        if self._activity_label is not None:
            self._activity_label.set_text(activity)
        if self._progress_bar is not None and busy:
            self._progress_bar.set_fraction(0.0)
            self._progress_bar.set_text("Preparing...")
        if self._activity_box is not None:
            self._activity_box.set_visible(busy)

    def _show_error(self, message: str) -> None:
        if self._error_label is not None:
            self._error_label.set_text(message)
        if self._error_revealer is not None:
            self._error_revealer.set_reveal_child(True)

    def _clear_error(self) -> None:
        if self._error_revealer is not None:
            self._error_revealer.set_reveal_child(False)

    def _prepare_results(self) -> Gtk.ListBox:
        if self._result_list is None or self._result_stack is None:
            raise RuntimeError("tab has not been built")
        while child := self._result_list.get_first_child():
            self._result_list.remove(child)
        self._result_stack.set_visible_child_name("results")
        return self._result_list

    def _show_empty_result(
        self,
        icon_name: str,
        title: str,
        description: str | None = None,
    ) -> None:
        if self._result_stack is None:
            raise RuntimeError("tab has not been built")
        empty = self._result_stack.get_child_by_name("empty")
        if empty is not None:
            self._result_stack.remove(empty)
        self._result_stack.add_named(
            _empty_state(icon_name, title, description),
            "empty",
        )
        self._result_stack.set_visible_child_name("empty")

    def _set_summary(self, icon_name: str, text: str) -> None:
        if self._summary_icon is not None:
            self._summary_icon.set_from_icon_name(icon_name)
            self._summary_icon.set_visible(True)
        if self._summary_label is not None:
            self._summary_label.set_text(text)

    def _render_result(self, result: ResultT) -> None:
        raise NotImplementedError


def form_grid() -> Gtk.Grid:
    """Create the standard settings grid used by production tabs."""

    grid = Gtk.Grid(column_spacing=18, row_spacing=12, hexpand=True)
    grid.set_margin_bottom(4)
    return grid


def add_form_row(
    grid: Gtk.Grid,
    row: int,
    label: str,
    control: Gtk.Widget,
) -> None:
    """Add a left-aligned label and expanding control to a settings grid."""

    field_label = Gtk.Label(label=label, xalign=0)
    field_label.add_css_class("dim-label")
    field_label.set_mnemonic_widget(control)
    control.set_hexpand(True)
    grid.attach(field_label, 0, row, 1, 1)
    grid.attach(control, 1, row, 1, 1)


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
        self.spin.set_value(value)
        self.spin.set_numeric(True)
        self.spin.set_sensitive(False)
        self.automatic = Gtk.CheckButton(label="Automatic", active=True)
        self.automatic.connect(
            "toggled",
            lambda button: self.spin.set_sensitive(not button.get_active()),
        )

        self.widget = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.widget.append(self.automatic)
        self.widget.append(self.spin)
        if unit is not None:
            unit_label = Gtk.Label(label=unit)
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
) -> Gtk.Box:
    """Create a compact GNOME-style result row."""

    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    row.set_margin_top(10)
    row.set_margin_bottom(10)
    row.set_margin_start(12)
    row.set_margin_end(12)
    icon = Gtk.Image.new_from_icon_name(icon_name)
    icon.set_pixel_size(24)
    row.append(icon)

    labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
    primary_label = Gtk.Label(
        label=primary,
        xalign=0,
        ellipsize=Pango.EllipsizeMode.END,
    )
    primary_label.add_css_class("heading")
    primary_label.set_tooltip_text(primary)
    secondary_label = Gtk.Label(
        label=secondary,
        xalign=0,
        ellipsize=Pango.EllipsizeMode.END,
    )
    secondary_label.add_css_class("dim-label")
    secondary_label.set_tooltip_text(secondary)
    labels.append(primary_label)
    labels.append(secondary_label)
    row.append(labels)
    return row


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


def _empty_state(
    icon_name: str,
    title: str,
    description: str | None,
) -> Gtk.Widget:
    box = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=8,
        halign=Gtk.Align.CENTER,
        valign=Gtk.Align.CENTER,
        hexpand=True,
        vexpand=True,
    )
    icon = Gtk.Image.new_from_icon_name(icon_name)
    icon.set_pixel_size(48)
    icon.add_css_class("dim-label")
    box.append(icon)
    heading = Gtk.Label(label=title)
    heading.add_css_class("title-3")
    box.append(heading)
    if description is not None:
        detail = Gtk.Label(label=description)
        detail.add_css_class("dim-label")
        box.append(detail)
    return box