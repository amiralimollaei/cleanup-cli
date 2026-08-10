"""Reusable contracts for ordered directory analysis."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import os
from pathlib import Path
import tempfile
from typing import Generic, Protocol, TypeVar

import tqdm
from PIL import Image, UnidentifiedImageError

from .path_sort import sort_numbered_paths


ValueT = TypeVar("ValueT")
AnalyzedValueT = TypeVar("AnalyzedValueT", covariant=True)
MeasuredValueT = TypeVar("MeasuredValueT", contravariant=True)


@dataclass(frozen=True)
class FileIdentity:
    """Metadata identifying the directory entry that was analyzed."""

    device: int
    inode: int
    size: int
    modified_ns: int


def file_identity(path: Path) -> FileIdentity:
    """Return identity metadata used to reject stale destructive actions."""

    stat = path.stat()
    return FileIdentity(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def hard_link_no_clobber(source: Path, destination: Path) -> None:
    """Make *destination* contain *source*'s data without clobbering it.

    Hard links give an atomic, same-filesystem, no-overwrite publish.  When the
    platform or filesystem does not provide ``os.link``, fall back to a
    byte-for-byte copy that likewise refuses to overwrite an existing file.

    Raises ``FileExistsError`` if *destination* already exists.
    """

    try:
        os.link(source, destination, follow_symlinks=False)
    except (AttributeError, NotImplementedError):
        # os.link is missing or unsupported on this platform.
        with open(source, "rb") as input_stream, open(destination, "xb") as output_stream:
            while chunk := input_stream.read(65536):
                output_stream.write(chunk)


def quarantine_if_unchanged(path: Path, expected: FileIdentity) -> Path:
    """Atomically move *path* aside and verify it is the analyzed file.

    Moving before checking closes the check/unlink race: even if a writer
    replaces the pathname at the worst moment, its data is retained either at
    the original name or at the returned quarantine path.
    """

    quarantine_directory = Path(
        tempfile.mkdtemp(dir=path.parent, prefix=f".{path.name}-quarantine-")
    )
    quarantine = quarantine_directory / path.name
    os.rename(path, quarantine)
    if file_identity(quarantine) == expected:
        return quarantine

    try:
        hard_link_no_clobber(quarantine, path)
    except FileExistsError:
        raise OSError(
            f"file changed and was preserved at recovery path: {quarantine}"
        )
    quarantine.unlink()
    quarantine_directory.rmdir()
    raise OSError(f"file changed since it was analyzed: {path}")


class FileAnalyzer(Protocol[AnalyzedValueT]):
    """Create a domain value from one file."""

    def analyze(self, path: Path) -> AnalyzedValueT:
        """Analyze *path*, raising when the file is unsupported."""
        ...


class DistanceMetric(Protocol[MeasuredValueT]):
    """Measure the distance between two domain values."""

    def distance(self, left: MeasuredValueT, right: MeasuredValueT) -> int:
        """Return a non-negative distance where zero means equivalent."""
        ...


class PathOrderer(Protocol):
    """Return paths in a deterministic order."""

    def order(self, paths: Iterable[Path]) -> list[Path]:
        """Order *paths* without changing their values."""
        ...


class DirectoryScanner(Protocol):
    """Discover files below a directory in a stable order."""

    def scan(self, directory: Path) -> Iterable[Path]:
        """Yield files below *directory*, raising if it is not a directory."""
        ...


class PathFilter(Protocol):
    """Decide whether a discovered path should be included in a scan."""

    def accepts(self, path: Path) -> bool:
        """Return *True* when *path* should be yielded by a scanner."""
        ...


#: Common still-image extensions supported by Pillow. Animated formats remain
#: included because Pillow can decode their first frame for image analysis.
IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".apng",
        ".gif",
        ".webp",
        ".bmp",
        ".tif",
        ".tiff",
        ".pnm",
        ".ppm",
        ".pgm",
        ".pbm",
        ".pcx",
        ".dds",
        ".exr",
        ".hdr",
        ".fits",
        ".qoi",
        ".jp2",
        ".j2k",
        ".jls",
        ".jxl",
        ".xbm",
        ".xwd",
        ".sun",
        ".ras",
        ".ico",
        ".wbmp",
    }
)


class ImageExtensionFilter:
    """Accept paths whose suffix is a recognized image extension."""

    def accepts(self, path: Path) -> bool:
        return path.suffix.lower() in IMAGE_EXTENSIONS


@dataclass(frozen=True)
class IndexedFile(Generic[ValueT]):
    """A file paired with its analyzed domain value."""

    path: Path
    value: ValueT
    identity: FileIdentity | None = field(default=None, compare=False)


class DirectoryIndexer(ABC, Generic[ValueT]):
    """Abstract source of analyzed files from a directory."""

    @abstractmethod
    def index(
        self, directory: Path, *, max_workers: int | None = None
    ) -> list[IndexedFile[ValueT]]:
        """Return analyzed files from *directory* in a stable order."""


class NaturalPathOrderer:
    """Order paths using the project's natural numeric sort."""

    def order(self, paths: Iterable[Path]) -> list[Path]:
        return sort_numbered_paths(paths)


