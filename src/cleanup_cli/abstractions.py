"""Reusable contracts for ordered directory analysis."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Protocol, TypeVar

from .path_sort import sort_numbered_paths


ValueT = TypeVar("ValueT")
AnalyzedValueT = TypeVar("AnalyzedValueT", covariant=True)
MeasuredValueT = TypeVar("MeasuredValueT", contravariant=True)


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

        files = (path for path in directory.rglob("*") if path.is_file())
        yield from self._orderer.order(files)


class RecursiveDirectoryIndexer(DirectoryIndexer[ValueT]):
    """Analyze all supported files below a directory."""

    def __init__(
        self,
        analyzer: FileAnalyzer[ValueT],
        *,
        scanner: DirectoryScanner | None = None,
        orderer: PathOrderer | None = None,
        ignored_errors: tuple[type[Exception], ...] = (),
    ) -> None:
        self._analyzer = analyzer
        if scanner is not None and orderer is not None:
            raise ValueError("scanner and orderer cannot both be provided")
        self._scanner = scanner or RecursiveDirectoryScanner(orderer)
        self._ignored_errors = ignored_errors

    def index(self, directory: Path) -> list[IndexedFile[ValueT]]:
        indexed: list[IndexedFile[ValueT]] = []
        for path in self._scanner.scan(directory):
            try:
                indexed.append(IndexedFile(path, self._analyzer.analyze(path)))
            except self._ignored_errors:
                continue
        return indexed