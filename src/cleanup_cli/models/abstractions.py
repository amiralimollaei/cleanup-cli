"""Reusable contracts for ordered directory analysis."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
import os
from pathlib import Path
import tempfile
from typing import Generic, Protocol, TypeVar

import tqdm

from .image.errors import IMAGE_INPUT_ERRORS
from .image.memory import MEBIBYTE, automatic_memory_limit
from .parallel import ordered_parallel_map, weighted_parallel_map
from .path_sort import sort_numbered_paths
from .validation import validate_optional_positive


ValueT = TypeVar("ValueT")
AnalyzedValueT = TypeVar("AnalyzedValueT", covariant=True)
MeasuredValueT = TypeVar("MeasuredValueT", contravariant=True)


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """Metadata identifying the directory entry that was analyzed."""

    device: int
    inode: int
    size: int
    modified_ns: int


@dataclass(frozen=True, slots=True)
class TaskProgress:
    """Completed work for one phase of a directory task."""

    activity: str
    completed: int
    total: int

    def __post_init__(self) -> None:
        if self.total < 0:
            raise ValueError("total must not be negative")
        if not 0 <= self.completed <= self.total:
            raise ValueError("completed must be between 0 and total")

    @property
    def fraction(self) -> float:
        """Return progress as a GTK-compatible value between zero and one."""

        return self.completed / self.total if self.total else 0.0


ProgressObserver = Callable[[TaskProgress], None]


def track_progress(
    values: Iterable[ValueT],
    *,
    total: int,
    description: str,
    unit: str,
    on_progress: ProgressObserver | None,
    activity: str | None = None,
) -> Iterator[ValueT]:
    """Track an iterable in the console or report it to an external view."""

    if on_progress is None:
        yield from tqdm.tqdm(values, total=total, desc=description, unit=unit)
        return

    label = activity or description
    on_progress(TaskProgress(label, 0, total))
    for completed, value in enumerate(values, start=1):
        yield value
        on_progress(TaskProgress(label, completed, total))


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
        created = False
        try:
            with open(source, "rb") as input_stream, open(
                destination, "xb"
            ) as output_stream:
                created = True
                while chunk := input_stream.read(65536):
                    output_stream.write(chunk)
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise


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
    try:
        os.rename(path, quarantine)
    except BaseException:
        quarantine_directory.rmdir()
        raise
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


class FileMemoryEstimator(Protocol):
    """Estimate peak memory before a file's full contents are decoded."""

    def estimate_memory(self, path: Path) -> int:
        """Return a positive peak-memory estimate in bytes for *path*."""
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


@dataclass(frozen=True, slots=True)
class IndexedFile(Generic[ValueT]):
    """A file paired with its analyzed domain value."""

    path: Path
    value: ValueT
    identity: FileIdentity | None = field(default=None, compare=False)


class DirectoryIndexCache(Protocol[ValueT]):
    """Persist analyzed files and reload entries whose identities still match."""

    def load(
        self, directory: Path, paths: Sequence[Path]
    ) -> dict[Path, IndexedFile[ValueT]]:
        """Return valid cached entries for the currently scanned paths."""
        ...

    def save(
        self, directory: Path, indexed: Sequence[IndexedFile[ValueT]]
    ) -> None:
        """Persist the successful results for the current directory scan."""
        ...


