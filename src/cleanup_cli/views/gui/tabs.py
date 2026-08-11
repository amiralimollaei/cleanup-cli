"""GTK tabs for the cleanup application's production workflows."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import gi  # pyright: ignore[reportMissingImports]

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402  # pyright: ignore[reportMissingImports, reportAttributeAccessIssue]

from ...controllers import (
    Controller,
    DeduplicationRequest,
    DeduplicationResult,
    WebPConversionRequest,
)
from ...models import (
    DeduplicationOptions,
    Duplicate,
    WebPConversion,
    WebPDirectoryConversionResult,
    WebPOptions,
    WebPResult,
    WebPSkip,
)
from ...models.image.signatures import PHASH_BITS
from ...models.validation import validate_inclusive_range
from .application import (
    ControllerGtkTab,
    OptionalNumberControl,
    add_form_row,
    choose_folder,
    confirm_destructive_action,
    form_grid,
    result_row,
)


class DeduplicationGtkTab(
    ControllerGtkTab[DeduplicationRequest, DeduplicationResult]
):
    """GUI workflow for finding and optionally deleting duplicate images."""

    title = "Duplicate Images"
    icon_name = "edit-find-symbolic"

    def __init__(
        self,
        controller: Controller[DeduplicationRequest, DeduplicationResult],
    ) -> None:
        super().__init__(controller)
        self._directory: Gtk.Entry | None = None
        self._threshold: Gtk.SpinButton | None = None
        self._workers: OptionalNumberControl | None = None
        self._memory: OptionalNumberControl | None = None
        self._delete: Gtk.Switch | None = None
        self._streamed_count = 0
        self._streamed_saved_bytes = 0

    @staticmethod
    def create_request(
        directory: str | Path,
        *,
        threshold: int = 0,
        delete: bool = False,
        max_workers: int | None = None,
        memory_limit_mb: int | None = None,
    ) -> DeduplicationRequest:
        path_text = str(directory).strip()
        if not path_text:
            raise ValueError("select a directory")
        validate_inclusive_range(
            "threshold",
            threshold,
            minimum=0,
            maximum=PHASH_BITS,
        )
        return DeduplicationRequest(
            Path(path_text).expanduser(),
            DeduplicationOptions(
                threshold=threshold,
                delete=delete,
                max_workers=max_workers,
                memory_limit_mb=memory_limit_mb,
            ),
        )

    def build(self) -> Gtk.Widget:
        grid = form_grid()

        directory = Gtk.Entry(
            placeholder_text="Select an image directory",
            primary_icon_name="folder-symbolic",
        )
        directory.set_input_purpose(Gtk.InputPurpose.FREE_FORM)
        self._directory = directory
        browse = Gtk.Button(
            icon_name="folder-open-symbolic",
            tooltip_text="Choose a directory",
        )
        browse.connect(
            "clicked",
            lambda *_: choose_folder(directory, "Choose Image Directory"),
        )
        directory_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        directory_box.append(directory)
        directory_box.append(browse)
        add_form_row(grid, 0, "Directory", directory_box)

        threshold = Gtk.SpinButton.new_with_range(0, 256, 1)
        threshold.set_value(0)
        threshold.set_numeric(True)
        self._threshold = threshold
        add_form_row(grid, 1, "Similarity threshold", threshold)

        self._workers = OptionalNumberControl(minimum=1, maximum=1024, value=4)
        add_form_row(grid, 2, "Workers", self._workers.widget)

        self._memory = OptionalNumberControl(
            minimum=1,
            maximum=1048576,
            value=512,
            unit="MiB",
        )
        add_form_row(grid, 3, "Memory limit", self._memory.widget)

        self._delete = Gtk.Switch(halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        add_form_row(grid, 4, "Delete duplicates", self._delete)

        run_button = Gtk.Button(
            label="Find Duplicates",
            icon_name="edit-find-symbolic",
            halign=Gtk.Align.END,
        )
        run_button.add_css_class("suggested-action")
        run_button.connect("clicked", self._on_run)
        self._run_button = run_button
        grid.attach(run_button, 1, 5, 1, 1)
        return self._page(grid)

    def _on_run(self, *_args: object) -> None:
        try:
            request = self._request_from_form()
        except ValueError as error:
            self._show_error(str(error))
            return

        if request.options.delete and self._run_button is not None:
            confirm_destructive_action(
                self._run_button,
                heading="Delete duplicate images?",
                body=(
                    "Files selected as duplicates will be permanently deleted. "
                    "This action cannot be undone."
                ),
                action_label="Delete",
                callback=lambda: self._submit_with_results(
                    request,
                    "Finding and deleting duplicates...",
                ),
            )
            return
        self._submit_with_results(request, "Finding duplicate images...")

    def _submit_with_results(
        self,
        request: DeduplicationRequest,
        activity: str,
    ) -> None:
        self._prepare_results()
        self._streamed_count = 0
        self._streamed_saved_bytes = 0
        self._submit(
            replace(
                request,
                on_result=self._queue_duplicate,
                on_progress=self._queue_progress,
            ),
            activity,
        )

    def _queue_duplicate(self, duplicate: Duplicate) -> None:
        GLib.idle_add(self._append_duplicate, duplicate)

    def _append_duplicate(self, duplicate: Duplicate) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        if self._result_list is None:
            raise RuntimeError("tab has not been built")
        deleted = self._delete.get_active() if self._delete is not None else False
        action = "Deleted" if deleted else "Would delete"
        self._result_list.append(self._duplicate_row(duplicate, deleted, action))
        self._streamed_count += 1
        self._streamed_saved_bytes += duplicate.saved_bytes
        status = "deleted" if deleted else "found"
        self._set_summary(
            "checkmark-symbolic",
            self._summary_text(
                self._streamed_count,
                status,
                self._streamed_saved_bytes,
                deleted,
            ),
        )
        return GLib.SOURCE_REMOVE

    def _request_from_form(self) -> DeduplicationRequest:
        if any(
            widget is None
            for widget in (
                self._directory,
                self._threshold,
                self._workers,
                self._memory,
                self._delete,
            )
        ):
            raise RuntimeError("tab has not been built")
        assert self._directory is not None
        assert self._threshold is not None
        assert self._workers is not None
        assert self._memory is not None
        assert self._delete is not None
        return self.create_request(
            self._directory.get_text(),
            threshold=self._threshold.get_value_as_int(),
            delete=self._delete.get_active(),
            max_workers=self._workers.value,
            memory_limit_mb=self._memory.value,
        )

    def _render_result(self, result: DeduplicationResult) -> None:
        if not result.duplicates:
            self._show_empty_result(
                "checkmark-symbolic",
                "No duplicate images found",
                "No files matched the selected similarity threshold.",
            )
            self._set_summary(
                "checkmark-symbolic",
                "0 duplicates found | 0 bytes would be saved",
            )
            return

        result_list = self._prepare_results()
        action = "Deleted" if result.deleted else "Would delete"
        for duplicate in result.duplicates:
            result_list.append(self._duplicate_row(duplicate, result.deleted, action))
        status = "deleted" if result.deleted else "found"
        count = len(result.duplicates)
        self._set_summary(
            "checkmark-symbolic",
            self._summary_text(
                count,
                status,
                result.total_saved_bytes,
                result.deleted,
            ),
        )

    @staticmethod
    def _duplicate_row(
        duplicate: Duplicate,
        deleted: bool,
        action: str,
    ) -> Gtk.Widget:
        savings = "Saved" if deleted else "Would save"
        return result_row(
            "edit-delete-symbolic" if deleted else "edit-find-symbolic",
            f"{action}: {duplicate.removed}",
            f"Keep: {duplicate.kept} | Distance: {duplicate.distance} | "
            f"{savings}: {_format_bytes(duplicate.saved_bytes)}",
        )

    @staticmethod
    def _summary_text(
        count: int,
        status: str,
        saved_bytes: int,
        deleted: bool,
    ) -> str:
        savings = "saved" if deleted else "would be saved"
        return (
            f"{count} duplicate{'s' if count != 1 else ''} {status} | "
            f"{_format_bytes(saved_bytes)} {savings}"
        )


class WebPConversionGtkTab(
    ControllerGtkTab[WebPConversionRequest, WebPDirectoryConversionResult]
):
    """GUI workflow for validating or replacing images with WebP files."""

    title = "WebP Conversion"
    icon_name = "image-x-generic-symbolic"

    def __init__(
        self,
        controller: Controller[WebPConversionRequest, WebPDirectoryConversionResult],
    ) -> None:
        super().__init__(controller)
        self._directory: Gtk.Entry | None = None
        self._quality: Gtk.SpinButton | None = None
        self._workers: OptionalNumberControl | None = None
        self._memory: OptionalNumberControl | None = None
        self._replace: Gtk.Switch | None = None
        self._streamed_conversions = 0
        self._streamed_skips = 0
        self._streamed_saved_bytes = 0

    @staticmethod
    def create_request(
        directory: str | Path,
        *,
        quality: int = 80,
        replace: bool = False,
        max_workers: int | None = None,
        memory_limit_mb: int | None = None,
    ) -> WebPConversionRequest:
        path_text = str(directory).strip()
        if not path_text:
            raise ValueError("select a directory")
        return WebPConversionRequest(
            Path(path_text).expanduser(),
            WebPOptions(
                quality=quality,
                replace=replace,
                max_workers=max_workers,
                memory_limit_mb=memory_limit_mb,
            ),
        )

    def build(self) -> Gtk.Widget:
        grid = form_grid()

        directory = Gtk.Entry(
            placeholder_text="Select an image directory",
            primary_icon_name="folder-symbolic",
        )
        self._directory = directory
        browse = Gtk.Button(
            icon_name="folder-open-symbolic",
            tooltip_text="Choose a directory",
        )
        browse.connect(
            "clicked",
            lambda *_: choose_folder(directory, "Choose Image Directory"),
        )
        directory_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        directory_box.append(directory)
        directory_box.append(browse)
        add_form_row(grid, 0, "Directory", directory_box)

        quality = Gtk.SpinButton.new_with_range(0, 100, 1)
        quality.set_value(80)
        quality.set_numeric(True)
        self._quality = quality
        add_form_row(grid, 1, "Quality", quality)

        self._workers = OptionalNumberControl(minimum=1, maximum=1024, value=4)
        add_form_row(grid, 2, "Workers", self._workers.widget)

        self._memory = OptionalNumberControl(
            minimum=1,
            maximum=1048576,
            value=512,
            unit="MiB",
        )
        add_form_row(grid, 3, "Memory limit", self._memory.widget)

        self._replace = Gtk.Switch(halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        add_form_row(grid, 4, "Replace originals", self._replace)

        run_button = Gtk.Button(
            label="Convert Images",
            icon_name="media-playback-start-symbolic",
            halign=Gtk.Align.END,
        )
        run_button.add_css_class("suggested-action")
        run_button.connect("clicked", self._on_run)
        self._run_button = run_button
        grid.attach(run_button, 1, 5, 1, 1)
        return self._page(grid)

    def _on_run(self, *_args: object) -> None:
        try:
            request = self._request_from_form()
        except ValueError as error:
            self._show_error(str(error))
            return

        if request.options.replace and self._run_button is not None:
            confirm_destructive_action(
                self._run_button,
                heading="Replace original images?",
                body=(
                    "Each source will be deleted after a smaller WebP file has "
                    "been validated. This action cannot be undone."
                ),
                action_label="Replace",
                callback=lambda: self._submit_with_results(
                    request,
                    "Converting images to WebP...",
                ),
            )
            return
        self._submit_with_results(request, "Checking WebP conversions...")

    def _submit_with_results(
        self,
        request: WebPConversionRequest,
        activity: str,
    ) -> None:
        self._prepare_results()
        self._streamed_conversions = 0
        self._streamed_skips = 0
        self._streamed_saved_bytes = 0
        self._submit(
            replace(
                request,
                on_result=self._queue_result,
                on_progress=self._queue_progress,
            ),
            activity,
        )

    def _queue_result(self, result: WebPResult) -> None:
        GLib.idle_add(self._append_result, result)

    def _append_result(self, result: WebPResult) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        if self._result_list is None:
            raise RuntimeError("tab has not been built")
        if isinstance(result, WebPConversion):
            self._result_list.append(self._conversion_row(result))
            self._streamed_conversions += 1
            self._streamed_saved_bytes += result.saved_bytes
        elif isinstance(result, WebPSkip):
            self._result_list.append(self._skip_row(result))
            self._streamed_skips += 1
        self._set_summary(
            "checkmark-symbolic",
            self._summary_text(
                self._streamed_conversions,
                self._streamed_skips,
                self._streamed_saved_bytes,
            ),
        )
        return GLib.SOURCE_REMOVE

    def _request_from_form(self) -> WebPConversionRequest:
        if any(
            widget is None
            for widget in (
                self._directory,
                self._quality,
                self._workers,
                self._memory,
                self._replace,
            )
        ):
            raise RuntimeError("tab has not been built")
        assert self._directory is not None
        assert self._quality is not None
        assert self._workers is not None
        assert self._memory is not None
        assert self._replace is not None
        return self.create_request(
            self._directory.get_text(),
            quality=self._quality.get_value_as_int(),
            replace=self._replace.get_active(),
            max_workers=self._workers.value,
            memory_limit_mb=self._memory.value,
        )

    def _render_result(self, result: WebPDirectoryConversionResult) -> None:
        if not result.conversions and not result.skips:
            self._show_empty_result(
                "checkmark-symbolic",
                "No convertible images found",
                "The directory contained no supported images requiring work.",
            )
            self._set_summary(
                "checkmark-symbolic",
                "0 converted, 0 skipped | 0 bytes saved",
            )
            return

        result_list = self._prepare_results()
        for conversion in result.conversions:
            result_list.append(self._conversion_row(conversion))
        for skip in result.skips:
            result_list.append(self._skip_row(skip))
        self._set_summary(
            "checkmark-symbolic",
            self._summary_text(
                len(result.conversions),
                len(result.skips),
                result.total_saved_bytes,
            ),
        )

    @staticmethod
    def _conversion_row(conversion: WebPConversion) -> Gtk.Widget:
        return result_row(
            "image-x-generic-symbolic",
            f"Converted: {conversion.source}",
            f"Output: {conversion.destination} | "
            f"Saved: {_format_bytes(conversion.saved_bytes)}",
        )

    @staticmethod
    def _skip_row(skip: WebPSkip) -> Gtk.Widget:
        return result_row(
            "dialog-information-symbolic",
            f"Skipped: {skip.path}",
            skip.reason,
        )

    @staticmethod
    def _summary_text(converted: int, skipped: int, saved_bytes: int) -> str:
        return (
            f"{converted} converted, {skipped} skipped | "
            f"{_format_bytes(saved_bytes)} saved"
        )


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KiB", "MiB", "GiB"):
        if abs(value) < 1024 or unit == "GiB":
            if unit == "bytes":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")