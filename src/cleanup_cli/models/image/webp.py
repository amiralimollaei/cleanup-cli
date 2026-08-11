"""In-place conversion of directory images to smaller WebP files."""

from __future__ import annotations

import os
import tempfile
import warnings
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Generic, TypeAlias, TypeVar

from PIL import Image

from cleanup_cli.models.abstractions import (
    DirectoryScanner,
    ImageDirectoryScanner,
)
from cleanup_cli.models.filesystem import (
    FileIdentity,
    file_identity,
    hard_link_no_clobber,
    quarantine_if_unchanged,
)
from cleanup_cli.models.image.errors import IMAGE_INPUT_ERRORS
from cleanup_cli.models.image.memory import (
    MEBIBYTE,
    automatic_memory_limit,
    estimate_peak_bytes,
    format_mebibytes,
)
from cleanup_cli.models.parallel import weighted_parallel_map
from cleanup_cli.models.progress import ProgressObserver, track_progress
from cleanup_cli.models.validation import (
    validate_inclusive_range,
    validate_optional_positive,
)


FrameT = TypeVar("FrameT")


@dataclass(frozen=True, slots=True)
class WebPConversion:
    """One source image replaced by a smaller WebP image."""

    source: Path
    destination: Path
    original_size: int
    webp_size: int

    @property
    def saved_bytes(self) -> int:
        """Return the number of bytes removed by this conversion."""

        return self.original_size - self.webp_size


@dataclass(frozen=True, slots=True)
class WebPSkip:
    """A file that was recognized as an image but not converted."""

    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class WebPDirectoryConversionResult:
    """All conversions and skips produced by one directory operation."""

    conversions: tuple[WebPConversion, ...]
    skips: tuple[WebPSkip, ...]

    @property
    def total_saved_bytes(self) -> int:
        """Return the aggregate storage saved by completed conversions."""

        return sum(conversion.saved_bytes for conversion in self.conversions)


WebPResult: TypeAlias = WebPConversion | WebPSkip
WebPResultObserver: TypeAlias = Callable[[WebPResult], None]


@dataclass(frozen=True, slots=True)
class WebPOptions:
    """Validated options for a directory WebP conversion."""

    quality: int = 80
    replace: bool = False
    max_workers: int | None = None
    memory_limit_mb: int | None = None

    def __post_init__(self) -> None:
        validate_inclusive_range("quality", self.quality, minimum=0, maximum=100)
        validate_optional_positive("max_workers", self.max_workers)
        validate_optional_positive("memory_limit_mb", self.memory_limit_mb)


@dataclass(frozen=True, slots=True)
class ImageInspection:
    """Header metadata used to schedule an image before decoding its pixels."""

    dimensions: tuple[int, int]
    is_webp: bool
    is_multi_frame: bool
    estimated_peak_bytes: int
    image_format: str | None = None
    frame_count: int | None = None

    def __post_init__(self) -> None:
        width, height = self.dimensions
        if width < 1 or height < 1:
            raise ValueError("image dimensions must be greater than 0")
        if self.estimated_peak_bytes < 1:
            raise ValueError("estimated_peak_bytes must be greater than 0")
        if self.frame_count is not None and self.frame_count < 1:
            raise ValueError("frame_count must be greater than 0")


@dataclass(frozen=True, slots=True)
class DecodedImage(Generic[FrameT]):
    """A decoded frame and the metadata needed by conversion policy."""

    frame: FrameT
    dimensions: tuple[int, int]
    is_webp: bool
    is_multi_frame: bool
    image_format: str | None = None
    frame_count: int | None = None


class WebPCodec(ABC, Generic[FrameT]):
    """Abstract image codec used by the directory conversion service."""

    @abstractmethod
    def inspect(self, path: Path) -> ImageInspection:
        """Read image metadata without decoding its raster data."""

    @abstractmethod
    def decode(self, path: Path) -> DecodedImage[FrameT]:
        """Decode one still image and return conversion metadata."""

    @abstractmethod
    def encode(self, frame: FrameT, destination: Path, quality: int) -> None:
        """Encode *frame* as WebP at *destination*."""

    @abstractmethod
    def dimensions(self, path: Path) -> tuple[int, int]:
        """Validate a WebP file and return its decoded dimensions."""

    def release(self, frame: FrameT) -> None:
        """Release resources retained by a decoded frame, if any."""


