from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
import subprocess
import sys
import textwrap
import threading
import time
from typing import Generic, TypeVar

import gi  # pyright: ignore[reportMissingImports]
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402  # pyright: ignore[reportMissingImports, reportAttributeAccessIssue]

from cleanup_cli.controllers import (
    Controller,
    DeduplicationRequest,
    DeduplicationResult,
    WebPConversionRequest,
)
from cleanup_cli.models import (
    DeduplicationOptions,
    Duplicate,
    TaskProgress,
    WebPConversion,
    WebPDirectoryConversionResult,
    WebPOptions,
    WebPSkip,
)
from cleanup_cli.models.filesystem import FileIdentity
from cleanup_cli.views.gui import (
    DeduplicationGtkTab,
    GtkGuiView,
    WebPConversionGtkTab,
    create_gui_view,
)
from cleanup_cli.views.gui.application import (
    GnomeThemeSynchronizer,
    confirm_destructive_action,
)


RequestT = TypeVar("RequestT")
ResultT = TypeVar("ResultT")
GTK_AVAILABLE = bool(Gtk.init_check() and Gdk.Display.get_default() is not None)
requires_display = pytest.mark.skipif(
    not GTK_AVAILABLE,
    reason="GTK display is not available",
)


def _process_gtk_until(condition: Callable[[], bool], timeout: float = 2) -> None:
    context = GLib.MainContext.default()
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if time.monotonic() - started >= 0.05 and condition():
            return
        time.sleep(0.005)


class RecordingController(Controller[RequestT, ResultT], Generic[RequestT, ResultT]):
    def __init__(self, result: ResultT) -> None:
        self.result = result
        self.requests: list[RequestT] = []
        self.thread_ids: list[int | None] = []

    def execute(self, request: RequestT) -> ResultT:
        self.requests.append(request)
        self.thread_ids.append(threading.current_thread().ident)
        return self.result


class StreamingController(RecordingController[RequestT, ResultT]):
    def __init__(self, result: ResultT, emit: Callable[[RequestT], None]) -> None:
        super().__init__(result)
        self.emit = emit
        self.started = threading.Event()
        self.finished = threading.Event()

    def execute(self, request: RequestT) -> ResultT:
        super().execute(request)
        self.started.set()
        try:
            self.emit(request)
            return self.result
        finally:
            self.finished.set()


class StaticTab:
    icon_name = "applications-system-symbolic"

    def __init__(self, title: str) -> None:
        self.title = title
        self.build_count = 0

    def build(self) -> Gtk.Widget:
        self.build_count += 1
        return Gtk.Label(label=self.title)


def _walk_widgets(widget: Gtk.Widget) -> Iterator[Gtk.Widget]:
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _walk_widgets(child)
        child = child.get_next_sibling()


class RecordingAlertDialog:
    def __init__(self) -> None:
        self.message = ""
        self.detail = ""
        self.buttons: list[str] = []
        self.cancel_button = -1
        self.default_button = -1
        self.parent: Gtk.Window | None = None

    def set_message(self, message: str) -> None:
        self.message = message

    def set_detail(self, detail: str) -> None:
        self.detail = detail

    def set_buttons(self, buttons: list[str]) -> None:
        self.buttons = buttons

    def set_cancel_button(self, button: int) -> None:
        self.cancel_button = button

    def set_default_button(self, button: int) -> None:
        self.default_button = button

    def choose(self, parent: Gtk.Window | None, *_args: object) -> None:
        self.parent = parent


def test_deduplication_gui_request_validation_and_options() -> None:
    assert DeduplicationGtkTab.create_request(
        "/photos",
        threshold=4,
        delete=True,
        max_workers=3,
        memory_limit_mb=256,
    ) == DeduplicationRequest(
        Path("/photos"),
        DeduplicationOptions(
            threshold=4,
            delete=True,
            max_workers=3,
            memory_limit_mb=256,
        ),
    )

    with pytest.raises(ValueError, match="select a directory"):
        DeduplicationGtkTab.create_request("   ")
    with pytest.raises(ValueError, match="threshold must be between"):
        DeduplicationGtkTab.create_request("/photos", threshold=257)


def test_webp_gui_request_validation_and_options() -> None:
    assert WebPConversionGtkTab.create_request(
        "/photos",
        quality=90,
        replace=True,
        max_workers=3,
        memory_limit_mb=256,
    ) == WebPConversionRequest(
        Path("/photos"),
        WebPOptions(
            quality=90,
            replace=True,
            max_workers=3,
            memory_limit_mb=256,
        ),
    )

    with pytest.raises(ValueError, match="select a directory"):
        WebPConversionGtkTab.create_request("")
    with pytest.raises(ValueError, match="quality must be between"):
        WebPConversionGtkTab.create_request("/photos", quality=101)


def test_destructive_confirmation_uses_gtk_alert_dialog_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alert = RecordingAlertDialog()
    monkeypatch.setattr(Gtk, "AlertDialog", lambda: alert)
    parent_widget = type("ParentWidget", (), {"get_root": lambda self: None})()

    confirm_destructive_action(
        parent_widget,  # type: ignore[arg-type]
        heading="Delete duplicate images?",
        body="This action cannot be undone.",
        action_label="Delete",
        callback=lambda: None,
    )

    assert alert.message == "Delete duplicate images?"
    assert alert.detail == "This action cannot be undone."
    assert alert.buttons == ["Cancel", "Delete"]
    assert alert.cancel_button == 0
    assert alert.default_button == 0
    assert alert.parent is None