class RecursiveDirectoryScanner:
    """Recursively discover files using an injected ordering strategy."""

    def __init__(self, orderer: PathOrderer | None = None) -> None:
        self._orderer = orderer or NaturalPathOrderer()

    def scan(self, directory: Path) -> Iterator[Path]:
        yield from self._discover(directory)

    def _discover(self, directory: Path) -> Iterator[Path]:
        """Yield every regular file below *directory* in natural order."""

        if not directory.is_dir():
            raise NotADirectoryError(directory)

        files = (
            path
            for path in directory.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        yield from self._orderer.order(files)


class ImageDirectoryScanner:
    """Recursively discover only still-image files in a stable order.

    Videos and other non-image files never reach a decoder, so a large media
    file cannot exhaust memory merely by being probed as a potential image.
    """

    def __init__(
        self,
        orderer: PathOrderer | None = None,
        filter: PathFilter | None = None,
    ) -> None:
        self._recursive = RecursiveDirectoryScanner(orderer)
        self._filter = filter or ImageExtensionFilter()

    def scan(self, directory: Path) -> Iterator[Path]:
        for path in self._recursive._discover(directory):
            if self._filter.accepts(path):
                yield path


class RecursiveDirectoryIndexer(DirectoryIndexer[ValueT]):
    """Analyze all supported files below a directory."""

    def __init__(
        self,
        analyzer: FileAnalyzer[ValueT],
        *,
        scanner: DirectoryScanner | None = None,
        orderer: PathOrderer | None = None,
        ignored_errors: tuple[type[Exception], ...] = (
            UnidentifiedImageError,
            OSError,
            EOFError,
            StopIteration,
            ValueError,
            IndexError,
            Image.DecompressionBombWarning,
            Image.DecompressionBombError,
        ),
    ) -> None:
        self._analyzer = analyzer
        if scanner is not None and orderer is not None:
            raise ValueError("scanner and orderer cannot both be provided")
        self._scanner = scanner or RecursiveDirectoryScanner(orderer)
        self._ignored_errors = ignored_errors

    def index(
        self, directory: Path, *, max_workers: int | None = None
    ) -> list[IndexedFile[ValueT]]:
        indexed: list[IndexedFile[ValueT]] = []
        paths = list(self._scanner.scan(directory))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = executor.map(self._index_file_safely, paths)
            for result in tqdm.tqdm(
                results, total=len(paths), desc=f"indexing {directory}", unit="file"
            ):
                if result is not None:
                    indexed.append(result)
        return indexed

    def _index_file_safely(self, path: Path) -> IndexedFile[ValueT] | None:
        try:
            identity = file_identity(path)
            value = self._analyzer.analyze(path)
            # Never retain an analysis of a file that changed while being read.
            if file_identity(path) != identity:
                return None
            return IndexedFile(path, value, identity)
        except self._ignored_errors:
            return None