class PillowWebPCodec(WebPCodec[Image.Image]):
    """WebP codec implementation backed by Pillow."""

    def inspect(self, path: Path) -> ImageInspection:
        # Image.open() is lazy: Pillow reads enough of the header to identify
        # the image and expose these properties without loading raster pixels.
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                dimensions = image.size
                frame_count = getattr(image, "n_frames", 1)
                return ImageInspection(
                    dimensions=dimensions,
                    is_webp=image.format == "WEBP",
                    is_multi_frame=frame_count > 1,
                    estimated_peak_bytes=estimate_peak_bytes(dimensions),
                    image_format=image.format,
                    frame_count=frame_count,
                )

    def decode(self, path: Path) -> DecodedImage[Image.Image]:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image.seek(0)
                frame_count = getattr(image, "n_frames", 1)
                image.load()
                return DecodedImage(
                    # A loaded single-frame image remains usable after its
                    # source file is closed by the context manager. Returning
                    # it directly avoids a second full-size raster allocation.
                    frame=image,
                    dimensions=image.size,
                    is_webp=image.format == "WEBP",
                    is_multi_frame=frame_count > 1,
                    image_format=image.format,
                    frame_count=frame_count,
                )

    def encode(self, frame: Image.Image, destination: Path, quality: int) -> None:
        frame.save(destination, format="WEBP", quality=quality)

    def dimensions(self, path: Path) -> tuple[int, int]:
        with Image.open(path) as image:
            if image.format != "WEBP":
                raise ValueError("encoded file is not WebP")
            return image.size

    def release(self, frame: Image.Image) -> None:
        frame.close()


@dataclass(frozen=True, slots=True)
class _ConversionCandidate:
    """An inspected source waiting for a memory reservation and worker."""

    index: int
    path: Path
    identity: FileIdentity
    inspection: ImageInspection


class WebPDirectoryConverter(Generic[FrameT]):
    """Apply WebP conversion policy independently of a concrete codec."""

    def __init__(
        self,
        codec: WebPCodec[FrameT],
        *,
        scanner: DirectoryScanner | None = None,
    ) -> None:
        self._codec = codec
        self._scanner = scanner or ImageDirectoryScanner()

    def convert(
        self,
        directory: Path,
        options: WebPOptions | None = None,
        *,
        on_result: WebPResultObserver | None = None,
        on_progress: ProgressObserver | None = None,
    ) -> WebPDirectoryConversionResult:
        request = options or WebPOptions()

        paths = list(self._scanner.scan(directory))
        memory_limit = (
            request.memory_limit_mb * MEBIBYTE
            if request.memory_limit_mb is not None
            else automatic_memory_limit()
        )

        # Keep one slot per scanned path so inspection and worker results can
        # be merged without making completion timing observable to callers.
        ordered_results: list[WebPConversion | WebPSkip | None] = [None] * len(paths)
        candidates: list[_ConversionCandidate] = []
        for index, path in enumerate(
            track_progress(
                paths,
                total=len(paths),
                description="inspecting",
                unit="file",
                on_progress=on_progress,
                activity="Inspecting images",
            )
        ):
            inspected = self._inspect_file_safely(index, path)
            if isinstance(inspected, _ConversionCandidate):
                required = inspected.inspection.estimated_peak_bytes
                if required > memory_limit:
                    skip = WebPSkip(
                        path,
                        "estimated conversion memory "
                        f"({format_mebibytes(required)}) exceeds limit "
                        f"({format_mebibytes(memory_limit)})",
                    )
                    ordered_results[index] = skip
                    if on_result is not None:
                        on_result(skip)
                else:
                    candidates.append(inspected)
            else:
                ordered_results[index] = inspected
                if inspected is not None and on_result is not None:
                    on_result(inspected)

        self._convert_candidates(
            candidates,
            ordered_results,
            request,
            memory_limit=memory_limit,
            on_result=on_result,
            on_progress=on_progress,
        )

        conversions: list[WebPConversion] = []
        skips: list[WebPSkip] = []
        for result in ordered_results:
            if isinstance(result, WebPConversion):
                conversions.append(result)
            elif isinstance(result, WebPSkip):
                skips.append(result)

        return WebPDirectoryConversionResult(tuple(conversions), tuple(skips))

    def _inspect_file_safely(
        self,
        index: int,
        path: Path,
    ) -> _ConversionCandidate | WebPSkip | None:
        # Existing WebP files are ignored by suffix before even their headers
        # are opened, preserving the converter's idempotent fast path.
        if path.suffix.lower() == ".webp":
            return None

        try:
            identity = file_identity(path)
            inspection = self._codec.inspect(path)
            if file_identity(path) != identity:
                return WebPSkip(path, "source changed while it was being inspected")
            if inspection.is_webp:
                return None
            if inspection.is_multi_frame:
                return WebPSkip(
                    path,
                    _multi_frame_skip_reason(
                        inspection.image_format,
                        inspection.frame_count,
                    ),
                )
            destination = path.with_suffix(".webp")
            if os.path.lexists(destination):
                return WebPSkip(path, f"destination exists: {destination}")
            return _ConversionCandidate(index, path, identity, inspection)
        except IMAGE_INPUT_ERRORS:
            return None

    def _convert_candidates(
        self,
        candidates: list[_ConversionCandidate],
        ordered_results: list[WebPConversion | WebPSkip | None],
        options: WebPOptions,
        *,
        memory_limit: int,
        on_result: WebPResultObserver | None,
        on_progress: ProgressObserver | None,
    ) -> None:
        """Run candidates while their estimated aggregate memory fits."""

        def convert(
            candidate: _ConversionCandidate,
        ) -> WebPConversion | WebPSkip | None:
            return self._convert_file_safely(candidate, options)

        results = weighted_parallel_map(
            convert,
            candidates,
            weight=lambda candidate: candidate.inspection.estimated_peak_bytes,
            capacity=memory_limit,
            max_workers=options.max_workers,
        )
        for candidate, result in track_progress(
            results,
            total=len(candidates),
            description="converting",
            unit="file",
            on_progress=on_progress,
            activity="Converting images",
        ):
            ordered_results[candidate.index] = result
            if result is not None and on_result is not None:
                on_result(result)

    def _convert_file_safely(
        self,
        candidate: _ConversionCandidate,
        options: WebPOptions,
    ) -> WebPConversion | WebPSkip | None:
        try:
            result = self._convert_file(candidate, options)
        except IMAGE_INPUT_ERRORS:
            # Directory scans commonly include non-images and unsupported files.
            return None

        return result

    def _convert_file(
        self,
        candidate: _ConversionCandidate,
        options: WebPOptions,
    ) -> WebPConversion | WebPSkip | None:
        path = candidate.path
        source_identity = candidate.identity
        if file_identity(path) != source_identity:
            return WebPSkip(path, "source changed after it was inspected")

        decoded = self._codec.decode(path)
        try:
            if file_identity(path) != source_identity:
                return WebPSkip(path, "source changed while it was being read")
            if decoded.dimensions != candidate.inspection.dimensions:
                return WebPSkip(path, "source dimensions changed after inspection")
            if decoded.is_webp:
                return None
            if decoded.is_multi_frame:
                return WebPSkip(
                    path,
                    _multi_frame_skip_reason(
                        decoded.image_format,
                        decoded.frame_count,
                    ),
                )

            destination = path.with_suffix(".webp")
            if os.path.lexists(destination):
                return WebPSkip(path, f"destination exists: {destination}")

            original_size = path.stat().st_size
            temporary = _temporary_webp_path(path)
            try:
                self._codec.encode(decoded.frame, temporary, options.quality)
                if self._codec.dimensions(temporary) != decoded.dimensions:
                    raise ValueError("converted image dimensions changed")

                webp_size = temporary.stat().st_size
                if webp_size >= original_size:
                    return WebPSkip(path, "WebP would not be smaller")

                if not options.replace:
                    return WebPSkip(path, "replacement not enabled (use --replace)")

                if os.path.lexists(destination):
                    return WebPSkip(path, f"destination exists: {destination}")
                try:
                    source_quarantine = quarantine_if_unchanged(path, source_identity)
                except OSError as error:
                    return WebPSkip(path, str(error))
                # A hard link gives same-filesystem atomic create/no-clobber
                # semantics. os.replace() would destroy a concurrent output.
                try:
                    hard_link_no_clobber(temporary, destination)
                except FileExistsError:
                    try:
                        hard_link_no_clobber(source_quarantine, path)
                    except FileExistsError:
                        pass
                    else:
                        source_quarantine.unlink()
                        source_quarantine.parent.rmdir()
                    return WebPSkip(path, f"destination exists: {destination}")
                temporary.unlink()
                source_quarantine.unlink()
                source_quarantine.parent.rmdir()
                return WebPConversion(path, destination, original_size, webp_size)
            finally:
                temporary.unlink(missing_ok=True)
        finally:
            self._codec.release(decoded.frame)