@requires_display
@pytest.mark.parametrize("header_first", [True, False])
def test_gui_main_view_accepts_any_number_of_tabs(header_first: bool) -> None:
    tabs = tuple(StaticTab(f"Tool {index}") for index in range(3))
    view = GtkGuiView(
        *tabs,
        application_id="io.github.amiralimollaei.CleanupCli.TabTest",
    )

    header = view._build_header_bar() if header_first else None
    main = view._build_main_view()
    if header is None:
        header = view._build_header_bar()

    assert view.tabs == tabs
    assert isinstance(main, Gtk.Box)
    stack = main.get_last_child()
    assert isinstance(stack, Gtk.Stack)
    assert stack.get_pages().get_n_items() == 3
    assert main.get_first_child() is stack
    switcher = next(
        widget for widget in _walk_widgets(header)
        if isinstance(widget, Gtk.StackSwitcher)
    )
    assert switcher.get_stack() is stack
    last_button = switcher.get_last_child()
    assert isinstance(last_button, Gtk.ToggleButton)
    last_button.set_active(True)
    assert stack.get_visible_child_name() == "tab-2"
    assert [tab.build_count for tab in tabs] == [1, 1, 1]
    # A custom tab does not need a shutdown hook.
    view._on_shutdown()


@requires_display
def test_gui_main_view_supports_no_tabs() -> None:
    view = GtkGuiView(
        application_id="io.github.amiralimollaei.CleanupCli.EmptyTest"
    )

    assert view.tabs == ()
    assert isinstance(view._build_main_view(), Gtk.ScrolledWindow)


@requires_display
def test_gui_header_title_has_vertical_padding() -> None:
    view = GtkGuiView(
        application_id="io.github.amiralimollaei.CleanupCli.HeaderTest"
    )

    header = view._build_header_bar()
    title = next(
        widget for widget in _walk_widgets(header)
        if isinstance(widget, Gtk.Label) and widget.get_text() == "Image Cleanup"
    )
    heading = title.get_parent()

    assert heading is not None
    assert heading.get_margin_top() == 6
    assert heading.get_margin_bottom() == 6


@requires_display
def test_gui_follows_gnome_color_scheme() -> None:
    synchronizer = GnomeThemeSynchronizer()
    interface = GnomeThemeSynchronizer._create_settings()
    if interface is None:
        pytest.skip("GNOME interface settings are not available")

    synchronizer.start()

    gtk_settings = Gtk.Settings.get_default()
    assert gtk_settings is not None
    assert bool(
        gtk_settings.get_property("gtk-application-prefer-dark-theme")
    ) is (interface.get_string("color-scheme") == "prefer-dark")
    synchronizer.stop()


@requires_display
def test_production_gui_icons_exist_in_the_active_theme() -> None:
    icon_theme = Gtk.IconTheme.get_for_display(Gtk.Widget.get_display(Gtk.Label()))
    icon_names = {
        "checkmark-symbolic",
        "dialog-error-symbolic",
        "dialog-information-symbolic",
        "edit-delete-symbolic",
        "edit-find-symbolic",
        "folder-open-symbolic",
        "folder-symbolic",
        "image-x-generic-symbolic",
        "media-playback-start-symbolic",
        "open-menu-symbolic",
        "user-trash-symbolic",
        "view-grid-symbolic",
        "view-list-symbolic",
    }

    assert not sorted(name for name in icon_names if not icon_theme.has_icon(name))


@requires_display
@pytest.mark.parametrize(
    ("color_scheme", "prefer_dark"),
    [
        ("prefer-dark", True),
        ("prefer-light", False),
        ("default", False),
    ],
)
def test_gnome_theme_mapping(color_scheme: str, prefer_dark: bool) -> None:
    gtk_settings = Gtk.Settings.get_default()
    assert gtk_settings is not None

    GnomeThemeSynchronizer._apply(color_scheme)

    assert bool(
        gtk_settings.get_property("gtk-application-prefer-dark-theme")
    ) is prefer_dark


@requires_display
def test_production_gui_composes_both_cleanup_tabs() -> None:
    view = create_gui_view()

    assert [type(tab) for tab in view.tabs] == [
        DeduplicationGtkTab,
        WebPConversionGtkTab,
    ]
    assert isinstance(view._build_main_view(), Gtk.Box)
    view._on_shutdown()


