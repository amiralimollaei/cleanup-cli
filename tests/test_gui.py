from __future__ import annotations

from pathlib import Path
import threading
import time
from typing import Generic, TypeVar

import gi  # pyright: ignore[reportMissingImports]
import pytest

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402  # pyright: ignore[reportMissingImports, reportAttributeAccessIssue]

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
GTK_AVAILABLE = bool(Gtk.init_check())
requires_display = pytest.mark.skipif(
    not GTK_AVAILABLE,
    reason="GTK display is not available",
)


class RecordingController(Controller[RequestT, ResultT], Generic[RequestT, ResultT]):
    def __init__(self, result: ResultT) -> None:
        self.result = result
        self.requests: list[RequestT] = []
        self.thread_ids: list[int | None] = []

    def execute(self, request: RequestT) -> ResultT:
        self.requests.append(request)
        self.thread_ids.append(threading.current_thread().ident)
        return self.result


class StaticTab:
    icon_name = "applications-system-symbolic"

    def __init__(self, title: str) -> None:
        self.title = title
        self.build_count = 0

    def build(self) -> Gtk.Widget:
        self.build_count += 1
        return Gtk.Label(label=self.title)


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
def test_gui_main_view_accepts_any_number_of_tabs() -> None:
    tabs = tuple(StaticTab(f"Tool {index}") for index in range(3))
    view = GtkGuiView(
        *tabs,
        application_id="io.github.amiralimollaei.CleanupCli.TabTest",
    )

    main = view._build_main_view()

    assert view.tabs == tabs
    assert isinstance(main, Gtk.Paned)
    stack = main.get_end_child()
    assert isinstance(stack, Gtk.Stack)
    assert stack.get_pages().get_n_items() == 3
    assert [tab.build_count for tab in tabs] == [1, 1, 1]
    # A custom tab does not need a shutdown hook.
    view._on_shutdown()


@requires_display
def test_gui_main_view_supports_no_tabs() -> None:
    view = GtkGuiView(
        application_id="io.github.amiralimollaei.CleanupCli.EmptyTest"
    )

    assert view.tabs == ()
    assert isinstance(view._build_main_view(), Gtk.Box)


@requires_display
def test_gui_header_title_has_vertical_padding() -> None:
    view = GtkGuiView(
        application_id="io.github.amiralimollaei.CleanupCli.HeaderTest"
    )

    heading = view._build_header_bar().get_title_widget()

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
    assert isinstance(view._build_main_view(), Gtk.Paned)
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
    assert tab._result_list is not None
    assert tab._result_list.get_first_child() is not None
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
    assert tab._result_list is not None
    first = tab._result_list.get_first_child()
    assert first is not None
    assert first.get_next_sibling() is not None
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
    assert not tab._activity_box.get_visible()

    tab._set_busy(True, "Working...")
    assert tab._activity_box.get_visible()
    assert tab._progress_bar.get_fraction() == 0.0
    assert tab._progress_bar.get_text() == "Preparing..."

    assert tab._apply_progress(TaskProgress("Indexing images", 2, 4)) is GLib.SOURCE_REMOVE
    assert tab._activity_label is not None
    assert tab._activity_label.get_text() == "Indexing images"
    assert tab._progress_bar.get_fraction() == pytest.approx(0.5)
    assert tab._progress_bar.get_text() == "2 of 4 files"

    tab._set_busy(False, "")
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
    assert tab._result_list is not None
    assert tab._result_list.get_first_child() is not None
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
    assert tab._result_list is not None
    first = tab._result_list.get_first_child()
    assert first is not None
    assert first.get_next_sibling() is not None
    assert tab._summary_label is not None
    assert (
        tab._summary_label.get_text()
        == "1 converted, 1 skipped | 1.5 KiB saved"
    )
    tab.shutdown()