class DirectoryIndexer(ABC, Generic[ValueT]):
    """Abstract source of analyzed files from a directory."""

    @abstractmethod
    def index(
        self,
        directory: Path,
        *,
        max_workers: int | None = None,
        memory_limit_mb: int | None = None,
    ) -> list[IndexedFile[ValueT]]:
        """Return analyzed files from *directory* in a stable order."""

    def index_with_progress(
        self,
        directory: Path,
        *,
        max_workers: int | None = None,
        memory_limit_mb: int | None = None,
        on_progress: ProgressObserver | None = None,
    ) -> list[IndexedFile[ValueT]]:
        """Index files, allowing implementations to expose phase progress.

        Existing indexers remain compatible by ignoring the optional observer.
        """

        return self.index(
            directory,
            max_workers=max_workers,
            memory_limit_mb=memory_limit_mb,
        )


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
    """Recursively discover recognized image files in a stable order.

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
        for path in self._recursive.scan(directory):
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
        memory_estimator: FileMemoryEstimator | None = None,
        cache: DirectoryIndexCache[ValueT] | None = None,
        ignored_errors: tuple[type[Exception], ...] = IMAGE_INPUT_ERRORS,
    ) -> None:
        self._analyzer = analyzer
        if scanner is not None and orderer is not None:
            raise ValueError("scanner and orderer cannot both be provided")
        self._scanner = scanner or RecursiveDirectoryScanner(orderer)
        self._memory_estimator = memory_estimator
        self._cache = cache
        self._ignored_errors = ignored_errors

    def index(
        self,
        directory: Path,
        *,
        max_workers: int | None = None,
        memory_limit_mb: int | None = None,
        on_progress: ProgressObserver | None = None,
    ) -> list[IndexedFile[ValueT]]:
        validate_optional_positive("max_workers", max_workers)
        validate_optional_positive("memory_limit_mb", memory_limit_mb)

        paths = list(self._scanner.scan(directory))
        cached: dict[Path, IndexedFile[ValueT]] = {}
        paths_to_index = paths
        if self._cache is not None:
            cached = self._cache.load(directory, paths)
            paths_to_index = [path for path in paths if path not in cached]

        if self._memory_estimator is not None:
            analyzed = self._index_with_memory_limit(
                paths_to_index,
                max_workers=max_workers,
                memory_limit=(
                    memory_limit_mb * MEBIBYTE
                    if memory_limit_mb is not None
                    else automatic_memory_limit()
                ),
                on_progress=on_progress,
            )
        else:
            analyzed = []
            results = ordered_parallel_map(
                self._index_file_safely,
                paths_to_index,
                max_workers=max_workers,
            )
            for result in track_progress(
                results,
                total=len(paths_to_index),
                description=f"indexing {directory}",
                unit="file",
                on_progress=on_progress,
                activity="Indexing images",
            ):
                if result is not None:
                    analyzed.append(result)

        analyzed_by_path = {item.path: item for item in analyzed}
        indexed: list[IndexedFile[ValueT]] = []
        for path in paths:
            item = cached.get(path) or analyzed_by_path.get(path)
            if item is not None:
                indexed.append(item)
        if self._cache is not None:
            self._cache.save(directory, indexed)
        return indexed

    def index_with_progress(
        self,
        directory: Path,
        *,
        max_workers: int | None = None,
        memory_limit_mb: int | None = None,
        on_progress: ProgressObserver | None = None,
    ) -> list[IndexedFile[ValueT]]:
        return self.index(
            directory,
            max_workers=max_workers,
            memory_limit_mb=memory_limit_mb,
            on_progress=on_progress,
        )

    def _index_with_memory_limit(
        self,
        paths: list[Path],
        *,
        max_workers: int | None,
        memory_limit: int,
        on_progress: ProgressObserver | None,
    ) -> list[IndexedFile[ValueT]]:
        """Analyze fitting files while aggregate estimated memory stays bounded."""

        candidates: list[_IndexCandidate] = []
        ordered: list[IndexedFile[ValueT] | None] = [None] * len(paths)
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
            candidate = self._estimate_file_safely(index, path)
            if candidate is not None and candidate.estimated_peak_bytes <= memory_limit:
                candidates.append(candidate)

        results = weighted_parallel_map(
            self._index_candidate_safely,
            candidates,
            weight=lambda candidate: candidate.estimated_peak_bytes,
            capacity=memory_limit,
            max_workers=max_workers,
        )
        for candidate, result in track_progress(
            results,
            total=len(candidates),
            description="indexing",
            unit="file",
            on_progress=on_progress,
            activity="Indexing images",
        ):
            ordered[candidate.index] = result

        return [item for item in ordered if item is not None]

    def _estimate_file_safely(
        self, index: int, path: Path
    ) -> _IndexCandidate | None:
        assert self._memory_estimator is not None
        try:
            identity = file_identity(path)
            estimate = self._memory_estimator.estimate_memory(path)
            if estimate < 1:
                raise ValueError("estimated memory must be greater than 0")
            if file_identity(path) != identity:
                return None
            return _IndexCandidate(index, path, identity, estimate)
        except self._ignored_errors:
            return None

    def _index_file_safely(
        self,
        path: Path,
        expected_identity: FileIdentity | None = None,
    ) -> IndexedFile[ValueT] | None:
        try:
            identity = file_identity(path)
            if expected_identity is not None and identity != expected_identity:
                return None
            value = self._analyzer.analyze(path)
            # Never retain an analysis of a file that changed while being read.
            if file_identity(path) != identity:
                return None
            return IndexedFile(path, value, identity)
        except self._ignored_errors:
            return None

    def _index_candidate_safely(
        self,
        candidate: _IndexCandidate,
    ) -> IndexedFile[ValueT] | None:
        return self._index_file_safely(candidate.path, candidate.identity)


@dataclass(frozen=True, slots=True)
class _IndexCandidate:
    """A file waiting for a memory reservation and analysis worker."""

    index: int
    path: Path
    identity: FileIdentity
    estimated_peak_bytes: int