@requires_display
@pytest.mark.parametrize("tool", ["duplicates", "webp"])
def test_cleanup_tabs_resize_with_long_paths_and_scrollable_content(
    tool: str,
) -> None:
    long_path = Path("/photos") / ("very-long-directory-name-" * 12) / "image.png"
    if tool == "duplicates":
        result = DeduplicationResult(
            (Duplicate(long_path, long_path.with_name("keep.png"), 0),),
            deleted=False,
        )
        tab = DeduplicationGtkTab(RecordingController(result))
        other_tab = WebPConversionGtkTab(
            RecordingController(WebPDirectoryConversionResult((), ()))
        )
    else:
        result = WebPDirectoryConversionResult(
            (WebPConversion(long_path, long_path.with_suffix(".webp"), 2048, 1024),),
            (),
        )
        tab = WebPConversionGtkTab(RecordingController(result))
        other_tab = DeduplicationGtkTab(
            RecordingController(DeduplicationResult((), deleted=False))
        )

    view = GtkGuiView(tab, other_tab)
    view._install_styles()
    page = view._build_main_view()
    window = Gtk.Window(child=page)
    window.add_css_class("cleanup-window")
    window.set_titlebar(view._build_header_bar())
    try:
        assert tab._directory is not None
        assert tab._run_button is not None
        assert tab._result_list is not None
        assert tab._memory is not None
        tab._directory.set_text(str(long_path.parent))
        if isinstance(tab, DeduplicationGtkTab):
            assert isinstance(result, DeduplicationResult)
            tab._render_result(result)
        else:
            assert isinstance(result, WebPDirectoryConversionResult)
            tab._render_result(result)
        tab._set_summary("checkmark-symbolic", "100000 images processed | " * 8)
        tab._set_busy(True, "Processing a directory with a long name " * 8)

        settings_scroll = tab._directory.get_ancestor(Gtk.ScrolledWindow)
        results_scroll = tab._result_list.get_ancestor(Gtk.ScrolledWindow)
        assert isinstance(settings_scroll, Gtk.ScrolledWindow)
        assert isinstance(results_scroll, Gtk.ScrolledWindow)
        assert settings_scroll is not results_scroll
        assert settings_scroll.get_policy() == (
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        assert not settings_scroll.get_overlay_scrolling()
        assert results_scroll.get_policy()[1] == Gtk.PolicyType.AUTOMATIC
        split = settings_scroll.get_ancestor(Gtk.Paned)
        assert isinstance(split, Gtk.Paned)
        assert results_scroll.get_ancestor(Gtk.Paned) is split
        assert tab._run_button.get_ancestor(Gtk.Paned) is split
        directory_box = tab._directory.get_parent()
        assert directory_box is not None
        browse = directory_box.get_last_child()
        assert browse is not None
        memory_spin = tab._memory.spin

        def assert_form_fits_horizontally() -> None:
            adjustment = settings_scroll.get_hadjustment()
            assert adjustment.get_upper() <= adjustment.get_page_size() + 1
            for control in (browse, memory_spin):
                bounds_available, bounds = control.compute_bounds(settings_scroll)
                assert bounds_available
                assert bounds.get_x() >= 0
                assert bounds.get_x() + bounds.get_width() <= (
                    settings_scroll.get_width() + 1
                )

        window.present()
        context = GLib.MainContext.default()
        for width, height, expected_orientation in [
            (620, 440, Gtk.Orientation.HORIZONTAL),
            (620, 720, Gtk.Orientation.VERTICAL),
            (1100, 760, Gtk.Orientation.HORIZONTAL),
            (620, 440, Gtk.Orientation.HORIZONTAL),
        ]:
            window.set_default_size(width, height)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                while context.pending():
                    context.iteration(False)
                if window.get_width() == width and window.get_height() == height:
                    break
                time.sleep(0.005)

            assert window.get_width() == width
            assert window.get_height() == height
            assert split.get_orientation() == expected_orientation
            assert tab._summary_label is not None
            assert tab._summary_label.get_layout().get_line_count() == 1
            assert tab._summary_label.get_tooltip_text() == "100000 images processed | " * 8
            assert page.measure(Gtk.Orientation.HORIZONTAL, -1).minimum <= width
            assert tab._directory.get_width() > 0
            assert results_scroll.get_width() > 0
            assert results_scroll.get_height() > 0
            if width == 1100:
                assert_form_fits_horizontally()
                horizontal = split.get_orientation() == Gtk.Orientation.HORIZONTAL
                extent = (
                    split.get_width()
                    if horizontal
                    else split.get_height()
                )
                original_results_size = (
                    results_scroll.get_width()
                    if horizontal
                    else results_scroll.get_height()
                )
                split.set_position(round(extent * 0.45))
                deadline = time.monotonic() + 1
                results_size = original_results_size
                while time.monotonic() < deadline:
                    while context.pending():
                        context.iteration(False)
                    results_size = (
                        results_scroll.get_width()
                        if horizontal
                        else results_scroll.get_height()
                    )
                    if results_size != original_results_size:
                        break
                    time.sleep(0.005)
                assert results_size != original_results_size
                assert_form_fits_horizontally()
            entry_bounds_available, entry_bounds = tab._directory.compute_bounds(
                directory_box
            )
            browse_bounds_available, browse_bounds = browse.compute_bounds(directory_box)
            assert entry_bounds_available and browse_bounds_available
            assert settings_scroll.get_height() >= entry_bounds.get_height()
            assert directory_box.get_width() - entry_bounds.get_width() <= (
                browse_bounds.get_width() + 12
            )
            settings = split.get_start_child()
            assert settings is not None
            bounds_available, bounds = tab._run_button.compute_bounds(settings)
            assert bounds_available
            assert bounds.get_y() >= 0
            assert bounds.get_y() + bounds.get_height() <= settings.get_height() + 1
    finally:
        window.destroy()
        view._on_shutdown()


@requires_display
def test_tool_divider_is_shared_resettable_and_keeps_controls_visible() -> None:
    view = create_gui_view()
    view._install_styles()
    main = view._build_main_view()
    stack = main.get_last_child()
    assert isinstance(stack, Gtk.Stack)
    window = Gtk.Window(child=main)
    window.add_css_class("cleanup-window")
    window.set_titlebar(view._build_header_bar())
    tabs = view.tabs
    splits: list[Gtk.Paned] = []
    settings: list[Gtk.Widget] = []
    for tab in tabs:
        assert isinstance(tab, (DeduplicationGtkTab, WebPConversionGtkTab))
        assert tab._directory is not None
        split = tab._directory.get_ancestor(Gtk.Paned)
        assert isinstance(split, Gtk.Paned)
        start = split.get_start_child()
        assert start is not None
        splits.append(split)
        settings.append(start)

    try:
        window.set_default_size(1100, 760)
        window.present()
        _process_gtk_until(lambda: window.get_width() == 1100)
        default_horizontal_position = splits[0].get_position()
        splits[0].set_position(480)
        _process_gtk_until(lambda: settings[0].get_width() >= 480)
        first_position = splits[0].get_position()
        assert first_position == 480

        stack.set_visible_child_name("tab-1")
        _process_gtk_until(lambda: splits[1].get_width() > 0)
        assert splits[1].get_position() == first_position
        assert settings[1].get_width() == settings[0].get_width()

        window.set_default_size(620, 440)
        _process_gtk_until(
            lambda: window.get_width() == 620
            and splits[1].get_position() < first_position
        )
        assert view._layout.positions[Gtk.Orientation.HORIZONTAL] == first_position
        stack.set_visible_child_name("tab-0")
        _process_gtk_until(lambda: splits[0].get_position() == splits[1].get_position())
        assert splits[0].get_position() == splits[1].get_position()
        window.set_default_size(1100, 760)
        _process_gtk_until(
            lambda: window.get_width() == 1100
            and splits[0].get_position() == first_position
        )
        assert splits[0].get_position() == first_position

        for index, tab in enumerate(tabs):
            assert isinstance(tab, (DeduplicationGtkTab, WebPConversionGtkTab))
            assert tab._directory is not None
            assert tab._memory is not None
            stack.set_visible_child_name(f"tab-{index}")
            split = splits[index]
            scroll = tab._directory.get_ancestor(Gtk.ScrolledWindow)
            assert isinstance(scroll, Gtk.ScrolledWindow)
            split.set_position(10)
            _process_gtk_until(
                lambda: split.get_position() >= settings[index].measure(
                    Gtk.Orientation.HORIZONTAL, settings[index].get_height()
                ).minimum
            )
            assert scroll.get_policy()[0] == Gtk.PolicyType.NEVER
            adjustment = scroll.get_hadjustment()
            assert adjustment.get_upper() <= adjustment.get_page_size() + 1
            directory_box = tab._directory.get_parent()
            assert directory_box is not None
            browse = directory_box.get_last_child()
            assert browse is not None
            for control in (browse, tab._memory.spin):
                bounds_available, bounds = control.compute_bounds(scroll)
                assert bounds_available
                assert bounds.get_x() >= 0
                assert bounds.get_x() + bounds.get_width() <= scroll.get_width() + 1
            minimum_position = split.get_position()
            other_index = 1 - index
            stack.set_visible_child_name(f"tab-{other_index}")
            _process_gtk_until(
                lambda: splits[other_index].get_position() == minimum_position
            )
            assert splits[other_index].get_position() == minimum_position

        reset = view._application.lookup_action("reset-layout")
        assert reset is not None
        reset.activate(None)
        _process_gtk_until(
            lambda: splits[1].get_position() == default_horizontal_position
        )
        assert [split.get_position() for split in splits] == [
            default_horizontal_position,
            default_horizontal_position,
        ]
        assert view._layout.positions[Gtk.Orientation.HORIZONTAL] == 360

        window.set_default_size(620, 720)
        _process_gtk_until(
            lambda: splits[1].get_orientation() == Gtk.Orientation.VERTICAL
        )
        splits[1].set_position(280)
        _process_gtk_until(lambda: settings[1].get_height() >= 280)
        stack.set_visible_child_name("tab-0")
        _process_gtk_until(
            lambda: splits[0].get_orientation() == Gtk.Orientation.VERTICAL
        )
        assert splits[0].get_position() == splits[1].get_position() == 280
        reset.activate(None)
        _process_gtk_until(lambda: splits[0].get_position() == 240)
        assert [split.get_position() for split in splits] == [240, 240]

        webp_tab = tabs[1]
        assert isinstance(webp_tab, WebPConversionGtkTab)
        assert webp_tab._replace is not None
        webp_tab._replace.set_active(True)
        window.set_default_size(560, 720)
        _process_gtk_until(lambda: window.get_width() == 560)
        assert window.get_width() == 560
        splits[0].set_position(10)
        _process_gtk_until(lambda: splits[0].get_position() > 10)
        minimum_vertical_position = splits[0].get_position()
        stack.set_visible_child_name("tab-1")
        _process_gtk_until(
            lambda: splits[1].get_position() == minimum_vertical_position
        )
        assert splits[0].get_position() == splits[1].get_position()
        assert splits[1].get_orientation() == Gtk.Orientation.VERTICAL
        for index, tab in enumerate(tabs):
            assert isinstance(tab, (DeduplicationGtkTab, WebPConversionGtkTab))
            assert tab._run_button is not None
            assert minimum_vertical_position >= settings[index].measure(
                Gtk.Orientation.VERTICAL, settings[index].get_width()
            ).minimum
            bounds_available, bounds = tab._run_button.compute_bounds(settings[index])
            assert bounds_available
            assert bounds.get_y() >= 0
            assert bounds.get_y() + bounds.get_height() <= settings[index].get_height() + 1
    finally:
        window.destroy()
        view._on_shutdown()


@requires_display
def test_deduplication_tab_builds_form_request_and_renders_results() -> None:
    duplicate = Duplicate(
        Path("one.jpg"),
        Path("two.jpg"),
        3,
        FileIdentity(1, 2, 2048, 3),
    )
    controller = RecordingController(
        DeduplicationResult((duplicate,), deleted=False)
    )
    tab = DeduplicationGtkTab(controller)
    tab.build()
    assert tab._directory is not None
    assert tab._threshold is not None
    assert tab._workers is not None
    assert tab._memory is not None
    assert tab._delete is not None
    tab._directory.set_text("/photos")
    tab._threshold.set_value(4)
    tab._workers.set_explicit(3)
    tab._memory.set_explicit(256)

    request = tab._request_from_form()
    tab._render_result(controller.result)

    assert request == DeduplicationRequest(
        Path("/photos"),
        DeduplicationOptions(
            threshold=4,
            max_workers=3,
            memory_limit_mb=256,
        ),
    )
    assert tab._result_model is not None
    assert tab._result_model.get_n_items() == 1
    assert tab._result_model.get_item(0).row.primary == "Would delete: one.jpg"
    assert tab._summary_label is not None
    assert (
        tab._summary_label.get_text()
        == "1 duplicate found | 2.0 KiB would be saved"
    )
    tab.shutdown()


@requires_display
def test_webp_tab_builds_form_request_and_renders_results() -> None:
    conversion = WebPConversion(
        Path("photo.png"),
        Path("photo.webp"),
        original_size=2048,
        webp_size=1024,
    )
    skip = WebPSkip(Path("small.png"), "WebP would not be smaller")
    controller = RecordingController(
        WebPDirectoryConversionResult((conversion,), (skip,))
    )
    tab = WebPConversionGtkTab(controller)
    tab.build()
    assert tab._directory is not None
    assert tab._quality is not None
    assert tab._workers is not None
    assert tab._memory is not None
    assert tab._replace is not None
    tab._directory.set_text("/photos")
    tab._quality.set_value(90)
    tab._workers.set_explicit(3)
    tab._memory.set_explicit(256)
    tab._replace.set_active(True)

    request = tab._request_from_form()
    tab._render_result(controller.result)

    assert request == WebPConversionRequest(
        Path("/photos"),
        WebPOptions(
            quality=90,
            replace=True,
            max_workers=3,
            memory_limit_mb=256,
        ),
    )
    assert tab._result_model is not None
    assert tab._result_model.get_n_items() == 2
    assert tab._result_model.get_item(0).row.primary == "Converted: photo.png"
    assert tab._result_model.get_item(1).row.primary == "Skipped: small.png"
    assert tab._summary_label is not None
    assert tab._summary_label.get_text() == "1 converted, 1 skipped | 1.0 KiB saved"
    tab.shutdown()


@requires_display
def test_controller_execution_runs_off_the_gtk_thread() -> None:
    controller = RecordingController(DeduplicationResult((), deleted=False))
    tab = DeduplicationGtkTab(controller)
    tab.build()
    request = DeduplicationGtkTab.create_request("/photos")
    gtk_thread = threading.current_thread().ident

    tab._submit(request, "Working...")
    deadline = time.monotonic() + 2
    context = GLib.MainContext.default()
    while tab._running and time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.005)

    assert not tab._running
    assert controller.requests == [request]
    assert controller.thread_ids != [gtk_thread]
    assert tab._summary_label is not None
    assert (
        tab._summary_label.get_text()
        == "0 duplicates found | 0 bytes would be saved"
    )
    tab.shutdown()


