"""Reusable GTK application shell and controller-backed tab infrastructure."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from itertools import chain, islice
from pathlib import Path
import sys
from threading import Condition
from typing import Generic, Protocol, TypeVar
from weakref import WeakSet

import gi  # pyright: ignore[reportMissingImports]

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402  # pyright: ignore[reportMissingImports, reportAttributeAccessIssue]

from cleanup_cli.controllers.core import Controller
from cleanup_cli.models.progress import TaskProgress
from cleanup_cli.views.gui.results import ResultList, ResultRow


RequestT = TypeVar("RequestT")
ResultT = TypeVar("ResultT")
StreamedT = TypeVar("StreamedT")


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
            version="0.1.0",
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


class ControllerGtkTab(Generic[RequestT, ResultT, StreamedT]):
    """Shared asynchronous execution behavior for controller-backed tabs."""

    title: str
    icon_name: str
    description = ""
    empty_title = "Ready when you are"
    empty_description = "Choose an image folder and run this tool to see results."
    empty_steps: tuple[str, ...] = ()
    _RESULT_BATCH_SIZE = 128
    _MAX_PENDING_RESULTS = 2048
    _UPDATE_INTERVAL_MS = 16

    def __init__(self, controller: Controller[RequestT, ResultT]) -> None:
        self._controller = controller
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="cleanup-gui",
        )
        self._form: Gtk.Widget | None = None
        self._run_button: Gtk.Button | None = None
        self._spinner: Gtk.Spinner | None = None
        self._activity_label: Gtk.Label | None = None
        self._progress_bar: Gtk.ProgressBar | None = None
        self._activity_box: Gtk.Box | None = None
        self._error_revealer: Gtk.Revealer | None = None
        self._error_label: Gtk.Label | None = None
        self._result_stack: Gtk.Stack | None = None
        self._results: ResultList | None = None
        self._result_list: Gtk.ListView | None = None
        self._result_model: Gio.ListStore | None = None
        self._summary_icon: Gtk.Image | None = None
        self._summary_label: Gtk.Label | None = None
        self._mode_label: Gtk.Label | None = None
        self._mode_description: Gtk.Label | None = None
        self._layout: WorkspaceLayout | None = None
        self._running = False
        self._closed = False
        self._updates = Condition()
        self._pending_results: deque[StreamedT] = deque()
        self._pending_progress: TaskProgress | None = None
        self._update_source: int | None = None
        self._completion: Future[ResultT] | None = None
        self._display_rows: Iterator[ResultRow] | None = None
        self._streaming_results = False

    def shutdown(self) -> None:
        with self._updates:
            self._closed = True
            self._pending_results.clear()
            self._pending_progress = None
            source = self._update_source
            self._update_source = None
            self._updates.notify_all()
        if source is not None:
            GLib.source_remove(source)
        self._display_rows = None
        self._completion = None
        self._executor.shutdown(wait=False, cancel_futures=True)
        if self._results is not None:
            self._results.close()

    def set_workspace_layout(self, layout: WorkspaceLayout) -> None:
        """Use the main window's shared panel sizes when building this tab."""

        self._layout = layout

    def _page(self, form: Gtk.Widget) -> Gtk.Widget:
        self._form = form
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        _set_margins(page, 20)

        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        title = Gtk.Label(label=self.title, xalign=0, wrap=True)
        title.add_css_class("title-1")
        titles.append(title)
        description = Gtk.Label(label=self.description, xalign=0, wrap=True)
        description.add_css_class("dim-label")
        titles.append(description)
        heading.append(titles)
        mode_label = Gtk.Label(label="Preview", valign=Gtk.Align.CENTER)
        mode_label.add_css_class("cleanup-mode")
        self._mode_label = mode_label
        heading.append(mode_label)
        page.append(heading)

        settings = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        settings_scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            overlay_scrolling=False,
            vexpand=True,
            child=form,
        )
        settings.append(settings_scroll)
        action = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        action.add_css_class("cleanup-action")
        mode_description = Gtk.Label(xalign=0, wrap=True)
        mode_description.add_css_class("dim-label")
        self._mode_description = mode_description
        action.append(mode_description)
        if self._run_button is not None:
            self._run_button.set_hexpand(True)
            self._run_button.set_halign(Gtk.Align.FILL)
            action.append(self._run_button)
        settings.append(action)

        results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        results.add_css_class("cleanup-card")
        result_header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        _set_margins(result_header, 16)
        result_heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        result_title = Gtk.Label(label="Results", xalign=0, hexpand=True)
        result_title.add_css_class("title-3")
        result_heading.append(result_title)
        result_header.append(result_heading)
        summary = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        summary_icon = Gtk.Image(valign=Gtk.Align.START)
        summary_icon.set_visible(False)
        self._summary_icon = summary_icon
        summary_label = Gtk.Label(
            xalign=0,
            wrap=False,
            hexpand=True,
            ellipsize=Pango.EllipsizeMode.END,
        )
        summary_label.set_text("Your results and storage savings will appear here.")
        summary_label.add_css_class("dim-label")
        summary_label.add_css_class("cleanup-summary")
        self._summary_label = summary_label
        summary.append(summary_icon)
        summary.append(summary_label)
        result_header.append(summary)
        results.append(result_header)
        results.append(Gtk.Separator())

        feedback = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        feedback.set_margin_start(16)
        feedback.set_margin_end(16)

        error_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        error_box.add_css_class("cleanup-error")
        error_box.set_margin_top(12)
        error_box.append(Gtk.Image.new_from_icon_name("dialog-error-symbolic"))
        self._error_label = Gtk.Label(
            xalign=0, wrap=True, hexpand=True, lines=3,
            ellipsize=Pango.EllipsizeMode.END,
        )
        error_box.append(self._error_label)
        self._error_revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
            child=error_box,
        )
        feedback.append(self._error_revealer)

        activity = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        activity.set_margin_top(12)
        activity.set_margin_bottom(12)
        activity_heading = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        self._spinner = Gtk.Spinner()
        self._activity_label = Gtk.Label(
            xalign=0,
            wrap=True,
            hexpand=True,
            lines=2,
            ellipsize=Pango.EllipsizeMode.END,
        )
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
        feedback.append(activity)
        results.append(feedback)

        result_stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            hexpand=True,
            vexpand=True,
            hhomogeneous=False,
            vhomogeneous=False,
        )
        self._result_stack = result_stack
        result_stack.add_named(
            _empty_state(
                self.icon_name,
                self.empty_title,
                self.empty_description,
                steps=self.empty_steps,
                scroll=True,
            ),
            "empty",
        )
        result_list = ResultList()
        self._results = result_list
        self._result_list = result_list.widget
        self._result_model = result_list.model
        results_scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            child=result_list.widget,
        )
        result_stack.add_named(results_scroll, "results")
        result_stack.set_visible_child_name("empty")
        results.append(result_stack)

        split = ResponsivePaned(self._layout)
        split.add_css_class("cleanup-workspace")
        split.set_start_child(settings)
        split.set_end_child(results)
        page.append(split)
        return page

    def _update_mode(self, destructive: bool, action: str, description: str) -> None:
        if self._mode_label is not None:
            self._mode_label.set_text(action if destructive else "Preview")
            if destructive:
                self._mode_label.add_css_class("cleanup-destructive-mode")
            else:
                self._mode_label.remove_css_class("cleanup-destructive-mode")
        if self._mode_description is not None:
            self._mode_description.set_text(description)
        if self._run_button is not None:
            self._run_button.remove_css_class(
                "suggested-action" if destructive else "destructive-action"
            )
            self._run_button.add_css_class(
                "destructive-action" if destructive else "suggested-action"
            )

    def _submit(self, request: RequestT, activity: str) -> None:
        if self._running or self._closed:
            return
        self._running = True
        self._clear_error()
        self._set_busy(True, activity)
        future = self._executor.submit(self._controller.execute, request)
        future.add_done_callback(self._schedule_completion)

    def _schedule_completion(self, future: Future[ResultT]) -> None:
        GLib.idle_add(self._complete, future)

    def _queue_progress(self, progress: TaskProgress) -> None:
        with self._updates:
            if self._closed:
                return
            self._pending_progress = progress
            self._ensure_update_source()

    def _queue_streamed_result(self, result: StreamedT) -> None:
        with self._updates:
            # Bound queued data without dropping results. Shutdown wakes a
            # controller waiting for the GTK thread to consume a batch.
            while len(self._pending_results) >= self._MAX_PENDING_RESULTS and not self._closed:
                self._updates.wait()
            if self._closed:
                return
            self._pending_results.append(result)
            self._ensure_update_source()

    def _ensure_update_source(self) -> None:
        """Schedule one flush while holding the update condition."""

        if self._update_source is None:
            self._update_source = GLib.timeout_add(
                self._UPDATE_INTERVAL_MS,
                self._flush_updates,
                priority=GLib.PRIORITY_DEFAULT_IDLE,
            )

    def _flush_updates(self) -> bool:
        with self._updates:
            if self._closed:
                self._update_source = None
                return GLib.SOURCE_REMOVE
            batch = [
                self._pending_results.popleft()
                for _ in range(min(len(self._pending_results), self._RESULT_BATCH_SIZE))
            ]
            progress = self._pending_progress
            self._pending_progress = None
            self._updates.notify_all()

        if batch:
            self._append_result_batch(batch)
        if progress is not None:
            self._apply_progress(progress)
        if self._display_rows is not None:
            rows = list(islice(self._display_rows, self._RESULT_BATCH_SIZE))
            if rows:
                self._append_rows(rows)
            else:
                self._display_rows = None
                self._running = False
                self._set_busy(False, "")

        with self._updates:
            pending = bool(self._pending_results or self._pending_progress is not None)
        if not pending and self._completion is not None:
            self._finish_completion()

        with self._updates:
            more = bool(
                self._pending_results or self._pending_progress is not None
                or self._display_rows is not None
            )
            if not more:
                self._update_source = None
        return GLib.SOURCE_CONTINUE if more else GLib.SOURCE_REMOVE

    def _apply_progress(self, progress: TaskProgress) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        if self._activity_label is not None:
            self._activity_label.set_text(progress.activity)
            self._activity_label.set_tooltip_text(progress.activity)
        if self._progress_bar is not None:
            self._progress_bar.set_fraction(progress.fraction)
            self._progress_bar.set_text(
                f"{progress.completed} of {progress.total} files"
            )
        return GLib.SOURCE_REMOVE

    def _complete(self, future: Future[ResultT]) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        self._completion = future
        with self._updates:
            pending = bool(self._pending_results or self._pending_progress is not None)
            if pending:
                self._ensure_update_source()
        if not pending:
            self._finish_completion()
        return GLib.SOURCE_REMOVE

    def _finish_completion(self) -> None:
        future = self._completion
        if future is None:
            return
        self._completion = None
        try:
            result = future.result()
        except Exception as error:
            self._show_error(str(error) or type(error).__name__)
        else:
            self._render_result(result)
        self._streaming_results = False
        if self._display_rows is None:
            self._running = False
            self._set_busy(False, "")

    def _set_busy(self, busy: bool, activity: str) -> None:
        if self._form is not None:
            self._form.set_sensitive(not busy)
        if self._run_button is not None:
            self._run_button.set_sensitive(not busy)
        if self._spinner is not None:
            if busy:
                self._spinner.start()
            else:
                self._spinner.stop()
        if self._activity_label is not None:
            self._activity_label.set_text(activity)
            self._activity_label.set_tooltip_text(activity or None)
        if self._progress_bar is not None and busy:
            self._progress_bar.set_fraction(0.0)
            self._progress_bar.set_text("Preparing...")
        if self._activity_box is not None:
            self._activity_box.set_visible(busy)

    def _show_error(self, message: str) -> None:
        if self._error_label is not None:
            self._error_label.set_text(message)
            self._error_label.set_tooltip_text(message)
        if self._error_revealer is not None:
            self._error_revealer.set_reveal_child(True)

    def _clear_error(self) -> None:
        if self._error_revealer is not None:
            self._error_revealer.set_reveal_child(False)

    def _prepare_results(self) -> ResultList:
        if self._results is None or self._result_stack is None:
            raise RuntimeError("tab has not been built")
        self._results.clear()
        self._result_stack.set_visible_child_name("results")
        return self._results

    def _begin_results(self) -> None:
        self._prepare_results()
        self._streaming_results = True
        if self._summary_icon is not None:
            self._summary_icon.set_visible(False)
        if self._summary_label is not None:
            self._summary_label.set_text("")
            self._summary_label.set_tooltip_text(None)

    def _append_rows(self, rows: Sequence[ResultRow]) -> None:
        if self._results is None:
            raise RuntimeError("tab has not been built")
        self._results.append_rows(rows)

    def _replace_result_rows(self, rows: Iterable[ResultRow]) -> None:
        self._prepare_results()
        iterator = iter(rows)
        self._append_rows(list(islice(iterator, self._RESULT_BATCH_SIZE)))
        next_row = next(iterator, None)
        if next_row is not None:
            self._display_rows = chain((next_row,), iterator)
            self._running = True
            self._set_busy(True, "Displaying results...")
            with self._updates:
                self._ensure_update_source()

    def _has_streamed_results(self, count: int) -> bool:
        return (
            self._streaming_results and self._result_model is not None
            and self._result_model.get_n_items() == count
        )

    def _append_result_batch(self, results: Sequence[StreamedT]) -> None:
        raise NotImplementedError

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
            _empty_state(icon_name, title, description, scroll=True),
            "empty",
        )
        self._result_stack.set_visible_child_name("empty")

    def _set_summary(self, icon_name: str, text: str) -> None:
        if self._summary_icon is not None:
            self._summary_icon.set_from_icon_name(icon_name)
            self._summary_icon.set_visible(True)
        if self._summary_label is not None:
            self._summary_label.set_text(text)
            self._summary_label.set_tooltip_text(text)

    def _render_result(self, result: ResultT) -> None:
        raise NotImplementedError


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


def _set_margins(widget: Gtk.Widget, margin: int) -> None:
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
) -> ResultRow:
    """Describe a result without creating widgets for off-screen rows."""

    return ResultRow(icon_name, primary, secondary)


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
    *,
    steps: tuple[str, ...] = (),
    scroll: bool = True,
) -> Gtk.Widget:
    box = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=12,
        valign=Gtk.Align.CENTER,
        hexpand=True,
        vexpand=True,
    )
    _set_margins(box, 24)
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
