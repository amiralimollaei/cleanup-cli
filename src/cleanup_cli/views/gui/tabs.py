"""GTK tabs for the cleanup application's production workflows."""

from __future__ import annotations

from pathlib import Path

import gi  # pyright: ignore[reportMissingImports]

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402  # pyright: ignore[reportMissingImports, reportAttributeAccessIssue]

from ...controllers import (
    Controller,
    DeduplicationRequest,
    DeduplicationResult,
    WebPConversionRequest,
)
from ...models import DeduplicationOptions, WebPDirectoryConversionResult, WebPOptions
from .application import (
    ControllerGtkTab,
    add_form_row,
    automatic_number_control,
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
        self._automatic_workers: Gtk.CheckButton | None = None
        self._workers: Gtk.SpinButton | None = None
        self._automatic_memory: Gtk.CheckButton | None = None
        self._memory: Gtk.SpinButton | None = None
        self._delete: Gtk.Switch | None = None

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

        threshold = Gtk.SpinButton.new_with_range(0, 64, 1)
        threshold.set_value(0)
        threshold.set_numeric(True)
        self._threshold = threshold
        add_form_row(grid, 1, "Similarity threshold", threshold)

        workers_box, self._automatic_workers, self._workers = (
            automatic_number_control(minimum=1, maximum=1024, value=4)
        )
        add_form_row(grid, 2, "Workers", workers_box)

        memory_box, self._automatic_memory, self._memory = automatic_number_control(
            minimum=1,
            maximum=1048576,
            value=512,
            unit="MiB",
        )
        add_form_row(grid, 3, "Memory limit", memory_box)

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
                callback=lambda: self._submit(request, "Finding and deleting duplicates..."),
            )
            return
        self._submit(request, "Finding duplicate images...")

    def _request_from_form(self) -> DeduplicationRequest:
        if any(
            widget is None
            for widget in (
                self._directory,
                self._threshold,
                self._automatic_workers,
                self._workers,
                self._automatic_memory,
                self._memory,
                self._delete,
            )
        ):
            raise RuntimeError("tab has not been built")
        assert self._directory is not None
        assert self._threshold is not None
        assert self._automatic_workers is not None
        assert self._workers is not None
        assert self._automatic_memory is not None
        assert self._memory is not None
        assert self._delete is not None
        workers = (
            None
            if self._automatic_workers.get_active()
            else self._workers.get_value_as_int()
        )
        memory = (
            None
            if self._automatic_memory.get_active()
            else self._memory.get_value_as_int()
        )
        return self.create_request(
            self._directory.get_text(),
            threshold=self._threshold.get_value_as_int(),
            delete=self._delete.get_active(),
            max_workers=workers,
            memory_limit_mb=memory,
        )

    def _render_result(self, result: DeduplicationResult) -> None:
        if not result.duplicates:
            self._show_empty_result(
                "checkmark-symbolic",
                "No duplicate images found",
                "No files matched the selected similarity threshold.",
            )
            self._set_summary("checkmark-symbolic", "0 duplicates found")
            return

        result_list = self._prepare_results()
        action = "Deleted" if result.deleted else "Would delete"
        for duplicate in result.duplicates:
            result_list.append(
                result_row(
                    "edit-delete-symbolic" if result.deleted else "edit-find-symbolic",
                    f"{action}: {duplicate.removed}",
                    f"Keep: {duplicate.kept} | Distance: {duplicate.distance}",
                )
            )
        status = "deleted" if result.deleted else "found"
        count = len(result.duplicates)
        self._set_summary(
            "checkmark-symbolic",
            f"{count} duplicate{'s' if count != 1 else ''} {status}",
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
        self._automatic_workers: Gtk.CheckButton | None = None
        self._workers: Gtk.SpinButton | None = None
        self._automatic_memory: Gtk.CheckButton | None = None
        self._memory: Gtk.SpinButton | None = None
        self._replace: Gtk.Switch | None = None

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

        workers_box, self._automatic_workers, self._workers = (
            automatic_number_control(minimum=1, maximum=1024, value=4)
        )
        add_form_row(grid, 2, "Workers", workers_box)

        memory_box, self._automatic_memory, self._memory = automatic_number_control(
            minimum=1,
            maximum=1048576,
            value=512,
            unit="MiB",
        )
        add_form_row(grid, 3, "Memory limit", memory_box)

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
                callback=lambda: self._submit(request, "Converting images to WebP..."),
            )
            return
        self._submit(request, "Checking WebP conversions...")

    def _request_from_form(self) -> WebPConversionRequest:
        if any(
            widget is None
            for widget in (
                self._directory,
                self._quality,
                self._automatic_workers,
                self._workers,
                self._automatic_memory,
                self._memory,
                self._replace,
            )
        ):
            raise RuntimeError("tab has not been built")
        assert self._directory is not None
        assert self._quality is not None
        assert self._automatic_workers is not None
        assert self._workers is not None
        assert self._automatic_memory is not None
        assert self._memory is not None
        assert self._replace is not None
        workers = (
            None
            if self._automatic_workers.get_active()
            else self._workers.get_value_as_int()
        )
        memory = (
            None
            if self._automatic_memory.get_active()
            else self._memory.get_value_as_int()
        )
        return self.create_request(
            self._directory.get_text(),
            quality=self._quality.get_value_as_int(),
            replace=self._replace.get_active(),
            max_workers=workers,
            memory_limit_mb=memory,
        )

    def _render_result(self, result: WebPDirectoryConversionResult) -> None:
        if not result.conversions and not result.skips:
            self._show_empty_result(
                "checkmark-symbolic",
                "No convertible images found",
                "The directory contained no supported images requiring work.",
            )
            self._set_summary("checkmark-symbolic", "0 converted, 0 skipped")
            return

        result_list = self._prepare_results()
        total_saved = 0
        for conversion in result.conversions:
            saved = conversion.original_size - conversion.webp_size
            total_saved += saved
            result_list.append(
                result_row(
                    "image-x-generic-symbolic",
                    f"Converted: {conversion.source}",
                    f"Output: {conversion.destination} | Saved: {_format_bytes(saved)}",
                )
            )
        for skip in result.skips:
            result_list.append(
                result_row(
                    "dialog-information-symbolic",
                    f"Skipped: {skip.path}",
                    skip.reason,
                )
            )
        summary = (
            f"{len(result.conversions)} converted, {len(result.skips)} skipped"
        )
        if total_saved:
            summary += f" | {_format_bytes(total_saved)} saved"
        self._set_summary("checkmark-symbolic", summary)


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KiB", "MiB", "GiB"):
        if abs(value) < 1024 or unit == "GiB":
            if unit == "bytes":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")