@requires_display
def test_gui_progress_bar_updates_resets_and_hides_with_task_lifecycle() -> None:
    controller = RecordingController(DeduplicationResult((), deleted=False))
    tab = DeduplicationGtkTab(controller)
    tab.build()

    assert tab._progress_bar is not None
    assert tab._activity_box is not None
    assert tab._run_button is not None
    assert not tab._activity_box.get_visible()

    tab._set_busy(True, "Working...")
    assert not tab._run_button.get_sensitive()
    assert tab._activity_box.get_visible()
    assert tab._progress_bar.get_fraction() == 0.0
    assert tab._progress_bar.get_text() == "Preparing..."

    assert tab._apply_progress(TaskProgress("Indexing images", 2, 4)) is GLib.SOURCE_REMOVE
    assert tab._activity_label is not None
    assert tab._activity_label.get_text() == "Indexing images"
    assert tab._progress_bar.get_fraction() == pytest.approx(0.5)
    assert tab._progress_bar.get_text() == "2 of 4 files"

    tab._set_busy(False, "")
    assert tab._run_button.get_sensitive()
    assert not tab._activity_box.get_visible()

    # A second operation starts from a clean determinate state.
    tab._set_busy(True, "Starting again...")
    assert tab._progress_bar.get_fraction() == 0.0
    assert tab._progress_bar.get_text() == "Preparing..."
    tab.shutdown()


