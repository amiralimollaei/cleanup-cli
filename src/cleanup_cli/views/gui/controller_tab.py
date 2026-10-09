"""Controller-backed GTK pages with asynchronous execution and batched results."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from itertools import chain, islice
from threading import Condition
from typing import Generic, TypeVar

import gi  # pyright: ignore[reportMissingImports]

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk, Pango  # noqa: E402  # pyright: ignore[reportMissingImports, reportAttributeAccessIssue]

from cleanup_cli.controllers.core import Controller
from cleanup_cli.models.progress import TaskProgress
from cleanup_cli.views.gui.layout import ResponsivePaned, WorkspaceLayout
from cleanup_cli.views.gui.results import ResultList, ResultRow
from cleanup_cli.views.gui.widgets import empty_state, set_margins


RequestT = TypeVar("RequestT")
ResultT = TypeVar("ResultT")
StreamedT = TypeVar("StreamedT")


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
        set_margins(page, 20)

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
        set_margins(result_header, 16)
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
            empty_state(
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
            empty_state(icon_name, title, description, scroll=True),
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