def _temporary_webp_path(source: Path) -> Path:
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=source.parent,
        prefix=f".{source.stem}-",
        suffix=".webp",
    )
    os.close(file_descriptor)
    return Path(temporary_name)


def _multi_frame_skip_reason(
    image_format: str | None,
    frame_count: int | None,
) -> str:
    """Explain why a multi-image input is skipped without losing its data."""

    if image_format == "MPO":
        count = (
            f"{frame_count} embedded images"
            if frame_count is not None
            else "embedded images"
        )
        return (
            "MPO (multi-picture JPEG) contains "
            f"{count}; conversion is skipped to avoid discarding embedded image data"
        )

    if image_format is not None and frame_count is not None:
        return (
            f"{image_format} contains {frame_count} frames; "
            "multi-frame conversion is not supported"
        )
    return "multi-frame images are not supported"


def convert_directory_to_webp(
    directory: str | Path,
    *,
    quality: int = 80,
    replace: bool = False,
    max_workers: int | None = None,
    memory_limit_mb: int | None = None,
    on_result: WebPResultObserver | None = None,
    on_progress: ProgressObserver | None = None,
) -> WebPDirectoryConversionResult:
    """Recursively replace images with smaller, equally sized WebP files.

    Non-images and existing WebP images are ignored. A source is removed only
    after its temporary WebP has been decoded, dimension-checked, and found to
    use fewer bytes. Existing destination paths are never overwritten. Images
    are decoded concurrently only while their combined estimated peak memory
    fits the configured or automatically selected budget.
    """

    converter = WebPDirectoryConverter(PillowWebPCodec())
    options = WebPOptions(
        quality=quality,
        replace=replace,
        max_workers=max_workers,
        memory_limit_mb=memory_limit_mb,
    )
    return converter.convert(
        Path(directory),
        options,
        on_result=on_result,
        on_progress=on_progress,
    )