@requires_display
def test_deduplication_tab_submits_gui_progress_observer() -> None:
    controller = RecordingController(DeduplicationResult((), deleted=False))
    tab = DeduplicationGtkTab(controller)
    tab.build()

    tab._submit_with_results(
        DeduplicationGtkTab.create_request("/photos"),
        "Finding duplicate images...",
    )
    deadline = time.monotonic() + 2
    while not controller.requests and time.monotonic() < deadline:
        time.sleep(0.005)

    assert controller.requests
    submitted = controller.requests[0]
    assert isinstance(submitted, DeduplicationRequest)
    assert submitted.on_progress is not None
    tab.shutdown()


@requires_display
def test_webp_tab_submits_gui_progress_observer() -> None:
    controller = RecordingController(WebPDirectoryConversionResult((), ()))
    tab = WebPConversionGtkTab(controller)
    tab.build()

    tab._submit_with_results(
        WebPConversionGtkTab.create_request("/photos"),
        "Checking WebP conversions...",
    )
    deadline = time.monotonic() + 2
    while not controller.requests and time.monotonic() < deadline:
        time.sleep(0.005)

    assert controller.requests
    submitted = controller.requests[0]
    assert isinstance(submitted, WebPConversionRequest)
    assert submitted.on_progress is not None
    tab.shutdown()


