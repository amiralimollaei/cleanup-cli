"""In-place conversion of directory images to smaller WebP files."""

from __future__ import annotations

import os
import tempfile
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from PIL import Image, UnidentifiedImageError
import tqdm

from .abstractions import (
    DirectoryScanner,
    ImageDirectoryScanner,
    file_identity,
    hard_link_no_clobber,
    quarantine_if_unchanged,
)


FrameT = TypeVar("FrameT")


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
class WebPOptions:
    """Validated options for a directory WebP conversion."""

    quality: int = 80
    replace: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.quality <= 100:
            raise ValueError("quality must be between 0 and 100")


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
    def decode(self, path: Path) -> DecodedImage[FrameT]:
        """Decode one still image and return conversion metadata."""

    @abstractmethod
    def encode(self, frame: FrameT, destination: Path, quality: int) -> None:
        """Encode *frame* as WebP at *destination*."""

    @abstractmethod
    def dimensions(self, path: Path) -> tuple[int, int]:
        """Validate a WebP file and return its decoded dimensions."""


class PillowWebPCodec(WebPCodec[Image.Image]):
    """WebP codec implementation backed by Pillow."""

    def decode(self, path: Path) -> DecodedImage[Image.Image]:
        with Image.open(path) as image:
            image.seek(0)
            return DecodedImage(
                frame=image.copy(),
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


class WebPDirectoryConverter(Generic[FrameT]):
    """Apply WebP conversion policy independently of a concrete codec."""

    def __init__(
        self,
        codec: WebPCodec[FrameT],
        *,
        scanner: DirectoryScanner | None = None,
        max_workers: int | None = None,
    ) -> None:
        self._codec = codec
        self._scanner = scanner or ImageDirectoryScanner()
        self._max_workers = max_workers

    def convert(
        self,
        directory: Path,
        *,
        quality: int = 80,
        replace: bool = False,
        max_workers: int | None = None,
    ) -> tuple[list[WebPConversion], list[WebPSkip]]:
        options = WebPOptions(quality, replace)

        conversions: list[WebPConversion] = []
        skips: list[WebPSkip] = []

        paths = list(self._scanner.scan(directory))
        workers = self._max_workers if max_workers is None else max_workers
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = executor.map(
                lambda path: self._convert_file_safely(path, options), paths
            )
            for result in tqdm.tqdm(
                results,
                total=len(paths),
                desc="converting",
                unit="file",
            ):
                if isinstance(result, WebPConversion):
                    conversions.append(result)
                elif result is not None:
                    skips.append(result)

        return conversions, skips

    def _convert_file_safely(
        self,
        path: Path,
        options: WebPOptions,
    ) -> WebPConversion | WebPSkip | None:
        try:
            result = self._convert_file(path, options)
        except (UnidentifiedImageError, OSError, StopIteration, ValueError):
            # Directory scans commonly include non-images and unsupported files.
            return None

        return result

    def _convert_file(
        self,
        path: Path,
        options: WebPOptions,
    ) -> WebPConversion | WebPSkip | None:
        # The scanner intentionally includes WebP files so other image
        # operations can inspect them, but this converter must never decode
        # them.  In particular, decoding first is unnecessary work and makes
        # the decision depend on codec probing rather than the directory
        # entry's format.  It also avoids any possibility of a WebP being
        # passed back through the encoder on a subsequent run.
        if path.suffix.lower() == ".webp":
            return None

        source_identity = file_identity(path)
        decoded = self._codec.decode(path)
        if file_identity(path) != source_identity:
            return WebPSkip(path, "source changed while it was being read")
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


def _temporary_webp_path(source: Path) -> Path:
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=source.parent,
        prefix=f".{source.stem}-",
        suffix=".webp",
    )
    os.close(file_descriptor)
    return Path(temporary_name)


def convert_directory_to_webp(
    directory: str | Path,
    *,
    quality: int = 80,
    replace: bool = False,
    max_workers: int | None = None,
) -> tuple[list[WebPConversion], list[WebPSkip]]:
    """Recursively replace images with smaller, equally sized WebP files.

    Non-images and existing WebP images are ignored. A source is removed only
    after its temporary WebP has been decoded, dimension-checked, and found to
    use fewer bytes. Existing destination paths are never overwritten.
    """

    converter = WebPDirectoryConverter(PillowWebPCodec(), max_workers=max_workers)
    return converter.convert(Path(directory), quality=quality, replace=replace)
