from __future__ import annotations

from collections.abc import Callable
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
from cleanup_cli.views.gui.dialogs import confirm_destructive_action


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


def test_destructive_confirmation_defaults_to_cancel(
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

    assert alert.cancel_button == 0
    assert alert.buttons[alert.cancel_button] == "Cancel"
    assert alert.default_button == alert.cancel_button


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
        split = settings_scroll.get_ancestor(Gtk.Paned)
        assert isinstance(split, Gtk.Paned)
        assert results_scroll.get_ancestor(Gtk.Paned) is split
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
            previous_size = (window.get_width(), window.get_height())
            window.set_default_size(width, height)
            _process_gtk_until(
                lambda: (window.get_width(), window.get_height()) != previous_size
                and split.get_orientation() == expected_orientation
            )
            assert (window.get_width(), window.get_height()) != previous_size
            assert split.get_orientation() == expected_orientation
            assert page.measure(Gtk.Orientation.HORIZONTAL, -1).minimum <= window.get_width()
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
            assert entry_bounds_available
            assert settings_scroll.get_height() >= entry_bounds.get_height()
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
        _process_gtk_until(lambda: splits[0].get_width() >= 1000)
        assert splits[0].get_width() >= 1000
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
            lambda: splits[1].get_position() < first_position
        )
        assert splits[1].get_position() < first_position
        assert view._layout.positions[Gtk.Orientation.HORIZONTAL] == first_position
        stack.set_visible_child_name("tab-0")
        _process_gtk_until(lambda: splits[0].get_position() == splits[1].get_position())
        assert splits[0].get_position() == splits[1].get_position()
        window.set_default_size(1100, 760)
        _process_gtk_until(
            lambda: splits[0].get_position() == first_position
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

        window.set_default_size(620, 720)
        _process_gtk_until(
            lambda: splits[1].get_orientation() == Gtk.Orientation.VERTICAL
        )
        default_vertical_position = splits[1].get_position()
        splits[1].set_position(280)
        _process_gtk_until(lambda: settings[1].get_height() >= 280)
        stack.set_visible_child_name("tab-0")
        _process_gtk_until(
            lambda: splits[0].get_orientation() == Gtk.Orientation.VERTICAL
        )
        assert splits[0].get_position() == splits[1].get_position() == 280
        reset.activate(None)
        _process_gtk_until(lambda: splits[0].get_position() == default_vertical_position)
        assert [split.get_position() for split in splits] == [
            default_vertical_position,
            default_vertical_position,
        ]

        webp_tab = tabs[1]
        assert isinstance(webp_tab, WebPConversionGtkTab)
        assert webp_tab._replace is not None
        webp_tab._replace.set_active(True)
        previous_width = window.get_width()
        window.set_default_size(560, 720)
        _process_gtk_until(lambda: window.get_width() < previous_width)
        assert window.get_width() < previous_width
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
    with pytest.raises(ValueError, match="select a directory"):
        tab._request_from_form()
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
    with pytest.raises(ValueError, match="select a directory"):
        tab._request_from_form()
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

    tab._apply_progress(TaskProgress("Indexing images", 2, 4))
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
    result_list = tab._result_list
    assert result_list is not None
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
    factory = result_list.get_factory()
    assert factory is not None
    factory.connect("setup", lambda _factory, item: factory_widgets.append(item))
    heartbeat_source = GLib.timeout_add(1, heartbeat)
    try:
        window.present()
        _process_gtk_until(lambda: result_list.get_width() > 0)
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
        tab._queue_streamed_result(duplicate)
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
                tab._queue_streamed_result(result)
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
