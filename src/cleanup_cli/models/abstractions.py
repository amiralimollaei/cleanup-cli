"""Reusable contracts for ordered directory analysis."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
import os
from pathlib import Path
import tempfile
from typing import Generic, Protocol, TypeVar

import tqdm

from av.error import FFmpegError

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
        os.link(quarantine, path, follow_symlinks=False)
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


@dataclass(frozen=True)
class IndexedFile(Generic[ValueT]):
    """A file paired with its analyzed domain value."""

    path: Path
    value: ValueT
    identity: FileIdentity | None = field(default=None, compare=False)


class DirectoryIndexer(ABC, Generic[ValueT]):
    """Abstract source of analyzed files from a directory."""

    @abstractmethod
    def index(self, directory: Path) -> list[IndexedFile[ValueT]]:
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
        if not directory.is_dir():
            raise NotADirectoryError(directory)

        files = (
            path
            for path in directory.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        yield from self._orderer.order(files)


class RecursiveDirectoryIndexer(DirectoryIndexer[ValueT]):
    """Analyze all supported files below a directory."""

    def __init__(
        self,
        analyzer: FileAnalyzer[ValueT],
        *,
        scanner: DirectoryScanner | None = None,
        orderer: PathOrderer | None = None,
        ignored_errors: tuple[type[Exception], ...] = (FFmpegError, EOFError, StopIteration, ValueError, IndexError),
    ) -> None:
        self._analyzer = analyzer
        if scanner is not None and orderer is not None:
            raise ValueError("scanner and orderer cannot both be provided")
        self._scanner = scanner or RecursiveDirectoryScanner(orderer)
        self._ignored_errors = ignored_errors

    def index(self, directory: Path) -> list[IndexedFile[ValueT]]:
        indexed: list[IndexedFile[ValueT]] = []
        for path in tqdm.tqdm(self._scanner.scan(directory), desc=f"indexing {directory}", unit="file"):
            try:
                identity = file_identity(path)
                value = self._analyzer.analyze(path)
                # Never retain an analysis of a file that changed while being
                # read; a later destructive operation must target this exact
                # snapshot rather than merely the same pathname.
                if file_identity(path) != identity:
                    continue
                indexed.append(IndexedFile(path, value, identity))
            except self._ignored_errors:
                continue
        return indexed