@requires_display
def test_gui_appends_streamed_results_while_task_is_running() -> None:
    duplicate = Duplicate(
        Path("early.jpg"),
        Path("kept.jpg"),
        0,
        FileIdentity(1, 2, 1024, 3),
    )
    controller = RecordingController(DeduplicationResult((duplicate,), False))
    tab = DeduplicationGtkTab(controller)
    tab.build()
    tab._running = True

    assert tab._append_duplicate(duplicate) is GLib.SOURCE_REMOVE

    assert tab._running
    assert tab._result_model is not None
    assert tab._result_model.get_n_items() == 1
    assert tab._result_model.get_item(0).row.primary == "Would delete: early.jpg"
    assert tab._summary_label is not None
    assert (
        tab._summary_label.get_text()
        == "1 duplicate found | 1.0 KiB would be saved"
    )
    tab.shutdown()


@requires_display
def test_gui_appends_streamed_webp_results_while_task_is_running() -> None:
    conversion = WebPConversion(
        Path("early.png"),
        Path("early.webp"),
        original_size=2048,
        webp_size=512,
    )
    skip = WebPSkip(Path("small.png"), "WebP would not be smaller")
    controller = RecordingController(
        WebPDirectoryConversionResult((conversion,), (skip,))
    )
    tab = WebPConversionGtkTab(controller)
    tab.build()
    tab._running = True

    assert tab._append_result(conversion) is GLib.SOURCE_REMOVE
    assert tab._append_result(skip) is GLib.SOURCE_REMOVE

    assert tab._running
    assert tab._result_model is not None
    assert tab._result_model.get_n_items() == 2
    assert tab._result_model.get_item(0).row.primary == "Converted: early.png"
    assert tab._result_model.get_item(1).row.primary == "Skipped: small.png"
    assert tab._summary_label is not None
    assert (
        tab._summary_label.get_text()
        == "1 converted, 1 skipped | 1.5 KiB saved"
    )
    tab.shutdown()


