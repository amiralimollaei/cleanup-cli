"""In-place conversion of directory images to smaller WebP files."""

from __future__ import annotations

import os
import tempfile
import warnings
from abc import ABC, abstractmethod
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

import tqdm
from PIL import Image, UnidentifiedImageError

from .abstractions import (
    DirectoryScanner,
    FileIdentity,
    ImageDirectoryScanner,
    file_identity,
    hard_link_no_clobber,
    quarantine_if_unchanged,
)


FrameT = TypeVar("FrameT")


_MEBIBYTE = 1024 * 1024
# Pillow may retain its decoded raster while its WebP plugin creates an RGB(A)
# conversion, libwebp working buffers, and the encoded output bytes.  This is
# intentionally more conservative than simply multiplying by channel count.
_ESTIMATED_BYTES_PER_PIXEL = 32
_ESTIMATED_FIXED_BYTES = 8 * _MEBIBYTE
_FALLBACK_MEMORY_LIMIT = 256 * _MEBIBYTE
_MAX_AUTOMATIC_MEMORY_LIMIT = 1024 * _MEBIBYTE


@dataclass(frozen=True)
class WebPConversion:
    """One source image replaced by a smaller WebP image."""

    source: Path
    destination: Path
    original_size: int
    webp_size: int


@dataclass(frozen=True)
class WebPSkip:
    """A file that was recognized as an image but not converted."""

    path: Path
    reason: str


@dataclass(frozen=True)
class WebPDirectoryConversionResult:
    """All conversions and skips produced by one directory operation."""

    conversions: tuple[WebPConversion, ...]
    skips: tuple[WebPSkip, ...]


@dataclass(frozen=True)
class WebPOptions:
    """Validated options for a directory WebP conversion."""

    quality: int = 80
    replace: bool = False
    max_workers: int | None = None
    memory_limit_mb: int | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.quality <= 100:
            raise ValueError("quality must be between 0 and 100")
        if self.max_workers is not None and self.max_workers < 1:
            raise ValueError("max_workers must be greater than 0")
        if self.memory_limit_mb is not None and self.memory_limit_mb < 1:
            raise ValueError("memory_limit_mb must be greater than 0")


@dataclass(frozen=True)
class ImageInspection:
    """Header metadata used to schedule an image before decoding its pixels."""

    dimensions: tuple[int, int]
    is_webp: bool
    is_multi_frame: bool
    estimated_peak_bytes: int

    def __post_init__(self) -> None:
        width, height = self.dimensions
        if width < 1 or height < 1:
            raise ValueError("image dimensions must be greater than 0")
        if self.estimated_peak_bytes < 1:
            raise ValueError("estimated_peak_bytes must be greater than 0")


