"""GTK tabs for the cleanup application's production workflows."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from itertools import chain
from pathlib import Path

import gi  # pyright: ignore[reportMissingImports]

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402  # pyright: ignore[reportMissingImports, reportAttributeAccessIssue]

from cleanup_cli.controllers.core import (
    Controller,
    DeduplicationRequest,
    DeduplicationResult,
    WebPConversionRequest,
)
from cleanup_cli.models.deduplication import DeduplicationOptions, Duplicate
from cleanup_cli.models.image.signatures import PHASH_BITS
from cleanup_cli.models.image.webp import (
    WebPConversion,
    WebPDirectoryConversionResult,
    WebPOptions,
    WebPResult,
    WebPSkip,
)
from cleanup_cli.models.validation import validate_inclusive_range
from cleanup_cli.views.gui.application import (
    ControllerGtkTab,
    OptionalNumberControl,
    choose_folder,
    confirm_destructive_action,
    result_row,
    setting_row,
    settings_group,
)
from cleanup_cli.views.gui.results import ResultRow


def _directory_path(directory: str | Path) -> Path:
    path_text = str(directory).strip()
    if not path_text:
        raise ValueError("select a directory")
    return Path(path_text).expanduser()


def _add_directory_setting(
    form: Gtk.Box,
    *,
    input_purpose: Gtk.InputPurpose | None = None,
) -> Gtk.Entry:
    directory = Gtk.Entry(
        placeholder_text="Select an image directory",
        primary_icon_name="folder-symbolic",
        hexpand=True,
    )
    directory.update_property([Gtk.AccessibleProperty.LABEL], ["Image folder"])
    if input_purpose is not None:
        directory.set_input_purpose(input_purpose)

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
    form.append(
        settings_group(
            "Image folder",
            setting_row(
                "", directory_box, "Includes images in subfolders.", stacked=True
            ),
        )
    )
    return directory


def _add_resource_settings(
    form: Gtk.Box,
) -> tuple[OptionalNumberControl, OptionalNumberControl]:
    workers = OptionalNumberControl(minimum=1, maximum=1024, value=4)
    memory = OptionalNumberControl(
        minimum=1,
        maximum=1048576,
        value=512,
        unit="MiB",
    )
    form.append(
        settings_group(
            "Resources",
            setting_row("Workers", workers.widget),
            setting_row("Memory", memory.widget),
        )
    )
    return workers, memory


class DeduplicationGtkTab(
    ControllerGtkTab[DeduplicationRequest, DeduplicationResult, Duplicate]
):
    """GUI workflow for finding and optionally deleting duplicate images."""

    title = "Duplicate Images"
    icon_name = "edit-find-symbolic"
    description = "Find matching images and reclaim space in your library."
    empty_title = "Make room for the originals"
    empty_description = (
        "See which images match, which copy to keep, and how much space you could save."
    )
    empty_steps = (
        "Choose a folder containing your images.",
        "Start with threshold 0 for the strictest matching.",
        "Review the results before enabling deletion.",
    )

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
        directory_path = _directory_path(directory)
        validate_inclusive_range(
            "threshold",
            threshold,
            minimum=0,
            maximum=PHASH_BITS,
        )
        return DeduplicationRequest(
            directory_path,
            DeduplicationOptions(
                threshold=threshold,
                delete=delete,
                max_workers=max_workers,
                memory_limit_mb=memory_limit_mb,
            ),
        )

    def build(self) -> Gtk.Widget:
        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)

        self._directory = _add_directory_setting(
            form,
            input_purpose=Gtk.InputPurpose.FREE_FORM,
        )

        threshold = Gtk.SpinButton.new_with_range(0, 256, 1)
        threshold.set_value(0)
        threshold.set_numeric(True)
        self._threshold = threshold
        delete = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._delete = delete
        delete.connect("notify::active", self._sync_mode)
        form.append(
            settings_group(
                "Matching & cleanup",
                setting_row(
                    "Similarity threshold",
                    threshold,
                    "0 is strictest. Higher values allow more differences.",
                ),
                setting_row(
                    "Delete duplicates",
                    self._delete,
                    "Permanently remove matches after confirmation.",
                ),
            )
        )

        self._workers, self._memory = _add_resource_settings(form)

        run_button = Gtk.Button(label="Find Duplicates")
        run_button.add_css_class("suggested-action")
        run_button.connect("clicked", self._on_run)
        self._run_button = run_button
        page = self._page(form)
        self._sync_mode()
        return page

    def _sync_mode(self, *_args: object) -> None:
        destructive = self._delete is not None and self._delete.get_active()
        self._update_mode(
            destructive,
            "Deletion enabled",
            "Matched files will be deleted after confirmation."
            if destructive else "Preview only. Your original files stay in place.",
        )
        if self._run_button is not None:
            self._run_button.set_label(
                "Find & Delete" if destructive else "Find Duplicates"
            )

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
        if self._running or self._closed:
            return
        self._begin_results()
        self._streamed_count = 0
        self._streamed_saved_bytes = 0
        self._submit(
            replace(
                request,
                on_result=self._queue_streamed_result,
                on_progress=self._queue_progress,
            ),
            activity,
        )

    def _append_result_batch(self, results: Sequence[Duplicate]) -> None:
        deleted = self._delete.get_active() if self._delete is not None else False
        action = "Deleted" if deleted else "Would delete"
        self._append_rows([
            self._duplicate_row(duplicate, deleted, action) for duplicate in results
        ])
        self._streamed_count += len(results)
        self._streamed_saved_bytes += sum(duplicate.saved_bytes for duplicate in results)
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

        action = "Deleted" if result.deleted else "Would delete"
        if not self._has_streamed_results(len(result.duplicates)):
            self._replace_result_rows(
                self._duplicate_row(duplicate, result.deleted, action)
                for duplicate in result.duplicates
            )
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
    ) -> ResultRow:
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
    ControllerGtkTab[WebPConversionRequest, WebPDirectoryConversionResult, WebPResult]
):
    """GUI workflow for validating or replacing images with WebP files."""

    title = "WebP Conversion"
    icon_name = "image-x-generic-symbolic"
    description = "Find smaller WebP versions of your images without changing dimensions."
    empty_title = "Smaller files, more space"
    empty_description = (
        "Check which images can become smaller WebP files before replacing originals."
    )
    empty_steps = (
        "Choose a folder containing your images.",
        "Set the quality. Higher values preserve more detail.",
        "Check the results, then enable replacement when ready.",
    )

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
        return WebPConversionRequest(
            _directory_path(directory),
            WebPOptions(
                quality=quality,
                replace=replace,
                max_workers=max_workers,
                memory_limit_mb=memory_limit_mb,
            ),
        )

    def build(self) -> Gtk.Widget:
        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)

        self._directory = _add_directory_setting(form)

        quality = Gtk.SpinButton.new_with_range(0, 100, 1)
        quality.set_value(80)
        quality.set_numeric(True)
        self._quality = quality
        replace_originals = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._replace = replace_originals
        replace_originals.connect("notify::active", self._sync_mode)
        form.append(
            settings_group(
                "Conversion",
                setting_row(
                    "Quality", quality, "0–100. Higher values preserve more detail."
                ),
                setting_row(
                    "Replace originals",
                    self._replace,
                    "Only replace a source when its WebP is smaller and validated.",
                ),
            )
        )

        self._workers, self._memory = _add_resource_settings(form)

        run_button = Gtk.Button(label="Check Images")
        run_button.add_css_class("suggested-action")
        run_button.connect("clicked", self._on_run)
        self._run_button = run_button
        page = self._page(form)
        self._sync_mode()
        return page

    def _sync_mode(self, *_args: object) -> None:
        destructive = self._replace is not None and self._replace.get_active()
        self._update_mode(
            destructive,
            "Replacement enabled",
            "Smaller, validated WebP files will replace originals after confirmation."
            if destructive else "Preview only. No output files are kept.",
        )
        if self._run_button is not None:
            self._run_button.set_label(
                "Convert & Replace" if destructive else "Check Images"
            )

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
        if self._running or self._closed:
            return
        self._begin_results()
        self._streamed_conversions = 0
        self._streamed_skips = 0
        self._streamed_saved_bytes = 0
        self._submit(
            replace(
                request,
                on_result=self._queue_streamed_result,
                on_progress=self._queue_progress,
            ),
            activity,
        )

    def _append_result_batch(self, results: Sequence[WebPResult]) -> None:
        rows = []
        for result in results:
            if isinstance(result, WebPConversion):
                rows.append(self._conversion_row(result))
                self._streamed_conversions += 1
                self._streamed_saved_bytes += result.saved_bytes
            elif isinstance(result, WebPSkip):
                rows.append(self._skip_row(result))
                self._streamed_skips += 1
        self._append_rows(rows)
        self._set_summary(
            "checkmark-symbolic",
            self._summary_text(
                self._streamed_conversions,
                self._streamed_skips,
                self._streamed_saved_bytes,
            ),
        )

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

        if not self._has_streamed_results(len(result.conversions) + len(result.skips)):
            self._replace_result_rows(chain(
                (self._conversion_row(conversion) for conversion in result.conversions),
                (self._skip_row(skip) for skip in result.skips),
            ))
        self._set_summary(
            "checkmark-symbolic",
            self._summary_text(
                len(result.conversions),
                len(result.skips),
                result.total_saved_bytes,
            ),
        )

    @staticmethod
    def _conversion_row(conversion: WebPConversion) -> ResultRow:
        return result_row(
            "image-x-generic-symbolic",
            f"Converted: {conversion.source}",
            f"Output: {conversion.destination} | "
            f"Saved: {_format_bytes(conversion.saved_bytes)}",
        )

    @staticmethod
    def _skip_row(skip: WebPSkip) -> ResultRow:
        reason = skip.reason
        if reason == "replacement not enabled (use --replace)":
            reason = "Enable Replace originals to apply this conversion."
        return result_row(
            "dialog-information-symbolic",
            f"Skipped: {skip.path}",
            reason,
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