@requires_display
@pytest.mark.parametrize("tool", ["duplicates", "webp"])
def test_large_result_stream_yields_to_input_and_keeps_rows_once(
    tool: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    count = 6000
    if tool == "duplicates":
        duplicates = tuple(
            Duplicate(Path(f"duplicate-{index}.png"), Path("keep.png"), 0,
                      FileIdentity(1, index, 1024, 0))
            for index in range(count)
        )

        def emit_duplicates(request: DeduplicationRequest) -> None:
            assert request.on_result is not None
            assert request.on_progress is not None
            for index, duplicate in enumerate(duplicates, 1):
                request.on_result(duplicate)
                request.on_progress(TaskProgress("Finding duplicates", index, count))

        controller = StreamingController(
            DeduplicationResult(duplicates, deleted=False), emit_duplicates,
        )
        tab = DeduplicationGtkTab(controller)
        request = DeduplicationGtkTab.create_request("/photos")
        expected_first = "Would delete: duplicate-0.png"
        expected_last = f"Would delete: duplicate-{count - 1}.png"
        expected_summary = "6000 duplicates found | 5.9 MiB would be saved"
    else:
        conversions = tuple(
            WebPConversion(Path(f"source-{index}.png"), Path(f"source-{index}.webp"),
                           2048, 1024)
            for index in range(count // 2)
        )
        skips = tuple(WebPSkip(Path(f"skip-{index}.png"), "Already smaller")
                      for index in range(count // 2))

        def emit_webp(request: WebPConversionRequest) -> None:
            assert request.on_result is not None
            assert request.on_progress is not None
            for index, result in enumerate((*conversions, *skips), 1):
                request.on_result(result)
                request.on_progress(TaskProgress("Checking conversions", index, count))

        controller = StreamingController(
            WebPDirectoryConversionResult(conversions, skips), emit_webp,
        )
        tab = WebPConversionGtkTab(controller)
        request = WebPConversionGtkTab.create_request("/photos")
        expected_first = "Converted: source-0.png"
        expected_last = f"Skipped: skip-{count // 2 - 1}.png"
        expected_summary = "3000 converted, 3000 skipped | 2.9 MiB saved"

    view = GtkGuiView(tab)
    view._install_styles()
    window = Gtk.Window(child=view._build_main_view())
    window.add_css_class("cleanup-window")
    window.set_default_size(980, 720)
    assert tab._result_model is not None
    assert tab._result_list is not None
    model = tab._result_model
    changes: list[tuple[int, int, int]] = []
    first_items: list[object] = []
    input_counts: list[int] = []
    heartbeat_counts: list[int] = []
    completion_waited: list[bool] = []
    progress_calls: list[TaskProgress] = []
    factory_widgets: list[object] = []
    input_button = Gtk.Button(label="Respond")
    input_button.connect("clicked", lambda *_: input_counts.append(model.get_n_items()))
    input_source: list[int | None] = [None]

    def input_callback() -> bool:
        input_source[0] = None
        input_button.emit("clicked")
        return GLib.SOURCE_REMOVE

    def changed(_model: object, position: int, removed: int, added: int) -> None:
        changes.append((position, removed, added))
        if added and not first_items:
            first_items.append(model.get_item(0))
            input_source[0] = GLib.idle_add(input_callback)

    def heartbeat() -> bool:
        rows = model.get_n_items()
        heartbeat_counts.append(rows)
        if controller.finished.is_set() and 0 < rows < count:
            completion_waited.append(tab._running)
        return GLib.SOURCE_CONTINUE

    original_apply_progress = tab._apply_progress

    def observe_progress(progress: TaskProgress) -> bool:
        progress_calls.append(progress)
        return original_apply_progress(progress)

    monkeypatch.setattr(tab, "_apply_progress", observe_progress)
    model.connect("items-changed", changed)
    factory = tab._result_list.get_factory()
    assert factory is not None
    factory.connect("setup", lambda _factory, item: factory_widgets.append(item))
    heartbeat_source = GLib.timeout_add(1, heartbeat)
    try:
        window.present()
        _process_gtk_until(lambda: window.get_width() == 980)
        if isinstance(tab, DeduplicationGtkTab):
            assert isinstance(request, DeduplicationRequest)
            tab._submit_with_results(request, "Streaming...")
        else:
            assert isinstance(request, WebPConversionRequest)
            tab._submit_with_results(request, "Streaming...")
        _process_gtk_until(lambda: not tab._running, timeout=15)

        assert not tab._running
        assert controller.finished.is_set()
        assert controller.thread_ids[0] != threading.current_thread().ident
        assert model.get_n_items() == count
        assert model.get_item(0) is first_items[0]
        assert model.get_item(0).row.primary == expected_first
        assert model.get_item(count - 1).row.primary == expected_last
        assert sum(added for _, _, added in changes) == count
        assert sum(removed for _, removed, _ in changes) == 0
        assert len(changes) < count // 4
        assert any(0 < rows < count for rows in heartbeat_counts)
        assert any(0 < rows < count for rows in input_counts)
        assert completion_waited and all(completion_waited)
        assert 0 < len(progress_calls) < count
        assert progress_calls[-1].completed == count
        assert len(factory_widgets) < count // 2
        assert tab._summary_label is not None
        assert tab._summary_label.get_text() == expected_summary
    finally:
        GLib.source_remove(heartbeat_source)
        if input_source[0] is not None:
            GLib.source_remove(input_source[0])
        window.destroy()
        view._on_shutdown()


@requires_display
def test_progress_burst_applies_only_the_latest_pending_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tab = DeduplicationGtkTab(RecordingController(DeduplicationResult((), False)))
    tab.build()
    tab._set_busy(True, "Working...")
    applied: list[TaskProgress] = []
    original_apply = tab._apply_progress

    def apply(progress: TaskProgress) -> bool:
        applied.append(progress)
        return original_apply(progress)

    monkeypatch.setattr(tab, "_apply_progress", apply)
    try:
        for index in range(1, 5001):
            tab._queue_progress(TaskProgress(f"Checking file {index}", index, 5000))
        assert applied == []
        _process_gtk_until(lambda: bool(applied))
        assert applied == [TaskProgress("Checking file 5000", 5000, 5000)]
        assert tab._progress_bar is not None
        assert tab._progress_bar.get_fraction() == 1.0
        assert tab._progress_bar.get_text() == "5000 of 5000 files"
    finally:
        tab.shutdown()


@requires_display
def test_error_waits_for_streamed_rows_and_next_run_replaces_them() -> None:
    partial = tuple(
        Duplicate(Path(f"partial-{index}.png"), Path("keep.png"), 0,
                  FileIdentity(1, index, 1024, 0))
        for index in range(300)
    )
    fresh = tuple(
        Duplicate(Path(f"fresh-{index}.png"), Path("keep.png"), 0,
                  FileIdentity(1, index, 1024, 0))
        for index in range(2)
    )

    def emit(request: DeduplicationRequest) -> None:
        assert request.on_result is not None
        rows = partial if len(controller.requests) == 1 else fresh
        for duplicate in rows:
            request.on_result(duplicate)
        if len(controller.requests) == 1:
            raise RuntimeError("stream failed after partial results")

    controller = StreamingController(DeduplicationResult(fresh, False), emit)
    tab = DeduplicationGtkTab(controller)
    tab.build()
    assert tab._result_model is not None
    model = tab._result_model
    request = DeduplicationGtkTab.create_request("/photos")
    try:
        tab._submit_with_results(request, "First run...")
        assert controller.finished.wait(2)
        assert tab._running
        assert model.get_n_items() == 0
        _process_gtk_until(lambda: not tab._running)
        assert not tab._running
        assert model.get_n_items() == len(partial)
        assert tab._error_label is not None
        assert tab._error_label.get_text() == "stream failed after partial results"
        assert tab._error_revealer is not None
        assert tab._error_revealer.get_reveal_child()

        tab._submit_with_results(request, "Second run...")
        _process_gtk_until(lambda: not tab._running)
        assert not tab._running
        assert len(controller.requests) == 2
        assert model.get_n_items() == len(fresh)
        assert [model.get_item(index).row.primary for index in range(len(fresh))] == [
            "Would delete: fresh-0.png", "Would delete: fresh-1.png",
        ]
        assert not tab._error_revealer.get_reveal_child()
        assert tab._summary_label is not None
        assert tab._summary_label.get_text() == "2 duplicates found | 2.0 KiB would be saved"
    finally:
        tab.shutdown()


@requires_display
def test_shutdown_releases_producer_waiting_on_a_full_result_queue() -> None:
    duplicate = Duplicate(Path("pending.png"), Path("keep.png"), 0,
                          FileIdentity(1, 2, 1024, 0))

    def emit(request: DeduplicationRequest) -> None:
        assert request.on_result is not None
        assert request.on_progress is not None
        for index in range(tab._MAX_PENDING_RESULTS + 100):
            request.on_result(duplicate)
            request.on_progress(TaskProgress("Pending", index, 10000))

    controller = StreamingController(DeduplicationResult((), False), emit)
    tab = DeduplicationGtkTab(controller)
    tab.build()
    try:
        tab._submit_with_results(
            DeduplicationGtkTab.create_request("/photos"), "Filling queue...",
        )
        assert controller.started.wait(2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with tab._updates:
                if len(tab._pending_results) == tab._MAX_PENDING_RESULTS:
                    break
            time.sleep(0.005)
        with tab._updates:
            assert len(tab._pending_results) == tab._MAX_PENDING_RESULTS
        assert not controller.finished.is_set()
        tab.shutdown()
        assert controller.finished.wait(2)
        tab._queue_duplicate(duplicate)
        tab._queue_progress(TaskProgress("Late update", 1, 1))
        _process_gtk_until(lambda: tab._completion is None)
        assert tab._closed
        assert not tab._pending_results
        assert tab._pending_progress is None
        assert tab._update_source is None
        assert tab._result_model is not None
        assert tab._result_model.get_n_items() == 0
    finally:
        tab.shutdown()


@requires_display
def test_realized_results_exit_cleanly_with_retained_global_references() -> None:
    script = textwrap.dedent("""\
        import faulthandler
        faulthandler.enable()
        from concurrent.futures import Future
        from pathlib import Path
        import resource
        import threading
        import time
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk
        from cleanup_cli.controllers import DeduplicationResult
        from cleanup_cli.models import Duplicate
        from cleanup_cli.models.filesystem import FileIdentity
        from cleanup_cli.views.gui import create_gui_view

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        count = 4000
        view = create_gui_view()
        view._theme.start()
        view._install_styles()
        main = view._build_main_view()
        window = Gtk.Window(child=main)
        window.add_css_class("cleanup-window")
        window.set_titlebar(view._build_header_bar())
        window.set_default_size(1100, 760)
        window.present()
        tab = view.tabs[0]
        tab._begin_results()
        tab._running = True
        context = GLib.MainContext.default()

        def settle(seconds):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                context.iteration(False)
                time.sleep(0.001)

        settle(0.25)
        identity = FileIdentity(0, 0, 1024, 0)
        results = tuple(
            Duplicate(Path(f"duplicate-{index}.png"), Path("keep.png"), 0, identity)
            for index in range(count)
        )
        heartbeats = []

        def beat():
            heartbeats.append(time.monotonic())
            return GLib.SOURCE_CONTINUE

        source = GLib.timeout_add(16, beat)
        future = Future()
        future.set_result(DeduplicationResult(results, False))

        def produce():
            for result in results:
                tab._queue_duplicate(result)
            tab._schedule_completion(future)

        producer = threading.Thread(target=produce)
        producer.start()
        deadline = time.monotonic() + 5
        while tab._running and time.monotonic() < deadline:
            context.iteration(False)
            time.sleep(0.001)
        assert not tab._running, "Streamed results did not complete"
        producer.join(timeout=1)
        assert not producer.is_alive()
        settle(0.25)
        counts = {"row_widgets": 0, "all_native_list_widgets": 0}

        def count_widgets(widget):
            counts["all_native_list_widgets"] += 1
            if type(widget).__name__ == "_ResultRowWidget":
                counts["row_widgets"] += 1
            child = widget.get_first_child()
            while child is not None:
                count_widgets(child)
                child = child.get_next_sibling()

        count_widgets(tab._result_list)
        assert 0 < counts["row_widgets"] < count
        assert tab._result_model.get_n_items() == count
        tab._streaming_results = True
        tab._render_result(DeduplicationResult(results, False))
        settle(0.25)
        GLib.source_remove(source)
        window.destroy()
        view._on_shutdown()
        assert tab._result_model.get_n_items() == count
        print("shutdown completed", flush=True)
        """)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "shutdown completed" in completed.stdout
