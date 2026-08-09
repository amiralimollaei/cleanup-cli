"""In-place conversion of directory images to smaller WebP files."""

from __future__ import annotations

import os
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar, cast

import av
from av.error import FFmpegError
from av.video.frame import VideoFrame
from av.video.stream import VideoStream
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


class PyAVWebPCodec(WebPCodec[VideoFrame]):
    """WebP codec implementation backed by PyAV and libwebp."""

    def decode(self, path: Path) -> DecodedImage[VideoFrame]:
        with av.open(str(path)) as container:
            if not container.streams.video:
                raise ValueError("file has no video stream")

            stream = container.streams.video[0]
            is_webp = stream.codec_context.name == "webp"
            frames = container.decode(stream)
            first = next(frames)
            try:
                next(frames)
            except StopIteration:
                is_multi_frame = False
            else:
                is_multi_frame = True

            return DecodedImage(
                frame=first,
                dimensions=(first.width, first.height),
                is_webp=is_webp,
                is_multi_frame=is_multi_frame,
            )

    def encode(self, frame: VideoFrame, destination: Path, quality: int) -> None:
        frame = frame.reformat(format="bgra")
        with av.open(str(destination), mode="w", format="webp") as container:
            stream = cast(VideoStream, container.add_stream("libwebp"))
            stream.width = frame.width
            stream.height = frame.height
            stream.pix_fmt = "bgra"
            stream.codec_context.options = {"quality": str(quality)}

            for packet in stream.encode(frame):
                container.mux(packet)
            for packet in stream.encode(None):
                container.mux(packet)

    def dimensions(self, path: Path) -> tuple[int, int]:
        with av.open(str(path)) as container:
            frame = next(container.decode(video=0))
            if container.streams.video[0].codec_context.name != "webp":
                raise ValueError("encoded file is not WebP")
            return frame.width, frame.height


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
        *,
        quality: int = 80,
        replace: bool = False,
    ) -> tuple[list[WebPConversion], list[WebPSkip]]:
        options = WebPOptions(quality, replace)

        conversions: list[WebPConversion] = []
        skips: list[WebPSkip] = []

        paths = list(self._scanner.scan(directory))
        for path in tqdm.tqdm(paths, desc="converting", unit="file"):
            result = self._convert_file_safely(path, options)
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
        except (FFmpegError, OSError, StopIteration, ValueError):
            # Directory scans commonly include non-images and unsupported files.
            return None

        return result

    def _convert_file(
        self,
        path: Path,
        options: WebPOptions,
    ) -> WebPConversion | WebPSkip | None:
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
) -> tuple[list[WebPConversion], list[WebPSkip]]:
    """Recursively replace images with smaller, equally sized WebP files.

    Non-images and existing WebP images are ignored. A source is removed only
    after its temporary WebP has been decoded, dimension-checked, and found to
    use fewer bytes. Existing destination paths are never overwritten.
    """

    converter = WebPDirectoryConverter(PyAVWebPCodec())
    return converter.convert(Path(directory), quality=quality, replace=replace)