@dataclass(frozen=True)
class DecodedImage(Generic[FrameT]):
    """A decoded frame and the metadata needed by conversion policy."""

    frame: FrameT
    dimensions: tuple[int, int]
    is_webp: bool
    is_multi_frame: bool


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
                return ImageInspection(
                    dimensions=dimensions,
                    is_webp=image.format == "WEBP",
                    is_multi_frame=getattr(image, "n_frames", 1) > 1,
                    estimated_peak_bytes=_estimate_peak_bytes(dimensions),
                )

    def decode(self, path: Path) -> DecodedImage[Image.Image]:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image.seek(0)
                image.load()
                return DecodedImage(
                    # A loaded single-frame image remains usable after its
                    # source file is closed by the context manager. Returning
                    # it directly avoids a second full-size raster allocation.
                    frame=image,
                    dimensions=image.size,
                    is_webp=image.format == "WEBP",
                    is_multi_frame=getattr(image, "n_frames", 1) > 1,
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


@dataclass(frozen=True)
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
        max_workers: int | None = None,
        memory_limit_mb: int | None = None,
    ) -> None:
        self._codec = codec
        self._scanner = scanner or ImageDirectoryScanner()
        self._max_workers = max_workers
        self._memory_limit_mb = memory_limit_mb
        if max_workers is not None and max_workers < 1:
            raise ValueError("max_workers must be greater than 0")
        if memory_limit_mb is not None and memory_limit_mb < 1:
            raise ValueError("memory_limit_mb must be greater than 0")

    def convert(
        self,
        directory: Path,
        *,
        quality: int = 80,
        replace: bool = False,
        max_workers: int | None = None,
        memory_limit_mb: int | None = None,
    ) -> WebPDirectoryConversionResult:
        configured_workers = (
            self._max_workers if max_workers is None else max_workers
        )
        configured_memory_limit = (
            self._memory_limit_mb if memory_limit_mb is None else memory_limit_mb
        )
        options = WebPOptions(
            quality, replace, configured_workers, configured_memory_limit
        )

        paths = list(self._scanner.scan(directory))
        worker_count = options.max_workers or min(32, (os.cpu_count() or 1) + 4)
        memory_limit = (
            options.memory_limit_mb * _MEBIBYTE
            if options.memory_limit_mb is not None
            else _automatic_memory_limit()
        )

        # Keep one slot per scanned path so inspection and worker results can
        # be merged without making completion timing observable to callers.
        ordered_results: list[WebPConversion | WebPSkip | None] = [None] * len(paths)
        candidates: list[_ConversionCandidate] = []
        for index, path in enumerate(
            tqdm.tqdm(paths, desc="inspecting", unit="file")
        ):
            inspected = self._inspect_file_safely(index, path)
            if isinstance(inspected, _ConversionCandidate):
                required = inspected.inspection.estimated_peak_bytes
                if required > memory_limit:
                    ordered_results[index] = WebPSkip(
                        path,
                        "estimated conversion memory "
                        f"({_format_mebibytes(required)}) exceeds limit "
                        f"({_format_mebibytes(memory_limit)})",
                    )
                else:
                    candidates.append(inspected)
            else:
                ordered_results[index] = inspected

        self._convert_candidates(
            candidates,
            ordered_results,
            options,
            max_workers=worker_count,
            memory_limit=memory_limit,
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
                return WebPSkip(path, "multi-frame images are not supported")
            destination = path.with_suffix(".webp")
            if os.path.lexists(destination):
                return WebPSkip(path, f"destination exists: {destination}")
            return _ConversionCandidate(index, path, identity, inspection)
        except (
            UnidentifiedImageError,
            OSError,
            EOFError,
            StopIteration,
            ValueError,
            Image.DecompressionBombWarning,
            Image.DecompressionBombError,
        ):
            return None

    def _convert_candidates(
        self,
        candidates: list[_ConversionCandidate],
        ordered_results: list[WebPConversion | WebPSkip | None],
        options: WebPOptions,
        *,
        max_workers: int,
        memory_limit: int,
    ) -> None:
        """Run candidates while their estimated aggregate memory fits."""

        # Largest-first admission prevents a stream of tiny files from
        # needlessly delaying the jobs that are hardest to fit. Results are
        # still placed into scan-order slots.
        waiting = sorted(
            candidates,
            key=lambda candidate: (
                -candidate.inspection.estimated_peak_bytes,
                candidate.index,
            ),
        )
        pending: dict[Future[WebPConversion | WebPSkip | None], _ConversionCandidate] = {}
        available = memory_limit

        with (
            ThreadPoolExecutor(max_workers=max_workers) as executor,
            tqdm.tqdm(total=len(candidates), desc="converting", unit="file") as progress,
        ):
            while waiting or pending:
                while len(pending) < max_workers:
                    next_index = next(
                        (
                            index
                            for index, candidate in enumerate(waiting)
                            if candidate.inspection.estimated_peak_bytes <= available
                        ),
                        None,
                    )
                    if next_index is None:
                        break

                    candidate = waiting.pop(next_index)
                    required = candidate.inspection.estimated_peak_bytes
                    available -= required
                    future = executor.submit(
                        self._convert_file_safely,
                        candidate,
                        options,
                    )
                    pending[future] = candidate

                # Every waiting candidate was pre-checked against the complete
                # budget, so lack of a fit means an active job must finish.
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    candidate = pending.pop(future)
                    available += candidate.inspection.estimated_peak_bytes
                    ordered_results[candidate.index] = future.result()
                    progress.update()

    def _convert_file_safely(
        self,
        candidate: _ConversionCandidate,
        options: WebPOptions,
    ) -> WebPConversion | WebPSkip | None:
        try:
            result = self._convert_file(candidate, options)
        except (
            UnidentifiedImageError,
            OSError,
            EOFError,
            StopIteration,
            ValueError,
            Image.DecompressionBombWarning,
            Image.DecompressionBombError,
        ):
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
                return WebPSkip(path, "multi-frame images are not supported")

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


def _estimate_peak_bytes(dimensions: tuple[int, int]) -> int:
    width, height = dimensions
    return width * height * _ESTIMATED_BYTES_PER_PIXEL + _ESTIMATED_FIXED_BYTES


def _format_mebibytes(byte_count: int) -> str:
    return f"{byte_count / _MEBIBYTE:.1f} MiB"


def _automatic_memory_limit() -> int:
    """Reserve a conservative fraction of memory currently available."""

    available = _available_memory_bytes()
    if available is None:
        return _FALLBACK_MEMORY_LIMIT
    return max(
        1,
        min(available // 4, _MAX_AUTOMATIC_MEMORY_LIMIT),
    )


def _available_memory_bytes() -> int | None:
    """Best-effort host/container available-memory detection."""

    candidates: list[int] = []
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                candidates.append(int(line.split()[1]) * 1024)
                break
    except (OSError, ValueError, IndexError):
        pass

    try:
        cgroup_limit = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if cgroup_limit != "max":
            limit = int(cgroup_limit)
            usage = int(Path("/sys/fs/cgroup/memory.current").read_text().strip())
            candidates.append(max(0, limit - usage))
    except (OSError, ValueError):
        pass

    if candidates:
        return min(candidates)

    try:
        pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        if pages > 0 and page_size > 0:
            return pages * page_size
    except (AttributeError, OSError, ValueError):
        pass
    return None


def convert_directory_to_webp(
    directory: str | Path,
    *,
    quality: int = 80,
    replace: bool = False,
    max_workers: int | None = None,
    memory_limit_mb: int | None = None,
) -> WebPDirectoryConversionResult:
    """Recursively replace images with smaller, equally sized WebP files.

    Non-images and existing WebP images are ignored. A source is removed only
    after its temporary WebP has been decoded, dimension-checked, and found to
    use fewer bytes. Existing destination paths are never overwritten. Images
    are decoded concurrently only while their combined estimated peak memory
    fits the configured or automatically selected budget.
    """

    converter = WebPDirectoryConverter(
        PillowWebPCodec(),
        max_workers=max_workers,
        memory_limit_mb=memory_limit_mb,
    )
    return converter.convert(Path(directory), quality=quality, replace=replace)
