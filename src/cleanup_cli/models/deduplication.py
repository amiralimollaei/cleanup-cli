"""Generic duplicate selection and safe file removal policies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, Protocol, TypeAlias, TypeVar

from cleanup_cli.models.abstractions import (
    DirectoryIndexer,
    DistanceMetric,
    IndexedFile,
    MeasuredValueT,
)
from cleanup_cli.models.filesystem import (
    FileIdentity,
    file_identity,
    quarantine_if_unchanged,
)
from cleanup_cli.models.progress import ProgressObserver
from cleanup_cli.models.validation import (
    validate_inclusive_range,
    validate_minimum,
    validate_optional_positive,
)


SignatureT = TypeVar("SignatureT")


@dataclass(frozen=True, slots=True)
class Duplicate:
    """A duplicate path and the higher-quality path retained in its place."""

    removed: Path
    kept: Path
    distance: int
    removed_identity: FileIdentity | None = field(default=None, compare=False)

    @property
    def saved_bytes(self) -> int:
        """Return the storage reclaimed when the duplicate is deleted."""

        if self.removed_identity is not None:
            return self.removed_identity.size
        try:
            return self.removed.stat().st_size
        except OSError:
            return 0


DuplicateObserver: TypeAlias = Callable[[Duplicate], None]


@dataclass(frozen=True, slots=True)
class DeduplicationOptions:
    """Policy for matching and optionally removing duplicate files."""

    threshold: int = 0
    delete: bool = False
    max_workers: int | None = None
    memory_limit_mb: int | None = None

    def __post_init__(self) -> None:
        validate_minimum("threshold", self.threshold, minimum=0)
        validate_optional_positive("max_workers", self.max_workers)
        validate_optional_positive("memory_limit_mb", self.memory_limit_mb)


class DuplicateDetector(ABC, Generic[SignatureT]):
    """Abstract strategy for selecting duplicate indexed files."""

    @abstractmethod
    def find(
        self,
        files: Sequence[IndexedFile[SignatureT]],
        threshold: int = 0,
        *,
        on_duplicate: DuplicateObserver | None = None,
    ) -> list[Duplicate]:
        """Return files considered duplicates under *threshold*."""

    def validate_threshold(self, threshold: int) -> None:
        """Reject distances unsupported by this detector."""

        validate_minimum("threshold", threshold, minimum=0)


class CandidateIndex(Protocol[MeasuredValueT]):
    """Narrow the retained values a new candidate must be compared against."""

    def candidates(self, value: MeasuredValueT) -> Iterable[int]:
        """Return retained-list indexes to test in ascending order."""
        ...

    def add(self, value: MeasuredValueT, rank: int) -> None:
        """Record that *value* was retained at *rank*."""
        ...


class ExhaustiveCandidateIndex:
    """Compare a candidate against every retained value."""

    def __init__(self, threshold: int = 0) -> None:
        self._retained = 0

    def candidates(self, value: object) -> Iterable[int]:
        return range(self._retained)

    def add(self, value: object, rank: int) -> None:
        self._retained += 1


class QualityAwareDuplicateDetector(DuplicateDetector[SignatureT]):
    """Keep the best match while avoiding non-transitive deletion chains."""

    def __init__(
        self,
        metric: DistanceMetric[SignatureT],
        quality_key: Callable[[IndexedFile[SignatureT]], Any] | None = None,
        index_factory: Callable[[int], CandidateIndex[SignatureT]] | None = None,
        *,
        maximum_threshold: int | None = None,
    ) -> None:
        self._metric = metric
        self._quality_key = quality_key or (lambda _: 0)
        self._index_factory = index_factory or ExhaustiveCandidateIndex
        self._maximum_threshold = maximum_threshold

    def validate_threshold(self, threshold: int) -> None:
        if self._maximum_threshold is None:
            super().validate_threshold(threshold)
            return
        validate_inclusive_range(
            "threshold",
            threshold,
            minimum=0,
            maximum=self._maximum_threshold,
        )

    def find(
        self,
        files: Sequence[IndexedFile[SignatureT]],
        threshold: int = 0,
        *,
        on_duplicate: DuplicateObserver | None = None,
    ) -> list[Duplicate]:
        self.validate_threshold(threshold)

        ranked_files = sorted(files, key=self._quality_key)
        index = self._index_factory(threshold)
        kept: list[IndexedFile[SignatureT]] = []
        duplicates: list[Duplicate] = []
        for file in reversed(ranked_files):
            match = self._find_match(file, kept, index, threshold)
            if match is None:
                index.add(file.value, len(kept))
                kept.append(file)
                continue

            kept_path, distance = match
            duplicate = Duplicate(file.path, kept_path, distance, file.identity)
            duplicates.append(duplicate)
            if on_duplicate is not None:
                on_duplicate(duplicate)

        return _restore_input_order(duplicates, files)

    def _find_match(
        self,
        file: IndexedFile[SignatureT],
        kept: Sequence[IndexedFile[SignatureT]],
        index: CandidateIndex[SignatureT],
        threshold: int,
    ) -> tuple[Path, int] | None:
        for rank in index.candidates(file.value):
            retained = kept[rank]
            distance = self._metric.distance(file.value, retained.value)
            if distance <= threshold:
                return retained.path, distance
        return None


def _restore_input_order(
    duplicates: list[Duplicate],
    files: Sequence[IndexedFile[SignatureT]],
) -> list[Duplicate]:
    if not duplicates:
        return duplicates
    positions = {file.path: position for position, file in enumerate(files)}
    duplicates.sort(key=lambda duplicate: positions[duplicate.removed])
    return duplicates


class FileRemover(Protocol):
    """Remove a file selected by a deduplication policy."""

    def remove(self, path: Path, expected: FileIdentity | None = None) -> None:
        """Remove *path* from storage."""
        ...


class LocalFileRemover:
    """Remove files from the local filesystem."""

    def remove(self, path: Path, expected: FileIdentity | None = None) -> None:
        if expected is None:
            path.unlink()
            return
        try:
            quarantine = quarantine_if_unchanged(path, expected)
        except OSError as error:
            raise FileChangedError(str(error)) from error
        quarantine.unlink()
        quarantine.parent.rmdir()


class FileChangedError(OSError):
    """A destructive action was refused because its input became stale."""


class DirectoryDeduplicator(Generic[SignatureT]):
    """Coordinate indexing, duplicate detection, and optional removal."""

    def __init__(
        self,
        indexer: DirectoryIndexer[SignatureT],
        detector: DuplicateDetector[SignatureT],
        *,
        remover: FileRemover | None = None,
    ) -> None:
        self._indexer = indexer
        self._detector = detector
        self._remover = remover or LocalFileRemover()

    def deduplicate(
        self,
        directory: Path,
        options: DeduplicationOptions | None = None,
        *,
        on_result: DuplicateObserver | None = None,
        on_progress: ProgressObserver | None = None,
    ) -> list[Duplicate]:
        request = options or DeduplicationOptions()
        self._detector.validate_threshold(request.threshold)
        files = self._indexer.index(
            directory,
            max_workers=request.max_workers,
            memory_limit_mb=request.memory_limit_mb,
            on_progress=on_progress,
        )
        reported: dict[Path, Duplicate] = {}

        def handle(duplicate: Duplicate) -> None:
            result = _with_identity(duplicate)
            reported[result.removed] = result
            if request.delete:
                self._remover.remove(result.removed, result.removed_identity)
            if on_result is not None:
                on_result(result)

        duplicates = self._detector.find(
            files,
            request.threshold,
            on_duplicate=handle,
        )
        return [reported.get(duplicate.removed, duplicate) for duplicate in duplicates]


def _with_identity(duplicate: Duplicate) -> Duplicate:
    if duplicate.removed_identity is not None:
        return duplicate
    try:
        identity = file_identity(duplicate.removed)
    except OSError:
        return duplicate
    return Duplicate(
        duplicate.removed,
        duplicate.kept,
        duplicate.distance,
        identity,
    )
